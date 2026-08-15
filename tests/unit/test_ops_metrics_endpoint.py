"""`GET /ops/metrics` — auth posture, payload shape, and leak guards.

Audit ref: `CORE_PRODUCT_PRODUCTION_READINESS_AUDIT_2026-08-15.md` §H5.

The endpoint is a *cross-workspace* ops surface, so the two things that
matter most here are (a) it is fail-closed behind the same static
service token as `/v1/admin/*`, unlike the deliberately unauthenticated
`/health` and `/version`, and (b) it leaks neither secrets nor any
tenant's row data.

The router's DB dependency is overridden with a tiny fake rather than a
real engine, exactly as `tests/unit/test_version_endpoint.py` does.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app_shared.opsmetrics import rules as rules_mod
from app_shared.opsmetrics.snapshot import (
    BreakerHealth,
    DatabaseRoleHealth,
    DomainDiscovery,
    DomainStats,
    Freshness,
    OpsSnapshot,
    OutboxHealth,
    PartitionHealth,
    QueueHealth,
    RedisHealth,
    RollupHealth,
    SpendVelocity,
)

from app.main import app
from app.routers import ops_metrics

TOKEN = "ops-service-token-for-tests"  # noqa: S105 - fixture value, not a credential
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)

#: A production-shaped snapshot: this is (approximately) what the live
#: database actually returned on 2026-08-15, so the endpoint tests assert
#: against a real posture rather than an invented one.
PRODUCTION_SHAPED = OpsSnapshot(
    collected_at=NOW,
    partitions=(
        PartitionHealth(
            table="request_attempts",
            exists=True,
            current_month_present=True,
            next_month_present=False,
            partition_count=2,
            days_until_next_month=16,
        ),
    ),
    rollups=RollupHealth(
        available=True, rows=0, observation_rows=32_231, max_observation_at=NOW
    ),
    outbox=OutboxHealth(
        available=False,
        unavailable_reason='UndefinedTable: relation "outbox_messages" does not exist',
    ),
    breaker=BreakerHealth(available=False, unavailable_reason="UndefinedTable"),
    queue=QueueHealth(
        available=True,
        targets_by_status={"DEFERRED": 16, "COMPLETED": 9_670},
        targets_deferred=16,
        oldest_deferred_target_age_seconds=3_032_369.0,
    ),
    domains_24h=(
        DomainStats(
            domain="amazon.sa",
            attempts=10_449,
            successes=5_399,
            distinct_urls=1_238,
            proxied=9_555,
            failed_paid=4_156,
        ),
    ),
    discovery=(DomainDiscovery(domain="extra.com", runs_24h=7_186, runs_7d=9_157),),
    spend=SpendVelocity(
        available=True,
        proxied_month_to_date=20_900,
        proxied_24h=0,
        proxied_prev_24h=0,
        seconds_remaining_in_month=16 * 86_400.0,
        ceiling_proxied_requests=250_000,
    ),
    freshness=Freshness(
        available=True,
        last_attempt_at=NOW - timedelta(hours=53),
        seconds_since_attempt=53 * 3_600.0,
        refresh_rules_total=0,
        refresh_rules_enabled=0,
    ),
    redis=RedisHealth(available=True, policy_status="COMPLIANT", policy="noeviction"),
    db_role=DatabaseRoleHealth(
        available=True, role="postgres", is_superuser=True, bypasses_rls=False
    ),
)


class _FakeSession:
    """Never used — `collect_snapshot` is stubbed out in these tests."""


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    class _Settings:
        SAAS_SERVICE_TOKEN = TOKEN
        PROXY_BREAKER_MONTHLY_PROXIED_REQUESTS = 250_000

    monkeypatch.setattr("app.service_auth.get_settings", lambda: _Settings())
    monkeypatch.setattr(ops_metrics, "get_settings", lambda: _Settings())
    monkeypatch.setattr(ops_metrics, "_get_redis", lambda: None)
    monkeypatch.setattr(
        ops_metrics, "collect_snapshot", lambda *a, **k: PRODUCTION_SHAPED
    )

    def _fake_session() -> Iterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[ops_metrics._get_ops_session] = _fake_session
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


# --------------------------------------------------------------------------
# Auth posture
# --------------------------------------------------------------------------


class TestAuth:
    def test_unauthenticated_request_is_rejected(self, client: TestClient) -> None:
        """Unlike `/health` and `/version`, this surface reports
        fleet-wide spend and competitor volumes across every workspace."""
        assert client.get("/ops/metrics").status_code == 401

    def test_wrong_token_is_rejected(self, client: TestClient) -> None:
        response = client.get(
            "/ops/metrics", headers={"Authorization": "Bearer nope"}
        )
        assert response.status_code == 401

    def test_rejection_does_not_reveal_whether_a_token_is_configured(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        unconfigured = client.get("/ops/metrics").json()

        class _NoToken:
            SAAS_SERVICE_TOKEN = None

        monkeypatch.setattr("app.service_auth.get_settings", lambda: _NoToken())
        assert client.get("/ops/metrics", headers=auth()).json() == unconfigured

    def test_fails_closed_when_no_service_token_is_configured(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _NoToken:
            SAAS_SERVICE_TOKEN = ""

        monkeypatch.setattr("app.service_auth.get_settings", lambda: _NoToken())
        assert client.get("/ops/metrics", headers=auth()).status_code == 401

    def test_authenticated_request_succeeds(self, client: TestClient) -> None:
        assert client.get("/ops/metrics", headers=auth()).status_code == 200


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------


class TestPayload:
    def test_reports_critical_alerts_but_still_returns_200(
        self, client: TestClient
    ) -> None:
        """A non-200 would conflate 'the monitor is broken' with 'the
        system is unhealthy' and break any platform health check."""
        response = client.get("/ops/metrics", headers=auth())
        assert response.status_code == 200
        body = response.json()
        assert body["worst_severity"] == "CRITICAL"
        assert body["alert_count"] == len(body["alerts"])
        assert body["alert_count"] > 0

    def test_surfaces_the_production_failures(self, client: TestClient) -> None:
        body = client.get("/ops/metrics", headers=auth()).json()
        fired = {a["rule_id"] for a in body["alerts"]}
        assert "partition.next_month_missing" in fired
        assert "rollup.never_run" in fired
        assert "discovery.runs_per_domain_per_day" in fired
        assert "security.rls_inert" in fired
        assert "freshness.pipeline_silent" in fired

    def test_body_is_strict_json(self, client: TestClient) -> None:
        """`Infinity`/`NaN` tokens would break strict downstream parsers."""
        raw = client.get("/ops/metrics", headers=auth()).text
        json.loads(raw, parse_constant=_reject_constant)

    def test_snapshot_sections_are_present(self, client: TestClient) -> None:
        snapshot = client.get("/ops/metrics", headers=auth()).json()["snapshot"]
        for section in (
            "partitions",
            "rollups",
            "outbox",
            "breaker",
            "queue",
            "domains",
            "discovery",
            "spend",
            "freshness",
            "optimizer",
            "redis",
            "scrapyd",
            "db_role",
        ):
            assert section in snapshot, section

    def test_unavailable_sections_are_reported_not_hidden(
        self, client: TestClient
    ) -> None:
        snapshot = client.get("/ops/metrics", headers=auth()).json()["snapshot"]
        assert snapshot["outbox"]["available"] is False
        assert "outbox_messages" in snapshot["outbox"]["unavailable_reason"]

    def test_alerts_carry_runbook_anchors(self, client: TestClient) -> None:
        alerts = client.get("/ops/metrics", headers=auth()).json()["alerts"]
        assert any(a["runbook"] for a in alerts)

    def test_prometheus_format(self, client: TestClient) -> None:
        response = client.get(
            "/ops/metrics", params={"format": "prometheus"}, headers=auth()
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        body = response.text
        assert "# TYPE crawmatic_partition_next_month_present gauge" in body
        assert 'crawmatic_domain_attempts_24h{domain="amazon.sa"} 10449.0' in body
        assert 'crawmatic_alerts_firing{severity="CRITICAL"}' in body
        assert "crawmatic_rls_effective 0.0" in body
        assert "Infinity" not in body and "NaN" not in body

    def test_prometheus_rejects_an_unknown_format(self, client: TestClient) -> None:
        response = client.get(
            "/ops/metrics", params={"format": "xml"}, headers=auth()
        )
        assert response.status_code == 422

    def test_emit_is_opt_in(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        def ops_records() -> list[str]:
            # Other loggers (e.g. `app.rate_limit`) also write during a
            # request; only this module's stream is under test.
            return [
                json.loads(r.message)["event"]
                for r in caplog.records
                if r.name == "app_shared.opsmetrics"
            ]

        with caplog.at_level("INFO", logger="app_shared.opsmetrics"):
            client.get("/ops/metrics", headers=auth())
        assert ops_records() == []

        caplog.clear()
        with caplog.at_level("INFO", logger="app_shared.opsmetrics"):
            client.get("/ops/metrics", params={"emit": "true"}, headers=auth())
        events = ops_records()
        assert "ops.snapshot" in events
        assert "ops.alert" in events


# --------------------------------------------------------------------------
# Leak guards — this is an ops surface, not a data surface
# --------------------------------------------------------------------------


class TestNoLeaks:
    def _payload(self, client: TestClient) -> str:
        return client.get("/ops/metrics", headers=auth()).text

    def test_no_secrets_in_the_payload(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secrets = (
            "postgresql://",
            "redis://",
            "SAAS_SERVICE_TOKEN",
            TOKEN,
            "password",
            "Bearer ",
            "api_key",
            "secret",
        )
        body = self._payload(client).lower()
        for needle in secrets:
            assert needle.lower() not in body, needle

    def test_no_tenant_row_data(self, client: TestClient) -> None:
        """Aggregates only: no workspace ids, no URLs, no product rows.

        A per-workspace field here would leak one tenant's catalog to any
        operator token holder and, worse, invite a future contributor to
        add a workspace filter and silently under-report fleet spend.
        """
        body = self._payload(client)
        assert "workspace_id" not in body
        assert "product_id" not in body
        assert "match_id" not in body
        assert "https://" not in body

    def test_no_stack_traces(self, client: TestClient) -> None:
        """Collector failures report a class name + message, never a
        traceback (which would name filesystem paths)."""
        body = self._payload(client)
        assert "Traceback" not in body
        assert "/srv/" not in body
        assert ".py\"" not in body

    def test_route_is_absent_from_the_public_openapi_spec(
        self, client: TestClient
    ) -> None:
        spec = client.get("/openapi-public.json").json()
        assert not any(path.startswith("/ops") for path in spec.get("paths", {}))


def _reject_constant(name: str):  # pragma: no cover - only runs on a failure
    raise AssertionError(f"non-JSON constant {name!r} in response body")


def test_rules_module_exposes_stable_severity_names() -> None:
    """Downstream filters key on these strings."""
    assert {str(s) for s in rules_mod.Severity} == {"CRITICAL", "HIGH", "WARNING"}


class TestPrometheusExpositionFormat:
    """Format invariants a strict scraper enforces.

    The first implementation appended samples in collection order, which
    interleaved metric families (a `..._fuse_days` sample landing between
    two `..._next_month_present` samples). That renders fine to a human
    and is rejected by a strict parser as a duplicated family — a silent
    "the metrics look right but nothing ingests them" failure.
    """

    @staticmethod
    def _render() -> str:
        from app_shared.opsmetrics import render_prometheus

        return render_prometheus(PRODUCTION_SHAPED)

    def test_each_metric_family_is_one_contiguous_block(self) -> None:
        seen_complete: set[str] = set()
        current: str | None = None
        for line in self._render().splitlines():
            if line.startswith("#"):
                continue
            name = line.split("{", 1)[0].split(" ", 1)[0]
            if name != current:
                assert name not in seen_complete, f"{name} family is split"
                if current is not None:
                    seen_complete.add(current)
                current = name

    def test_help_and_type_precede_every_family_exactly_once(self) -> None:
        body = self._render()
        names = {
            line.split("{", 1)[0].split(" ", 1)[0]
            for line in body.splitlines()
            if line and not line.startswith("#")
        }
        for name in names:
            assert body.count(f"# HELP {name} ") == 1, name
            assert body.count(f"# TYPE {name} ") == 1, name
            assert body.index(f"# HELP {name} ") < body.index(f"# TYPE {name} ")

    def test_no_none_or_non_numeric_sample_values(self) -> None:
        for line in self._render().splitlines():
            if not line or line.startswith("#"):
                continue
            float(line.rsplit(" ", 1)[1])

    def test_body_ends_with_a_newline(self) -> None:
        assert self._render().endswith("\n")


class TestResilience:
    """The monitor must survive the deployment states it reports on."""

    def test_unconstructable_settings_does_not_500_the_endpoint(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`get_settings()` raises a pydantic ValidationError on a
        misconfigured environment — the exact state this endpoint exists
        to surface. Propagating it would break the monitor precisely
        when the monitored system is broken."""
        def _boom():
            raise RuntimeError("8 validation errors for Settings")

        monkeypatch.setattr(ops_metrics, "get_settings", _boom)
        response = client.get("/ops/metrics", headers=auth())
        assert response.status_code == 200
        assert response.json()["worst_severity"] == "CRITICAL"

    def test_get_redis_returns_none_when_the_client_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Redis counters are a nice-to-have; an unreachable Redis must
        not turn the ops endpoint into a second outage. Calls the real
        wrapper (the fixture stubs it out for the HTTP tests)."""
        import app_shared.redis_client as redis_client

        def _boom():
            raise ConnectionError("redis unreachable")

        monkeypatch.setattr(redis_client, "get_redis_client", _boom)
        # `_get_redis` imports lazily inside its own body, so patching the
        # source module is what it actually resolves at call time.
        assert ops_metrics._get_redis() is None
