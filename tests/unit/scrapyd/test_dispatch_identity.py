"""Durable dispatch intent + canonical identity (EPA B1, READY-002, P0.4A).

The bug this file exists to make impossible: the dispatch idempotency key
used to be **positional** — ``dispatched:{scrape_job_id}:{batch_index}``.
``batch_index`` is the enumerated position of a chunk in a *re-derivable*
plan, so the moment the planner re-plans a different set of targets (a
partially-finished job, a strategy fallback, a stall recovery) the SAME
key stands for DIFFERENT work. Two consequences, both observed in
production:

* a replanned **subset** aliases the full batch's committed key, so the
  client returns the old jobid as a "no-op" and the subset is *never*
  dispatched (targets wedge forever); and
* the guard's TTL was the only thing that ever released it, so batch
  identity silently changed meaning every 900 s.

B1 replaces the position with a **canonical identity** — job, durable
planning generation, strategy method, domain, mode, node class, and a
digest over the actual work (the sorted ``match_ids``) — and backs the
Redis guard with a durable ``dispatch_intents`` row so that:

1. the same work in a different *order* is the same identity;
2. a different *subset* is never the same identity;
3. a different *mode* is never the same identity;
4. a new *planning generation* is never the same identity;
5. a Celery **replay** reuses the persisted generation (one POST), while
6. a task **retry** never mints a new generation;
7. a **failed** POST releases the Redis claim *and* records a durable
   ``FAILED`` state; and
8. ``get_committed_dispatch`` refuses to hand back a jobid whose stored
   ``identity_payload`` does not equal the identity being asked about.

Two of these (5, 6) are asserted against the **REAL planner path**
(``apps/workers/app/workers/tasks_jobs.py::dispatch_job``) rather than a
hand-built identity, because "the generation is durable" is only true if
the planner is what persists it. Those run in a fresh subprocess for the
same reason ``tests/unit/test_jobs_dispatch_task.py`` does: ``apps/api``
and ``apps/workers`` each ship a top-level ``app`` package, and
``celery_app.py`` calls ``get_settings()`` at import time.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import requests

from app_shared.enums import DispatchIntentState, ScrapeProfileMode
from app_shared.scrapyd.client import ScrapydDispatchClient
from app_shared.scrapyd.errors import (
    ScrapydDispatchError,
    StaleCancellationGenerationError,
)
from app_shared.scrapyd.identity import (
    PENDING_SENTINEL,
    CommittedDispatch,
    DispatchIdentity,
    build_dispatch_identity,
    encode_guard_value,
    get_committed_dispatch,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

JOB_ID = "22222222-2222-2222-2222-222222222222"
MATCH_A = "33333333-3333-3333-3333-333333333333"
MATCH_B = "44444444-4444-4444-4444-444444444444"
MATCH_C = "55555555-5555-5555-5555-555555555555"


def _identity(**overrides: Any) -> DispatchIdentity:
    kwargs: dict[str, Any] = {
        "scrape_job_id": JOB_ID,
        "planning_generation": 1,
        "strategy_method": "HTTP_CLIENT/JSONLD/v1",
        "domain": "shop.example.com",
        "mode": "HTTP",
        "node_class": "price_monitor:generic_price_spider",
        "match_ids": [MATCH_A, MATCH_B],
    }
    kwargs.update(overrides)
    return build_dispatch_identity(**kwargs)


# ---------------------------------------------------------------------------
# 1-4: what may and may not alias
# ---------------------------------------------------------------------------


def test_identity_is_order_insensitive_over_match_ids() -> None:
    """The same work in a different order is the SAME dispatch."""
    forward = _identity(match_ids=[MATCH_A, MATCH_B, MATCH_C])
    shuffled = _identity(match_ids=[MATCH_C, MATCH_A, MATCH_B])

    assert forward.work_digest == shuffled.work_digest
    assert forward.canonical_payload == shuffled.canonical_payload
    assert forward.key == shuffled.key


def test_identity_is_insensitive_to_match_id_duplicates_and_types() -> None:
    """``uuid.UUID`` and its string spelling are the same match id."""
    as_text = _identity(match_ids=[MATCH_A, MATCH_B])
    as_uuid = _identity(match_ids=[uuid.UUID(MATCH_B), uuid.UUID(MATCH_A)])

    assert as_text.key == as_uuid.key


def test_replanned_fallback_subset_never_aliases_the_full_batch() -> None:
    """THE P0.4A REGRESSION: a subset must not inherit the batch's key.

    Positionally these are both ``batch_index=0``; that identity is what
    made the client answer a genuinely-new subset dispatch with the old
    batch's jobid and never POST it.
    """
    full = _identity(match_ids=[MATCH_A, MATCH_B, MATCH_C])
    subset = _identity(match_ids=[MATCH_A])

    assert full.work_digest != subset.work_digest
    assert full.key != subset.key


def test_cross_mode_never_aliases() -> None:
    http = _identity(mode="HTTP")
    browser = _identity(mode="BROWSER")

    assert http.key != browser.key
    assert http.canonical_payload != browser.canonical_payload


def test_new_planning_generation_never_aliases() -> None:
    first = _identity(planning_generation=1)
    second = _identity(planning_generation=2)

    assert first.key != second.key


def test_distinct_strategy_method_domain_and_node_class_never_alias() -> None:
    base = _identity()
    assert _identity(strategy_method="BROWSER_RENDER/CSS/v2").key != base.key
    assert _identity(domain="other.example.com").key != base.key
    assert _identity(node_class="price_monitor_browser:generic_browser_spider").key != base.key


def test_canonical_payload_and_key_shape_are_the_contract() -> None:
    identity = _identity()

    assert identity.canonical_payload == "|".join(
        [
            JOB_ID,
            "1",
            "HTTP_CLIENT/JSONLD/v1",
            "shop.example.com",
            "HTTP",
            "price_monitor:generic_price_spider",
            identity.work_digest,
        ]
    )
    assert len(identity.work_digest) == 16
    prefix, job, digest = identity.key.split(":")
    assert (prefix, job) == ("dispatched", JOB_ID)
    assert len(digest) == 32


def test_batch_index_is_not_part_of_the_identity() -> None:
    """``batch_index`` is a spider arg — never an idempotency input."""
    assert "batch_index" not in DispatchIdentity.__dataclass_fields__


# ---------------------------------------------------------------------------
# test doubles for the client-level tests
# ---------------------------------------------------------------------------


@dataclass
class _FakeSettings:
    SCRAPYD_HTTP_URLS: list[str] = field(
        default_factory=lambda: ["http://scrapers:6800"]
    )
    SCRAPYD_USERNAME: str = "scrapyd"
    SCRAPYD_PASSWORD: str = "correct-horse"
    SCRAPYD_DISPATCH_GUARD_TTL_SECONDS: int = 900


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int | None] = {}

    def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and name in self.store:
            return None
        self.store[name] = value
        self.ttls[name] = ex
        return True

    def get(self, name: str) -> str | None:
        return self.store.get(name)

    def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            if self.store.pop(name, None) is not None:
                removed += 1
        return removed


class _FakeScrapyd:
    def __init__(self, *, jobid: str = "jobid-abc123", raise_exc: Exception | None = None):
        self._jobid = jobid
        self._raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, *, data: dict[str, Any], auth: Any, timeout: float) -> Any:
        self.calls.append({"url": url, "data": dict(data), "auth": auth})
        if self._raise_exc is not None:
            raise self._raise_exc

        class _Response:
            status_code = 200

            @staticmethod
            def json() -> dict[str, Any]:
                return {"status": "ok", "jobid": self._jobid}

        response = _Response()
        response.json = lambda: {"status": "ok", "jobid": self._jobid}  # type: ignore[method-assign]
        return response


class _FakeAuthority:
    """In-memory stand-in for the durable ``dispatch_intents`` authority.

    Mirrors ``app_shared.jobs.dispatch_intents.DispatchIntentStore``'s
    surface without a database: the point of the client-level tests is
    the claim/commit/release *ordering*, not SQL.
    """

    def __init__(self, *, cancellation_generation: int = 0) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.cancellation_generation = cancellation_generation
        self.job_cancellation_generation = cancellation_generation
        self.events: list[str] = []

    def _row(self, identity: DispatchIdentity) -> dict[str, Any]:
        return self.rows.setdefault(
            identity.key,
            {
                "intent_id": str(uuid.uuid4()),
                "identity_payload": identity.canonical_payload,
                "state": DispatchIntentState.PLANNED.value,
                "scrapyd_job_id": None,
                "cancellation_generation_at_creation": self.cancellation_generation,
                "committed_at": None,
            },
        )

    def plan(self, identity: DispatchIdentity) -> dict[str, Any]:
        self.events.append("plan")
        return self._row(identity)

    def reconcile(self, identity: DispatchIdentity) -> CommittedDispatch | None:
        self.events.append("reconcile")
        row = self.rows.get(identity.key)
        authorized = (
            row["cancellation_generation_at_creation"]
            if row is not None
            else self.cancellation_generation
        )
        if authorized != self.job_cancellation_generation:
            raise StaleCancellationGenerationError(
                f"dispatch intent authorized under cancellation generation "
                f"{authorized}, job is now at {self.job_cancellation_generation}"
            )
        if row is None or row["state"] != DispatchIntentState.CONFIRMED.value:
            return None
        return CommittedDispatch(
            jobid=row["scrapyd_job_id"],
            identity_payload=row["identity_payload"],
            intent_id=row["intent_id"],
            committed_at=row["committed_at"],
            source="dispatch_intents",
        )

    def record_post(self, identity: DispatchIdentity) -> str:
        self.events.append("record_post")
        row = self._row(identity)
        row["state"] = DispatchIntentState.POSTED.value
        return str(row["intent_id"])

    def confirm(self, identity: DispatchIdentity, scrapyd_job_id: str) -> None:
        self.events.append("confirm")
        row = self._row(identity)
        row["state"] = DispatchIntentState.CONFIRMED.value
        row["scrapyd_job_id"] = scrapyd_job_id
        row["committed_at"] = "2026-08-25T00:00:00+00:00"

    def fail(self, identity: DispatchIdentity, error: str) -> None:
        self.events.append("fail")
        row = self._row(identity)
        row["state"] = DispatchIntentState.FAILED.value
        row["error"] = error


def _client(
    scrapyd: _FakeScrapyd,
    redis: _FakeRedis,
    authority: _FakeAuthority | None = None,
) -> ScrapydDispatchClient:
    session = requests.Session()
    session.post = scrapyd.post  # type: ignore[assignment]
    return ScrapydDispatchClient(
        settings=_FakeSettings(),  # type: ignore[arg-type]
        redis_client=redis,
        session=session,
        intents=authority,
    )


_SCHEDULE_ARGS = {
    "workspace_id": "11111111-1111-1111-1111-111111111111",
    "scrape_job_id": JOB_ID,
    "mode": "HTTP",
    "batch_index": 0,
}


# ---------------------------------------------------------------------------
# 7-8 + guard mechanics
# ---------------------------------------------------------------------------


def test_schedule_commits_identity_scoped_guard_and_confirms_the_intent() -> None:
    identity = _identity()
    scrapyd, redis, authority = _FakeScrapyd(jobid="job-777"), _FakeRedis(), _FakeAuthority()

    jobid = _client(scrapyd, redis, authority).schedule(
        "price_monitor",
        "generic_price_spider",
        match_ids=[MATCH_A, MATCH_B],
        identity=identity,
        **_SCHEDULE_ARGS,
    )

    assert jobid == "job-777"
    assert len(scrapyd.calls) == 1
    # The guard lives under the IDENTITY key, not `dispatched:{job}:0`.
    assert identity.key in redis.store
    assert f"dispatched:{JOB_ID}:0" not in redis.store
    guard = json.loads(redis.store[identity.key])
    assert guard["jobid"] == "job-777"
    assert guard["identity_payload"] == identity.canonical_payload
    assert guard["intent_id"]
    assert guard["committed_at"]
    # Durable state followed the POST, and the reconcile ran BEFORE the claim.
    assert authority.rows[identity.key]["state"] == DispatchIntentState.CONFIRMED.value
    assert authority.rows[identity.key]["scrapyd_job_id"] == "job-777"
    assert authority.events[0] == "reconcile"
    assert authority.events.index("record_post") < authority.events.index("confirm")


def test_duplicate_delivery_of_the_same_identity_does_not_re_post() -> None:
    identity = _identity()
    scrapyd, redis, authority = _FakeScrapyd(jobid="job-777"), _FakeRedis(), _FakeAuthority()
    client = _client(scrapyd, redis, authority)

    first = client.schedule(
        "price_monitor", "generic_price_spider",
        match_ids=[MATCH_A, MATCH_B], identity=identity, **_SCHEDULE_ARGS,
    )
    second = client.schedule(
        "price_monitor", "generic_price_spider",
        match_ids=[MATCH_B, MATCH_A], identity=_identity(match_ids=[MATCH_B, MATCH_A]),
        **_SCHEDULE_ARGS,
    )

    assert first == second == "job-777"
    assert len(scrapyd.calls) == 1


def test_replanned_subset_is_posted_even_though_the_full_batch_committed() -> None:
    """The wedge, at the client level: a subset must still be dispatched."""
    full = _identity(match_ids=[MATCH_A, MATCH_B, MATCH_C])
    subset = _identity(match_ids=[MATCH_A])
    scrapyd, redis, authority = _FakeScrapyd(), _FakeRedis(), _FakeAuthority()
    client = _client(scrapyd, redis, authority)

    client.schedule(
        "price_monitor", "generic_price_spider",
        match_ids=[MATCH_A, MATCH_B, MATCH_C], identity=full, **_SCHEDULE_ARGS,
    )
    client.schedule(
        "price_monitor", "generic_price_spider",
        match_ids=[MATCH_A], identity=subset, **_SCHEDULE_ARGS,
    )

    assert len(scrapyd.calls) == 2


def test_failed_post_releases_the_claim_and_records_durable_failed() -> None:
    identity = _identity()
    scrapyd = _FakeScrapyd(raise_exc=requests.ConnectionError("node down"))
    redis, authority = _FakeRedis(), _FakeAuthority()

    with pytest.raises(requests.ConnectionError):
        _client(scrapyd, redis, authority).schedule(
            "price_monitor", "generic_price_spider",
            match_ids=[MATCH_A, MATCH_B], identity=identity, **_SCHEDULE_ARGS,
        )

    # release: no poisoned key survives a failed attempt.
    assert identity.key not in redis.store
    # ...but the ATTEMPT is durable, so a reader can tell "never tried"
    # from "tried and failed".
    assert authority.rows[identity.key]["state"] == DispatchIntentState.FAILED.value
    assert "fail" in authority.events


def test_expired_sentinel_alone_does_not_authorize_a_re_post() -> None:
    """Sentinel expiry is not evidence that no POST happened.

    The durable intent is reconciled BEFORE the Redis claim, so a guard
    that has aged out of Redis while the intent says ``CONFIRMED`` still
    resolves to the committed jobid — and the guard is healed on the way
    out rather than re-POSTed.
    """
    identity = _identity()
    scrapyd, redis, authority = _FakeScrapyd(jobid="job-777"), _FakeRedis(), _FakeAuthority()
    client = _client(scrapyd, redis, authority)

    client.schedule(
        "price_monitor", "generic_price_spider",
        match_ids=[MATCH_A, MATCH_B], identity=identity, **_SCHEDULE_ARGS,
    )
    redis.store.clear()  # the guard's TTL elapses
    redis.ttls.clear()

    again = client.schedule(
        "price_monitor", "generic_price_spider",
        match_ids=[MATCH_A, MATCH_B], identity=identity, **_SCHEDULE_ARGS,
    )

    assert again == "job-777"
    assert len(scrapyd.calls) == 1, "an expired sentinel re-POSTed committed work"
    assert identity.key in redis.store, "the guard was not healed from the durable intent"


def test_concurrent_in_flight_sentinel_is_not_double_posted() -> None:
    identity = _identity()
    scrapyd, redis, authority = _FakeScrapyd(), _FakeRedis(), _FakeAuthority()
    redis.store[identity.key] = PENDING_SENTINEL

    with pytest.raises(ScrapydDispatchError, match="already in progress"):
        _client(scrapyd, redis, authority).schedule(
            "price_monitor", "generic_price_spider",
            match_ids=[MATCH_A, MATCH_B], identity=identity, **_SCHEDULE_ARGS,
        )

    assert scrapyd.calls == []


def test_stale_cancellation_generation_is_refused_at_dispatch_time() -> None:
    """A2's fence, wired: work authorized before a cancellation never POSTs."""
    identity = _identity()
    scrapyd, redis = _FakeScrapyd(), _FakeRedis()
    authority = _FakeAuthority(cancellation_generation=0)
    authority.job_cancellation_generation = 1  # the job was cancelled meanwhile

    with pytest.raises(StaleCancellationGenerationError):
        _client(scrapyd, redis, authority).schedule(
            "price_monitor", "generic_price_spider",
            match_ids=[MATCH_A, MATCH_B], identity=identity, **_SCHEDULE_ARGS,
        )

    assert scrapyd.calls == []
    assert identity.key not in redis.store


# ---------------------------------------------------------------------------
# 8: get_committed_dispatch
# ---------------------------------------------------------------------------


def test_get_committed_dispatch_returns_the_record_on_an_exact_payload_match() -> None:
    identity = _identity()
    redis = _FakeRedis()
    redis.store[identity.key] = encode_guard_value(
        identity, jobid="job-777", intent_id="intent-1", committed_at="2026-08-25T00:00:00+00:00"
    )

    committed = get_committed_dispatch(redis, identity)

    assert committed is not None
    assert committed.jobid == "job-777"
    assert committed.identity_payload == identity.canonical_payload
    assert committed.intent_id == "intent-1"
    assert committed.source == "redis"


def test_get_committed_dispatch_rejects_an_identity_mismatch() -> None:
    """A guard value that does not describe THIS identity is not an answer."""
    stored = _identity(match_ids=[MATCH_A, MATCH_B, MATCH_C])
    asked = _identity(match_ids=[MATCH_A])
    redis = _FakeRedis()
    # Force the mismatch under the *asked* key — the shape a legacy
    # positional guard, or a corrupted/aliased key, would have.
    redis.store[asked.key] = encode_guard_value(
        stored, jobid="job-777", intent_id="intent-1", committed_at="2026-08-25T00:00:00+00:00"
    )

    assert get_committed_dispatch(redis, asked) is None


def test_get_committed_dispatch_ignores_the_pending_sentinel_and_legacy_values() -> None:
    identity = _identity()
    redis = _FakeRedis()

    redis.store[identity.key] = PENDING_SENTINEL
    assert get_committed_dispatch(redis, identity) is None

    redis.store[identity.key] = "job-legacy-plain-string"
    assert get_committed_dispatch(redis, identity) is None

    redis.store.pop(identity.key)
    assert get_committed_dispatch(redis, identity) is None


def test_get_committed_dispatch_falls_back_to_the_durable_intent() -> None:
    """Redis absent/expired -> ``dispatch_intents`` is the authority."""
    identity = _identity()
    authority = _FakeAuthority()
    authority.record_post(identity)
    authority.confirm(identity, "job-777")

    committed = get_committed_dispatch([_FakeRedis(), authority], identity)

    assert committed is not None
    assert committed.jobid == "job-777"
    assert committed.source == "dispatch_intents"


# ---------------------------------------------------------------------------
# 5-6: the REAL planner path (subprocess — see the module docstring)
# ---------------------------------------------------------------------------

_PLANNER_HARNESS = (Path(__file__).parent / "_planner_replay_harness.py").read_text(
    encoding="utf-8"
)

#: `celery_app.py` calls `get_settings()` at import time, so the harness
#: needs a complete, self-contained env — same list as
#: `tests/unit/test_jobs_dispatch_task.py::_DISPATCH_TASK_ENV`. None of
#: these hosts are contacted: every I/O seam is faked inside the harness.
_PLANNER_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def _run_planner_scenario(scenario: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _PLANNER_HARNESS, scenario],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, **_PLANNER_ENV},
    )


@pytest.mark.parametrize(
    "scenario",
    [
        "celery_replay_reuses_persisted_generation",
        "task_retry_does_not_mint_a_new_generation",
        "committed_replan_advances_the_generation",
        "intents_are_persisted_by_the_planner",
    ],
)
def test_real_planner_path(scenario: str) -> None:
    result = _run_planner_scenario(scenario)
    assert result.returncode == 0, (
        f"{scenario} failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "OK" in result.stdout
