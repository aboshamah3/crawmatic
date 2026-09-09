"""EPA D5 (deep dive §12 item 9): the daily cost/freshness scorecard.

Two things this module's acceptance criterion demands evidence for:

1. Every field the plan names is present on the computed row, the ORM
   model, and the durable UPSERT's bind parameters.
2. A metric this task could not measure comes back `None`, never a
   fabricated `0` -- proven by a session that fails every query AND by
   individually zero-valued denominators (a real "no signal" case that
   must not collapse to a division error or a fake ratio).

No live database: every query in `app_shared.maintenance.scorecard` is
exercised against a fake `Session` whose `execute()` dispatches on a
substring of the compiled statement -- the same style
`tests/unit/test_ops_metrics_rules.py`/`test_admin_ops_endpoint.py` use
for a stubbed collection pipeline, adapted here for several independent
statements on one session.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app_shared.maintenance import scorecard as sc
from app_shared.models import FleetDailyScorecard

# The plan's own field list (packet D5 acceptance criterion, verbatim).
PLAN_FIELDS = {
    "provider_bytes",
    "railway_cpu_seconds",
    "railway_ram_gb_hours",
    "railway_egress_gb",
    "valid_fresh_matches",
    "attempts_per_valid_fresh",
    "browser_share",
    "proxied_share",
    "queue_oldest_seconds_p95",
    "persistence_lag_seconds_p95",
    "missing_metric_fraction",
    "budget_reserved_usd",
    "budget_settled_usd",
    "backup_egress_gb",
    "cost_per_valid_fresh_micro_usd",
}


class _Row:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _Result:
    def __init__(self, payload: object) -> None:
        self._payload = payload

    def one(self) -> object:
        return self._payload

    def first(self) -> object:
        return self._payload

    def all(self) -> object:
        return self._payload


class FakeSession:
    """Dispatches `execute(stmt, params)` on a substring of the SQL text.

    `responses` maps a substring unique to one statement to either a
    `_Row`/list-of-`_Row` payload or an `Exception` instance to raise
    (simulating a missing table / locked partition, exactly the
    ``_read_one`` degrade path is built to survive). A substring with no
    match is a hard test failure -- an unrecognised statement means the
    module changed shape without the test noticing.
    """

    def __init__(self, responses: dict[str, object]) -> None:
        self.responses = responses
        self.rollback_count = 0
        self.executed: list[tuple[str, dict]] = []

    def execute(self, stmt: object, params: dict | None = None) -> _Result:
        sql = str(stmt)
        self.executed.append((sql, params or {}))
        for needle, payload in self.responses.items():
            if needle in sql:
                if isinstance(payload, Exception):
                    raise payload
                return _Result(payload)
        raise AssertionError(f"FakeSession: no response registered for SQL: {sql[:120]!r}")

    def rollback(self) -> None:
        self.rollback_count += 1


class FakeRedis:
    """Minimal in-memory stand-in for the redis-py hash + pipeline calls
    `record_backup_report`/`backup_egress_gb_for_day` use."""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = {}

    def pipeline(self) -> "_FakePipeline":
        return _FakePipeline(self)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self.hashes.get(key, {}))


class _FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple] = []

    def hincrby(self, key: str, field: str, amount: int) -> "_FakePipeline":
        self._ops.append(("hincrby", key, field, amount))
        return self

    def hset(self, key: str, field: str, value: str) -> "_FakePipeline":
        self._ops.append(("hset", key, field, value))
        return self

    def expire(self, key: str, ttl: int) -> "_FakePipeline":
        self._ops.append(("expire", key, ttl))
        return self

    def execute(self) -> list[None]:
        for op in self._ops:
            kind, key = op[0], op[1]
            table = self._redis.hashes.setdefault(key, {})
            if kind == "hincrby":
                _, _, field, amount = op
                table[field] = str(int(table.get(field, "0")) + amount)
            elif kind == "hset":
                _, _, field, value = op
                table[field] = value
            # "expire" is a no-op in this fake: TTL correctness is not
            # what these tests are proving.
        self._ops.clear()
        return []


# --- 1. Field-set completeness ---------------------------------------------


def test_scorecard_row_carries_every_plan_field() -> None:
    row = sc.ScorecardRow(date=date(2026, 9, 7))
    assert PLAN_FIELDS <= set(row.as_dict().keys())


def test_orm_model_has_every_plan_field_plus_the_date_key() -> None:
    columns = set(FleetDailyScorecard.__table__.columns.keys())
    assert PLAN_FIELDS <= columns
    assert "date" in columns


def test_upsert_binds_every_plan_field_by_name() -> None:
    row = sc.ScorecardRow(date=date(2026, 9, 7), valid_fresh_matches=3)
    session = FakeSession({"INSERT INTO fleet_daily_scorecard": None})
    sc.upsert_scorecard(session, row)
    assert len(session.executed) == 1
    _, params = session.executed[0]
    assert PLAN_FIELDS <= set(params.keys())
    assert params["date"] == date(2026, 9, 7)
    assert params["valid_fresh_matches"] == 3


# --- 2. NULL, never 0, for an unmeasured input ------------------------------


def _all_queries_fail_session() -> FakeSession:
    """Every statement `compute_scorecard` can issue raises -- the
    "table not migrated yet / partition locked" case every group must
    degrade from independently."""
    boom = RuntimeError("simulated: relation does not exist")
    return FakeSession(
        {
            "match_current_prices": boom,
            "AS attempts": boom,
            "browser_ops": boom,
            "FROM scrape_job_targets": boom,
            "provider_usage_records": boom,
            "reserved_cost_micro_units": boom,
            "settled_cost_micro_units": boom,
        }
    )


def test_every_field_is_none_not_zero_when_every_source_is_unreadable() -> None:
    session = _all_queries_fail_session()
    row = sc.compute_scorecard(session, date(2026, 9, 7), redis_client=None)

    for field in sc.ScorecardRow.MEASURED_FIELDS:
        assert getattr(row, field) is None, f"{field} should be None, got {getattr(row, field)!r}"
    # missing_metric_fraction is ALWAYS computed, and every input was
    # unmeasured -- 100% missing, not itself None.
    assert row.missing_metric_fraction == pytest.approx(1.0)
    # Every failing statement rolled the (poisoned) transaction back
    # before the next group ran, so no group blinded the rest.
    assert session.rollback_count == len(session.responses)


def test_railway_platform_fields_are_always_none_today() -> None:
    """No durable Railway usage-API store exists (module docstring) --
    these three columns must never carry a value, regardless of what
    every other source reports."""
    session = FakeSession(
        {
            "match_current_prices": _Row(valid_fresh_matches=5),
            "AS attempts": _Row(attempts=10),
            "browser_ops": _Row(total_ops=10, browser_ops=2, proxied_ops=1),
            "FROM scrape_job_targets": _Row(
                queue_oldest_seconds_p95=1.0, persistence_lag_seconds_p95=2.0
            ),
            "provider_usage_records": _Row(provider_bytes=100),
            "reserved_cost_micro_units": _Row(reserved_micro=1_000_000),
            "settled_cost_micro_units": _Row(settled_micro=500_000),
        }
    )
    row = sc.compute_scorecard(session, date(2026, 9, 7), redis_client=None)
    assert row.railway_cpu_seconds is None
    assert row.railway_ram_gb_hours is None
    assert row.railway_egress_gb is None


def test_zero_denominator_is_none_not_a_fabricated_ratio() -> None:
    """`valid_fresh_matches == 0` is a REAL measurement (nothing refreshed
    that day) -- so it is 0, not None. But a ratio over that zero
    denominator has no answer and must be None, never 0 or a ZeroDivisionError."""
    session = FakeSession(
        {
            "match_current_prices": _Row(valid_fresh_matches=0),
            "AS attempts": _Row(attempts=42),
            "browser_ops": _Row(total_ops=0, browser_ops=0, proxied_ops=0),
            "FROM scrape_job_targets": _Row(
                queue_oldest_seconds_p95=None, persistence_lag_seconds_p95=None
            ),
            "provider_usage_records": _Row(provider_bytes=None),
            "reserved_cost_micro_units": _Row(reserved_micro=None),
            "settled_cost_micro_units": _Row(settled_micro=1_000_000),
        }
    )
    row = sc.compute_scorecard(session, date(2026, 9, 7), redis_client=None)
    assert row.valid_fresh_matches == 0
    assert row.attempts_per_valid_fresh is None
    assert row.browser_share is None
    assert row.proxied_share is None
    assert row.cost_per_valid_fresh_micro_usd is None
    assert row.budget_settled_usd == pytest.approx(1.0)


def test_fully_populated_row_computes_every_ratio_correctly() -> None:
    session = FakeSession(
        {
            "match_current_prices": _Row(valid_fresh_matches=4),
            "AS attempts": _Row(attempts=20),
            "browser_ops": _Row(total_ops=10, browser_ops=3, proxied_ops=2),
            "FROM scrape_job_targets": _Row(
                queue_oldest_seconds_p95=120.5, persistence_lag_seconds_p95=45.0
            ),
            "provider_usage_records": _Row(provider_bytes=1000),
            "reserved_cost_micro_units": _Row(reserved_micro=2_000_000),
            "settled_cost_micro_units": _Row(settled_micro=1_000_000),
        }
    )
    row = sc.compute_scorecard(session, date(2026, 9, 7), redis_client=None)

    assert row.valid_fresh_matches == 4
    assert row.attempts_per_valid_fresh == pytest.approx(5.0)
    assert row.browser_share == pytest.approx(0.3)
    assert row.proxied_share == pytest.approx(0.2)
    assert row.queue_oldest_seconds_p95 == pytest.approx(120.5)
    assert row.persistence_lag_seconds_p95 == pytest.approx(45.0)
    assert row.provider_bytes == 1000
    assert row.budget_reserved_usd == pytest.approx(2.0)
    assert row.budget_settled_usd == pytest.approx(1.0)
    assert row.cost_per_valid_fresh_micro_usd == pytest.approx(250_000.0)
    # backup_egress_gb is the only field with no redis client here.
    assert row.backup_egress_gb is None
    # 4 of the 14 measured fields are None: the three Railway
    # platform-billing columns (always None today, see the module
    # docstring) plus backup_egress_gb (no redis_client passed here).
    missing = 4
    assert row.missing_metric_fraction == pytest.approx(
        missing / len(sc.ScorecardRow.MEASURED_FIELDS)
    )


# --- 3. The C10 backup-report side channel ----------------------------------


def test_record_backup_report_rejects_an_unrecognised_schema() -> None:
    redis = FakeRedis()
    accepted = sc.record_backup_report(redis, {"schema": "some.other.v2"})
    assert accepted is False
    assert redis.hashes == {}


def test_record_backup_report_folds_private_and_public_legs_by_day() -> None:
    redis = FakeRedis()
    day = date(2026, 9, 7)
    private_payload = {
        "schema": sc.BACKUP_REPORT_SCHEMA,
        "backup_set": "set-20260907T040000Z",
        "created_utc": "2026-09-07T04:00:00Z",
        "private_network": True,
        "totals": {"bytes_on_wire": 5_000_000_000},
    }
    public_payload = {
        "schema": sc.BACKUP_REPORT_SCHEMA,
        "backup_set": "set-20260907T080000Z",
        "created_utc": "2026-09-07T08:00:00Z",
        "private_network": False,
        "totals": {"bytes_on_wire": 2_000_000_000},
    }
    assert sc.record_backup_report(redis, private_payload) is True
    assert sc.record_backup_report(redis, public_payload) is True

    egress_gb = sc.backup_egress_gb_for_day(redis, day)
    # Only the PUBLIC leg's bytes count as egress -- the private leg
    # contributed 0 by construction (that is the entire point of C10).
    assert egress_gb == pytest.approx(2.0)


def test_backup_egress_is_zero_not_none_when_every_leg_was_private() -> None:
    """A day where reports arrived and EVERY one was private-network is a
    genuine, measured zero -- the C10 success case -- not a missing
    input."""
    redis = FakeRedis()
    day = date(2026, 9, 7)
    payload = {
        "schema": sc.BACKUP_REPORT_SCHEMA,
        "backup_set": "set-20260907T040000Z",
        "created_utc": "2026-09-07T04:00:00Z",
        "private_network": True,
        "totals": {"bytes_on_wire": 5_000_000_000},
    }
    assert sc.record_backup_report(redis, payload) is True
    assert sc.backup_egress_gb_for_day(redis, day) == pytest.approx(0.0)


def test_backup_egress_is_none_when_no_report_was_ever_received() -> None:
    redis = FakeRedis()
    assert sc.backup_egress_gb_for_day(redis, date(2026, 9, 7)) is None


def test_backup_egress_is_none_when_redis_is_unavailable() -> None:
    assert sc.backup_egress_gb_for_day(None, date(2026, 9, 7)) is None


def test_compute_scorecard_reads_backup_egress_through_redis() -> None:
    redis = FakeRedis()
    day = date(2026, 9, 7)
    sc.record_backup_report(
        redis,
        {
            "schema": sc.BACKUP_REPORT_SCHEMA,
            "backup_set": "set-20260907T080000Z",
            "created_utc": "2026-09-07T08:00:00Z",
            "private_network": False,
            "totals": {"bytes_on_wire": 3_000_000_000},
        },
    )
    session = _all_queries_fail_session()
    row = sc.compute_scorecard(session, day, redis_client=redis)
    assert row.backup_egress_gb == pytest.approx(3.0)


# --- 4. run_daily_scorecard: default day, idempotent write ------------------


def test_run_daily_scorecard_defaults_to_yesterday_utc() -> None:
    session = _all_queries_fail_session()
    now = datetime(2026, 9, 8, 3, 0, tzinfo=timezone.utc)
    session.responses["INSERT INTO fleet_daily_scorecard"] = None
    report = sc.run_daily_scorecard(session, now=now, redis_client=None)
    assert report.date == date(2026, 9, 7)
    assert report.row.date == date(2026, 9, 7)


def test_run_daily_scorecard_honours_an_explicit_day() -> None:
    session = _all_queries_fail_session()
    session.responses["INSERT INTO fleet_daily_scorecard"] = None
    report = sc.run_daily_scorecard(
        session, day=date(2026, 1, 1), now=datetime.now(timezone.utc), redis_client=None
    )
    assert report.date == date(2026, 1, 1)
