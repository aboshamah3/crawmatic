"""Release identity — baked build manifest, `/version`, `/ready`, heartbeats (READY-001, Task A5).

Audit ref: `PRODUCTION_READINESS_IMPLEMENTATION_PLAN_2026-08-25.md` Task A5
("Release identity — build manifest, deployment attestation, `/version`,
external probes").

What this file pins down
------------------------

1. **Two linked records, not one mutable file.** `scripts/build_release_manifest.py`
   emits an immutable, self-hashed *build manifest*; `scripts/build_deployment_attestation.py`
   emits a *deployment attestation* that references it by digest. Deploy-time
   facts (backup id, approver, live migration head, smoke results) never
   mutate the signed build manifest.
2. **Baked wins over env.** Release identity is injected at BUILD time (a
   generated `release_identity.json` / `app_shared._baked_release` module),
   not read from mutable Railway environment variables. Env vars may *mirror*
   the baked values; when they disagree the baked value is what is reported
   and the disagreement is surfaced, never silently resolved.
3. **`/version` exposes BOTH migration heads** — the code-expected head (from
   the running code's Alembic script directory) and the live database head
   (read from `alembic_version` at request time) — so a mismatch is explicit.
4. **`/ready` returns 503 on migration mismatch**, and aggregates *per-instance*
   worker/scheduler heartbeat freshness rather than a single global flag.

Test-harness notes
------------------

These tests follow this repo's established router-test convention exactly
(`tests/unit/test_version_endpoint.py`, `tests/unit/test_ready_endpoint.py`):
a `TestClient(app)` plus `app.dependency_overrides` on each router's own
named DB/Redis dependency, with small hand-rolled in-memory doubles. No live
Postgres and no live Redis are required, and `fakeredis` is deliberately not
a dependency anywhere in this repo (see `tests/unit/test_match_lock.py`).

In particular the plan's `alembic_downgraded_db` fixture is realised here as
a session double whose `alembic_version` row reports an *older* revision than
the running code's head. That is precisely the state `alembic downgrade -1`
produces, expressed in the DB-free style every other unit test in this
directory uses; the real-database version of the same assertion belongs in
`tests/integration/`, not here.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from collections.abc import Iterator
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import ready, version
from app_shared import heartbeat as heartbeat_mod
from app_shared import release as release_mod

_REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------
# Doubles (mirroring tests/unit/test_version_endpoint.py + test_ready_endpoint.py)
# --------------------------------------------------------------------------


class _FakeRow:
    def __init__(self, value: str) -> None:
        self._value = value

    def __getitem__(self, index: int) -> str:
        assert index == 0
        return self._value


class _FakeResult:
    def __init__(self, row: _FakeRow | None) -> None:
        self._row = row

    def first(self) -> _FakeRow | None:
        return self._row


class _FakeSession:
    """Answers `SELECT version_num FROM alembic_version` with `head`, the
    `proxy_circuit_breakers` freshness read (EPA B1) with a just-evaluated
    row, and any other statement (`SELECT 1`, the `/ready` connectivity
    probe) with None.

    These tests are about release identity, not about the breaker: a fresh
    breaker row keeps "every dependency is up" meaning exactly what it
    meant before `/ready` grew that check."""

    def __init__(self, *, head: str | None = None, raises: Exception | None = None) -> None:
        self._head = head
        self._raises = raises

    def execute(self, statement: Any = None, *_args: object, **_kwargs: object) -> Any:
        if self._raises is not None:
            raise self._raises
        rendered = str(statement)
        if "alembic_version" in rendered:
            return _FakeResult(_FakeRow(self._head) if self._head is not None else None)
        if "proxy_circuit_breakers" in rendered:
            return _FakeResult((datetime.now(UTC), "CLOSED"))
        return _FakeResult(None)


class _FakeRedis:
    """In-memory stand-in for the `redis.Redis` subset `app_shared.heartbeat`
    uses: `get`/`set(ex=)`/`incr`/`scan_iter`/`ttl`/`ping`. Per this repo's
    convention (`tests/unit/test_match_lock.py`) there is no `fakeredis`
    dependency; expiry is modelled by an explicit `expire(key)` helper the
    tests call, which is what a TTL lapse looks like to a reader."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self._raises = raises

    def _guard(self) -> None:
        if self._raises is not None:
            raise self._raises

    def ping(self) -> bool:
        self._guard()
        return True

    def get(self, key: str) -> str | None:
        self._guard()
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None, **_kw: object) -> bool:
        self._guard()
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    def incr(self, key: str) -> int:
        self._guard()
        nxt = int(self.store.get(key, "0")) + 1
        self.store[key] = str(nxt)
        return nxt

    def ttl(self, key: str) -> int:
        self._guard()
        return self.ttls.get(key, -1)

    def scan_iter(self, match: str = "*", count: int | None = None) -> Iterator[str]:
        self._guard()
        import fnmatch

        for key in list(self.store):
            if fnmatch.fnmatch(key, match):
                yield key

    def expire_key(self, key: str) -> None:
        """Model a TTL lapse — the key simply stops existing."""
        self.store.pop(key, None)
        self.ttls.pop(key, None)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()
    release_mod.reset_release_identity_cache()


@pytest.fixture()
def api_client() -> TestClient:
    return TestClient(app)


@pytest.fixture()
def baked_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> release_mod.ReleaseIdentity:
    """A build-time-baked identity file, exactly as `build_release_manifest.py
    --emit-identity` writes it into the image."""
    payload = {
        "manifest_id": "sha256:" + "a1" * 32,
        "source_digest": "sha256:" + "b2" * 32,
        "image_digest": "sha256:" + "c3" * 32,
        "config_schema_version": "sha256:" + "d4" * 32,
        "expected_db_migration": "bakedhead001",
    }
    path = tmp_path / "release_identity.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(release_mod.RELEASE_IDENTITY_PATH_ENV, str(path))
    release_mod.reset_release_identity_cache()
    return release_mod.get_release_identity()


def _override_version_session(session: object) -> None:
    def _dep() -> Iterator[object]:
        yield session

    app.dependency_overrides[version._get_db_session] = _dep


def _override_ready(
    monkeypatch: pytest.MonkeyPatch, session: object, redis_client: object
) -> None:
    """EPA B8 (F16): `/ready` no longer takes a request-scoped DB/Redis
    dependency — each probe opens its own via the module-level
    `get_session`/`get_redis_client` names, so tests monkeypatch those
    directly instead of using `app.dependency_overrides`."""
    monkeypatch.setattr(ready, "get_session", lambda: nullcontext(session))
    monkeypatch.setattr(ready, "get_redis_client", lambda: redis_client)


@pytest.fixture()
def alembic_downgraded_db() -> _FakeSession:
    """The state `alembic downgrade -1` leaves behind: the database reports an
    older revision than the running code's head."""
    return _FakeSession(head="older_revision_000")


# ==========================================================================
# 1. The plan's two named acceptance tests
# ==========================================================================


def test_version_reports_baked_manifest_and_both_migration_heads(
    api_client: TestClient,
    baked_identity: release_mod.ReleaseIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(version, "_code_migration_head", lambda: "codehead123")
    _override_version_session(_FakeSession(head="codehead123"))

    resp = api_client.get("/version")
    body = resp.json()

    assert resp.status_code == 200
    assert body["manifest_id"] == baked_identity.manifest_id
    assert body["source_digest"] == baked_identity.source_digest
    assert body["expected_db_migration"]  # from the running code
    assert body["live_db_migration"]  # read live from alembic_version


def test_readiness_fails_on_migration_mismatch(
    api_client: TestClient,
    alembic_downgraded_db: _FakeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "codehead123")
    _override_ready(monkeypatch, alembic_downgraded_db, _FakeRedis())

    resp = api_client.get("/ready")

    assert resp.status_code == 503


# ==========================================================================
# 2. `/version` — extends, never breaks, the deployed contract
# ==========================================================================


def test_version_preserves_existing_deployed_fields(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Other code (and the operator runbook) already reads `git_sha`,
    `code_migration_head`, `db_migration_head`, `migration_heads_match` and
    `db_error`. A5 ADDS fields; it must not rename or drop these."""
    monkeypatch.setattr(version, "_code_migration_head", lambda: "codehead123")
    _override_version_session(_FakeSession(head="codehead123"))

    body = api_client.get("/version").json()

    for legacy_field in (
        "git_sha",
        "build_time",
        "code_migration_head",
        "db_migration_head",
        "migration_heads_match",
        "db_error",
    ):
        assert legacy_field in body, legacy_field
    assert body["code_migration_head"] == "codehead123"
    assert body["db_migration_head"] == "codehead123"
    assert body["migration_heads_match"] is True


def test_version_new_and_legacy_migration_fields_agree(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(version, "_code_migration_head", lambda: "codehead123")
    _override_version_session(_FakeSession(head="older_revision_000"))

    body = api_client.get("/version").json()

    assert body["expected_db_migration"] == body["code_migration_head"] == "codehead123"
    assert body["live_db_migration"] == body["db_migration_head"] == "older_revision_000"
    assert body["migration_heads_match"] is False


def test_version_reports_identity_source_when_nothing_is_baked(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(release_mod.RELEASE_IDENTITY_PATH_ENV, raising=False)
    for env_name in release_mod.ENV_MIRRORS.values():
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr(release_mod, "_baked_payload_from_module", lambda: None)
    monkeypatch.setattr(release_mod, "_baked_payload_from_wellknown_files", lambda: None)
    release_mod.reset_release_identity_cache()
    monkeypatch.setattr(version, "_code_migration_head", lambda: "codehead123")
    _override_version_session(_FakeSession(head="codehead123"))

    body = api_client.get("/version").json()

    assert body["manifest_id"] is None
    assert body["identity_source"] == "unavailable"
    # An unbaked image is still a *legible* one: the migration provenance the
    # deployed endpoint already promised keeps working.
    assert body["expected_db_migration"] == "codehead123"


def test_version_never_leaks_config_values_only_the_schema_version(
    api_client: TestClient,
    baked_identity: release_mod.ReleaseIdentity,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/version` is unauthenticated. It may publish a *digest* of the config
    schema, never a name→value map, and never a secret."""
    monkeypatch.setattr(version, "_code_migration_head", lambda: "codehead123")
    _override_version_session(_FakeSession(head="codehead123"))

    resp = api_client.get("/version")
    body = resp.json()

    assert body["config_schema_version"] == baked_identity.config_schema_version
    for forbidden in ("DATABASE_URL", "REDIS_URL", "JWT_SECRET", "postgresql://", "redis://"):
        assert forbidden not in resp.text


# ==========================================================================
# 3. Baked-wins-over-env
# ==========================================================================


def test_baked_identity_wins_over_mutable_env_and_surfaces_the_mismatch(
    baked_identity: release_mod.ReleaseIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(release_mod.ENV_MIRRORS["source_digest"], "sha256:" + "ff" * 32)
    release_mod.reset_release_identity_cache()

    identity = release_mod.get_release_identity()

    assert identity.identity_source == "baked"
    assert identity.source_digest == baked_identity.source_digest  # baked wins
    assert "source_digest" in identity.env_mismatches  # and the drift is reported


def test_env_mirror_agreeing_with_baked_is_not_a_mismatch(
    baked_identity: release_mod.ReleaseIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(release_mod.ENV_MIRRORS["source_digest"], baked_identity.source_digest or "")
    release_mod.reset_release_identity_cache()

    assert "source_digest" not in release_mod.get_release_identity().env_mismatches


def test_baked_migration_head_disagreeing_with_the_code_head_is_surfaced(
    baked_identity: release_mod.ReleaseIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `baked_identity` fixture bakes `"bakedhead001"`, which is not this
    repo's real head — the drifted-bake case. The code-resolved head wins (it
    is what the running code will actually run against) and the disagreement
    with the build's claim is published rather than averaged away."""
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "codehead123")
    release_mod.reset_release_identity_cache()

    identity = release_mod.get_release_identity()

    assert identity.expected_db_migration == "codehead123"
    assert "baked_expected_db_migration" in identity.env_mismatches


def test_matching_baked_and_code_heads_are_not_reported_as_drift(
    baked_identity: release_mod.ReleaseIdentity, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "bakedhead001")
    release_mod.reset_release_identity_cache()

    identity = release_mod.get_release_identity()

    assert identity.expected_db_migration == "bakedhead001"
    assert identity.env_mismatches == ()


def test_env_only_identity_is_reported_as_env_sourced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(release_mod.RELEASE_IDENTITY_PATH_ENV, raising=False)
    monkeypatch.setattr(release_mod, "_baked_payload_from_module", lambda: None)
    monkeypatch.setattr(release_mod, "_baked_payload_from_wellknown_files", lambda: None)
    monkeypatch.setenv(release_mod.ENV_MIRRORS["manifest_id"], "sha256:" + "0e" * 32)
    release_mod.reset_release_identity_cache()

    identity = release_mod.get_release_identity()

    assert identity.identity_source == "env"
    assert identity.manifest_id == "sha256:" + "0e" * 32


def test_config_schema_version_is_deterministic_and_value_free() -> None:
    first = release_mod.compute_config_schema_version()
    second = release_mod.compute_config_schema_version()
    assert first == second
    assert first.startswith("sha256:")

    schema = release_mod.config_schema()
    names = {field["name"] for field in schema}
    assert "DATABASE_URL" in names and "JWT_SECRET" in names
    # Names and types only — no `value`/`default` key may exist on any entry.
    for field in schema:
        assert set(field) == {"name", "type", "required"}


# ==========================================================================
# 4. `/ready` — migration gate + per-instance heartbeat aggregation
# ==========================================================================


def test_ready_is_200_when_heads_match(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "codehead123")
    _override_ready(monkeypatch, _FakeSession(head="codehead123"), _FakeRedis())

    resp = api_client.get("/ready")
    body = resp.json()

    assert resp.status_code == 200
    assert body["ready"] is True
    assert body["checks"]["migrations"]["ok"] is True


def test_ready_migration_mismatch_names_the_failure_without_leaking(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "codehead123")
    _override_ready(monkeypatch, _FakeSession(head="older_revision_000"), _FakeRedis())

    resp = api_client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["checks"]["migrations"]["ok"] is False
    assert body["checks"]["migrations"]["error"] == "MigrationHeadMismatch"
    assert "postgresql://" not in resp.text


def test_ready_heartbeats_not_configured_is_reported_not_failed(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No declared heartbeat services (the state before Step 7 wires config)
    is a labelled absence, not a false readiness failure."""
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "codehead123")
    monkeypatch.delenv(heartbeat_mod.REQUIRED_SERVICES_ENV, raising=False)
    _override_ready(monkeypatch, _FakeSession(head="codehead123"), _FakeRedis())

    resp = api_client.get("/ready")
    body = resp.json()

    assert resp.status_code == 200
    assert body["checks"]["heartbeats"]["ok"] is True
    assert body["checks"]["heartbeats"]["detail"] == "not-configured"


def test_ready_fails_when_a_declared_service_has_no_fresh_instance(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "codehead123")
    monkeypatch.setenv(heartbeat_mod.REQUIRED_SERVICES_ENV, "worker,scheduler")
    redis_client = _FakeRedis()
    # Only `worker` ever beat; `scheduler` has never been seen.
    heartbeat_mod.HeartbeatEmitter(
        redis_client, service="worker", instance_id="w-1"
    ).start().beat()
    _override_ready(monkeypatch, _FakeSession(head="codehead123"), redis_client)

    resp = api_client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["checks"]["heartbeats"]["ok"] is False
    assert "scheduler" in (body["checks"]["heartbeats"]["detail"] or "")


def test_ready_ok_when_every_declared_service_has_a_fresh_instance(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "codehead123")
    monkeypatch.setenv(heartbeat_mod.REQUIRED_SERVICES_ENV, "worker,scheduler")
    redis_client = _FakeRedis()
    for service, instance in (("worker", "w-1"), ("worker", "w-2"), ("scheduler", "s-1")):
        heartbeat_mod.HeartbeatEmitter(
            redis_client, service=service, instance_id=instance
        ).start().beat()
    _override_ready(monkeypatch, _FakeSession(head="codehead123"), redis_client)

    resp = api_client.get("/ready")
    body = resp.json()

    assert resp.status_code == 200
    assert body["checks"]["heartbeats"]["ok"] is True


def test_ready_still_fails_on_database_down(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The A5 additions must not mask the checks `/ready` already performed."""
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "codehead123")
    _override_ready(monkeypatch, _FakeSession(raises=RuntimeError("connection refused")), _FakeRedis())

    resp = api_client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["checks"]["database"]["ok"] is False
    assert "connection refused" not in resp.text


def test_migration_check_is_short_circuited_when_the_database_check_failed(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out `database` check leaves its worker thread holding the
    session (`pool.shutdown(wait=False)`), and a SQLAlchemy `Session` is not
    thread-safe — so the migration check must not touch it. One root cause,
    reported once."""
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "codehead123")
    _override_ready(monkeypatch, _FakeSession(raises=RuntimeError("connection refused")), _FakeRedis())

    body = api_client.get("/ready").json()

    assert body["checks"]["migrations"]["ok"] is False
    assert body["checks"]["migrations"]["error"] == "DatabaseUnavailable"
    assert "not checked" in body["checks"]["migrations"]["detail"]


def test_heartbeat_check_is_short_circuited_when_redis_is_down(
    api_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(release_mod, "code_migration_head", lambda: "codehead123")
    monkeypatch.setenv(heartbeat_mod.REQUIRED_SERVICES_ENV, "worker")
    _override_ready(
        monkeypatch, _FakeSession(head="codehead123"),
        _FakeRedis(raises=ConnectionError("redis://user:pw@host:6379 refused")),
    )

    resp = api_client.get("/ready")
    body = resp.json()

    assert resp.status_code == 503
    assert body["checks"]["heartbeats"]["error"] == "RedisUnavailable"
    # The short-circuit also means no Redis error text can reach this body.
    assert "pw" not in resp.text and "redis://" not in resp.text


# ==========================================================================
# 5. Heartbeats: instance identity, monotonic timestamps, fencing
# ==========================================================================


def test_heartbeat_key_shape_and_ttl() -> None:
    assert heartbeat_mod.heartbeat_key("worker", "abc") == "heartbeat:worker:abc"
    assert heartbeat_mod.HEARTBEAT_TTL_SECONDS == 120


def test_heartbeat_written_with_identity_monotonic_time_and_ttl() -> None:
    redis_client = _FakeRedis()
    emitter = heartbeat_mod.HeartbeatEmitter(
        redis_client, service="worker", instance_id="w-1"
    ).start()

    assert emitter.beat() is True

    key = heartbeat_mod.heartbeat_key("worker", "w-1")
    record = json.loads(redis_client.store[key])
    assert record["service"] == "worker"
    assert record["instance_id"] == "w-1"
    assert record["fence"] >= 1
    assert record["monotonic_ns"] > 0
    assert redis_client.ttls[key] == heartbeat_mod.HEARTBEAT_TTL_SECONDS


def test_heartbeat_monotonic_timestamp_strictly_increases_within_a_fence() -> None:
    redis_client = _FakeRedis()
    emitter = heartbeat_mod.HeartbeatEmitter(
        redis_client, service="worker", instance_id="w-1"
    ).start()
    emitter.beat()
    first = json.loads(redis_client.store[heartbeat_mod.heartbeat_key("worker", "w-1")])
    emitter.beat()
    second = json.loads(redis_client.store[heartbeat_mod.heartbeat_key("worker", "w-1")])

    assert second["monotonic_ns"] > first["monotonic_ns"]


def test_a_replayed_stale_beat_is_rejected() -> None:
    """A stale timestamp from the same fence cannot refresh the TTL — that is
    exactly how a hung-then-resumed process would fake liveness."""
    redis_client = _FakeRedis()
    emitter = heartbeat_mod.HeartbeatEmitter(
        redis_client, service="worker", instance_id="w-1"
    ).start()
    emitter.beat()
    live = json.loads(redis_client.store[heartbeat_mod.heartbeat_key("worker", "w-1")])

    accepted = heartbeat_mod.write_heartbeat(
        redis_client,
        service="worker",
        instance_id="w-1",
        fence=live["fence"],
        monotonic_ns=live["monotonic_ns"] - 1,
        wall_clock_epoch=live["wall_clock_epoch"] + 1,
    )

    assert accepted is False


def test_a_zombie_duplicate_with_a_lower_fence_cannot_keep_the_service_healthy() -> None:
    """The plan's core requirement: 'a stale or duplicate process cannot keep
    the global service heartbeat healthy'. A restarted instance takes a HIGHER
    fence; the old process's writes are then refused outright."""
    redis_client = _FakeRedis()
    zombie = heartbeat_mod.HeartbeatEmitter(
        redis_client, service="worker", instance_id="w-1"
    ).start()
    zombie.beat()

    restarted = heartbeat_mod.HeartbeatEmitter(
        redis_client, service="worker", instance_id="w-1"
    ).start()
    assert restarted.fence > zombie.fence
    assert restarted.beat() is True

    # The zombie is now fenced out and can never refresh the key again.
    assert zombie.beat() is False
    record = json.loads(redis_client.store[heartbeat_mod.heartbeat_key("worker", "w-1")])
    assert record["fence"] == restarted.fence


def test_freshness_counts_instances_individually_not_as_one_global_flag() -> None:
    redis_client = _FakeRedis()
    for instance in ("w-1", "w-2", "w-3"):
        heartbeat_mod.HeartbeatEmitter(
            redis_client, service="worker", instance_id=instance
        ).start().beat()

    # One instance's key lapses (its TTL expired — the process died).
    redis_client.expire_key(heartbeat_mod.heartbeat_key("worker", "w-2"))

    freshness = heartbeat_mod.aggregate_service_freshness(redis_client, "worker")

    assert freshness.fresh_instance_count == 2
    assert {i.instance_id for i in freshness.instances} == {"w-1", "w-3"}
    assert freshness.ok is True


def test_service_with_zero_fresh_instances_is_not_ok() -> None:
    redis_client = _FakeRedis()
    heartbeat_mod.HeartbeatEmitter(
        redis_client, service="worker", instance_id="w-1"
    ).start().beat()
    redis_client.expire_key(heartbeat_mod.heartbeat_key("worker", "w-1"))

    freshness = heartbeat_mod.aggregate_service_freshness(redis_client, "worker")

    assert freshness.fresh_instance_count == 0
    assert freshness.ok is False


def test_min_instances_floor_is_enforced_per_service() -> None:
    redis_client = _FakeRedis()
    heartbeat_mod.HeartbeatEmitter(
        redis_client, service="worker", instance_id="w-1"
    ).start().beat()

    assert heartbeat_mod.aggregate_service_freshness(
        redis_client, "worker", min_instances=2
    ).ok is False
    assert heartbeat_mod.aggregate_service_freshness(
        redis_client, "worker", min_instances=1
    ).ok is True


def test_wall_clock_age_beyond_the_budget_is_stale_even_if_the_key_survives() -> None:
    """TTL is the authoritative expiry, but a clock-skewed or paused writer
    whose key somehow outlives its own budget is still reported stale."""
    redis_client = _FakeRedis()
    emitter = heartbeat_mod.HeartbeatEmitter(
        redis_client, service="worker", instance_id="w-1"
    ).start()
    emitter.beat()
    key = heartbeat_mod.heartbeat_key("worker", "w-1")
    record = json.loads(redis_client.store[key])
    record["wall_clock_epoch"] -= heartbeat_mod.HEARTBEAT_TTL_SECONDS * 10
    redis_client.store[key] = json.dumps(record)

    freshness = heartbeat_mod.aggregate_service_freshness(redis_client, "worker")

    assert freshness.fresh_instance_count == 0
    assert freshness.ok is False


def test_unreadable_redis_is_a_failed_aggregation_not_a_healthy_one() -> None:
    """Fail-closed: 'I cannot tell whether the fleet is alive' is not ready."""
    freshness = heartbeat_mod.aggregate_service_freshness(
        _FakeRedis(raises=ConnectionError("redis://user:pw@host:6379 refused")), "worker"
    )

    assert freshness.ok is False
    assert freshness.error == "ConnectionError"
    assert "pw" not in (freshness.detail or "")


def test_a_beat_against_an_unreachable_redis_returns_false_and_never_raises() -> None:
    """A Redis blip must not crash a worker loop. `beat()` reports the
    failure by returning False; the key then ages out and the fleet reads
    degraded, which is the truthful outcome."""
    broken = _FakeRedis(raises=ConnectionError("redis://user:pw@host:6379 refused"))
    emitter = heartbeat_mod.HeartbeatEmitter(broken, service="worker", instance_id="w-1")

    assert emitter.start().fence == 0  # INCR failed; degraded to unfenced
    assert emitter.beat() is False


def test_start_degrades_to_unfenced_rather_than_refusing_to_run() -> None:
    """Refusing to start a worker because a fencing counter was briefly
    unreachable would convert a monitoring outage into a processing one."""
    emitter = heartbeat_mod.HeartbeatEmitter(
        _FakeRedis(raises=TimeoutError("slow")), service="worker", instance_id="w-1"
    ).start()

    assert emitter.fence == 0


def test_corrupt_heartbeat_record_is_ignored_not_counted_fresh() -> None:
    redis_client = _FakeRedis()
    redis_client.set(heartbeat_mod.heartbeat_key("worker", "w-1"), "not json", ex=120)

    freshness = heartbeat_mod.aggregate_service_freshness(redis_client, "worker")

    assert freshness.fresh_instance_count == 0


def test_required_services_env_parses_to_a_clean_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(heartbeat_mod.REQUIRED_SERVICES_ENV, " worker , , scheduler ")
    assert heartbeat_mod.required_heartbeat_services() == ("worker", "scheduler")
    monkeypatch.delenv(heartbeat_mod.REQUIRED_SERVICES_ENV, raising=False)
    assert heartbeat_mod.required_heartbeat_services() == ()


# ==========================================================================
# 6. The two builder scripts — immutable manifest + linked attestation
# ==========================================================================


@dataclass
class _ScriptResult:
    """What a CLI invocation produced, in the shape `subprocess.run` returns."""

    returncode: int
    stdout: str
    stderr: str


def _load_script(name: str) -> Any:
    """Import a `scripts/*.py` CLI as a module, once, by path.

    Deliberately IN-PROCESS rather than `subprocess.run([sys.executable, ...])`.
    Both scripts expose `main(argv)`, so argparse — the whole CLI surface these
    tests care about — is still exercised end to end. What the subprocess
    bought was an extra interpreter spawn per test, and this file would need
    ten of them: each one re-imports alembic and walks the git tree. On a
    loaded machine that is enough to starve OTHER subprocess-based tests in
    this suite (several assert on a child's stdout and simply see `''` when the
    child is starved), turning a green suite into a flaky one for no coverage
    gain. In-process is faster, deterministic, and reports real tracebacks
    instead of an opaque exit code.
    """
    module_name = f"_a5_script_{name.removesuffix('.py')}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, _REPO_ROOT / "scripts" / name
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _run_script(name: str, *args: str) -> _ScriptResult:
    """Invoke a builder CLI's `main(argv)`, capturing output and exit code.

    `argparse` and the scripts' own validation both signal failure by raising
    `SystemExit`, which is caught here and turned into a return code so these
    tests read exactly as they would against a real subprocess.
    """
    module = _load_script(name)
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = int(module.main(list(args)) or 0)
    except SystemExit as exc:
        raw = exc.code
        code = 0 if raw is None else (raw if isinstance(raw, int) else 1)
        if isinstance(raw, str):
            err.write(raw)
    return _ScriptResult(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())


def _build_manifest(tmp_path: Path, *extra: str) -> tuple[Path, dict[str, Any]]:
    out = tmp_path / "release_manifest.json"
    result = _run_script(
        "build_release_manifest.py",
        "--out",
        str(out),
        "--image-digest",
        "api=sha256:" + "11" * 32,
        *extra,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))

    # `build_release_manifest.py`'s own `_source_record()` reports THIS
    # checkout's real `git status --porcelain` (H6, production-readiness
    # audit) — which is whatever it happens to be while this suite runs,
    # not a property these tests are exercising. Force it clean here so
    # every test using this helper is deterministic regardless of the
    # ambient working tree, and so `build_deployment_attestation.py`'s
    # dirty-manifest refusal (also H6; see test_write_release_manifest.py
    # for the tests that specifically target it) doesn't leak into tests
    # that have nothing to do with it. Recomputing manifest_id keeps the
    # file's own self-hash check consistent with the edit.
    brm = _load_script("build_release_manifest.py")
    if manifest.get("source", {}).get("dirty") is not False:
        manifest["source"]["dirty"] = False
        manifest["manifest_id"] = brm.compute_manifest_id(manifest)
        out.write_text(json.dumps(manifest), encoding="utf-8")

    return out, manifest


def test_build_release_manifest_emits_a_self_hashed_immutable_record(tmp_path: Path) -> None:
    _, manifest = _build_manifest(tmp_path)

    assert manifest["manifest_id"].startswith("sha256:")
    assert manifest["source"]["digest"].startswith("sha256:")
    assert manifest["images"]["api"] == "sha256:" + "11" * 32
    assert manifest["migrations"]["head"]
    assert isinstance(manifest["migrations"]["revisions"], list)
    assert manifest["api_schema"]["version"]
    assert manifest["config_schema"]["version"].startswith("sha256:")
    assert manifest["signing"]["mechanism"] == "sha256-self-hash+optional-gpg-detached"


def test_build_release_manifest_is_deterministic_for_the_same_inputs(tmp_path: Path) -> None:
    _, first = _build_manifest(tmp_path / "a", "--generated-at", "2026-08-25T00:00:00Z")
    _, second = _build_manifest(tmp_path / "b", "--generated-at", "2026-08-25T00:00:00Z")
    assert first["manifest_id"] == second["manifest_id"]


def test_build_release_manifest_config_schema_carries_names_never_values(
    tmp_path: Path,
) -> None:
    out, manifest = _build_manifest(tmp_path)

    names = {field["name"] for field in manifest["config_schema"]["fields"]}
    assert "DATABASE_URL" in names and "JWT_SECRET" in names
    raw = out.read_text(encoding="utf-8")
    for forbidden in ("postgresql://", "redis://", "password", "SECRET="):
        assert forbidden not in raw


def test_build_release_manifest_records_absent_external_components_honestly(
    tmp_path: Path,
) -> None:
    """The SaaS repo, the WooCommerce plugin ZIP and the Salla contract live
    outside this repo. When they are not supplied the manifest says so rather
    than inventing a version."""
    _, manifest = _build_manifest(tmp_path)

    for component in ("saas", "woo_plugin", "salla"):
        assert manifest["external_components"][component]["status"] == "not-supplied"


def test_build_release_manifest_records_supplied_external_components(
    tmp_path: Path,
) -> None:
    _, manifest = _build_manifest(
        tmp_path,
        "--saas-commit",
        "deadbeef",
        "--saas-protocol-range",
        ">=1.2,<2.0",
        "--plugin-zip-sha256",
        "sha256:" + "22" * 32,
        "--plugin-version-matrix",
        "0.9.3:wp>=6.4,woo>=8.5",
        "--salla-contract-version",
        "2026-05-01",
    )

    saas = manifest["external_components"]["saas"]
    assert saas["status"] == "supplied"
    assert saas["commit"] == "deadbeef"
    assert saas["protocol_range"] == ">=1.2,<2.0"
    assert manifest["external_components"]["woo_plugin"]["version_matrix"] == {
        "0.9.3": "wp>=6.4,woo>=8.5"
    }
    assert manifest["external_components"]["salla"]["contract_version"] == "2026-05-01"


def test_build_release_manifest_emits_the_bakeable_identity_file(tmp_path: Path) -> None:
    out = tmp_path / "release_manifest.json"
    identity_out = tmp_path / "release_identity.json"
    result = _run_script(
        "build_release_manifest.py",
        "--out",
        str(out),
        "--image-digest",
        "api=sha256:" + "11" * 32,
        "--emit-identity",
        str(identity_out),
    )
    assert result.returncode == 0, result.stderr

    manifest = json.loads(out.read_text(encoding="utf-8"))
    identity = json.loads(identity_out.read_text(encoding="utf-8"))

    assert identity["manifest_id"] == manifest["manifest_id"]
    assert identity["source_digest"] == manifest["source"]["digest"]
    assert set(identity) == {
        "manifest_id",
        "source_digest",
        "image_digest",
        "config_schema_version",
        "expected_db_migration",
    }


def test_emitted_identity_file_is_what_get_release_identity_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: the builder's output is exactly the shape the running API
    reads back — no hand-written glue in between."""
    out = tmp_path / "release_manifest.json"
    identity_out = tmp_path / "release_identity.json"
    result = _run_script(
        "build_release_manifest.py",
        "--out",
        str(out),
        "--image-digest",
        "api=sha256:" + "11" * 32,
        "--emit-identity",
        str(identity_out),
    )
    assert result.returncode == 0, result.stderr

    monkeypatch.setenv(release_mod.RELEASE_IDENTITY_PATH_ENV, str(identity_out))
    release_mod.reset_release_identity_cache()
    identity = release_mod.get_release_identity()

    assert identity.identity_source == "baked"
    assert identity.manifest_id == json.loads(out.read_text(encoding="utf-8"))["manifest_id"]


def test_build_deployment_attestation_links_to_the_manifest_by_digest(
    tmp_path: Path,
) -> None:
    manifest_path, manifest = _build_manifest(tmp_path)
    attestation_path = tmp_path / "deployment_attestation.json"

    result = _run_script(
        "build_deployment_attestation.py",
        "--build-manifest",
        str(manifest_path),
        "--out",
        str(attestation_path),
        "--environment",
        "staging",
        "--backup-id",
        "backup-2026-08-25-01",
        "--approver",
        "owner",
        "--live-migration-head",
        "codehead123",
        "--smoke",
        "version_endpoint=pass",
        "--smoke",
        "ready_endpoint=pass",
        "--rollback-artifact",
        "api=sha256:" + "99" * 32,
    )
    assert result.returncode == 0, result.stderr

    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    assert attestation["build_manifest"]["manifest_id"] == manifest["manifest_id"]
    assert attestation["build_manifest"]["sha256"].startswith("sha256:")
    assert attestation["environment"] == "staging"
    assert attestation["backup_id"] == "backup-2026-08-25-01"
    assert attestation["approver"] == "owner"
    assert attestation["deployed_at"]
    assert attestation["live_db_migration"] == "codehead123"
    assert attestation["smoke"] == {"version_endpoint": "pass", "ready_endpoint": "pass"}
    assert attestation["rollback"]["artifacts"] == {"api": "sha256:" + "99" * 32}


def test_deployment_attestation_never_mutates_the_signed_build_manifest(
    tmp_path: Path,
) -> None:
    manifest_path, _ = _build_manifest(tmp_path)
    before = manifest_path.read_bytes()

    result = _run_script(
        "build_deployment_attestation.py",
        "--build-manifest",
        str(manifest_path),
        "--out",
        str(tmp_path / "att.json"),
        "--environment",
        "staging",
        "--backup-id",
        "backup-1",
        "--approver",
        "owner",
        "--live-migration-head",
        "codehead123",
    )
    assert result.returncode == 0, result.stderr
    assert manifest_path.read_bytes() == before


def test_deployment_attestation_requires_a_backup_id(tmp_path: Path) -> None:
    """A4's backup precedes every migration; an attestation without one is
    not evidence of a safe deploy."""
    manifest_path, _ = _build_manifest(tmp_path)

    result = _run_script(
        "build_deployment_attestation.py",
        "--build-manifest",
        str(manifest_path),
        "--out",
        str(tmp_path / "att.json"),
        "--environment",
        "staging",
        "--approver",
        "owner",
        "--live-migration-head",
        "codehead123",
    )

    assert result.returncode != 0


def test_deployment_attestation_refuses_a_tampered_manifest(tmp_path: Path) -> None:
    manifest_path, manifest = _build_manifest(tmp_path)
    manifest["environment_note"] = "tampered"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_script(
        "build_deployment_attestation.py",
        "--build-manifest",
        str(manifest_path),
        "--out",
        str(tmp_path / "att.json"),
        "--environment",
        "staging",
        "--backup-id",
        "backup-1",
        "--approver",
        "owner",
        "--live-migration-head",
        "codehead123",
    )

    assert result.returncode != 0
    assert "self-hash" in (result.stderr + result.stdout).lower()


def test_both_records_document_the_gpg_signing_mechanism(tmp_path: Path) -> None:
    """No throwaway keys are ever generated. When no signing key is configured
    both records state the mechanism and the exact operator command."""
    manifest_path, manifest = _build_manifest(tmp_path)

    assert manifest["signing"]["gpg"]["status"] in {"signed", "unsigned"}
    if manifest["signing"]["gpg"]["status"] == "unsigned":
        assert "gpg --detach-sign" in manifest["signing"]["gpg"]["operator_command"]
