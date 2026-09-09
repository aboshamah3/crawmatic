"""Unit tests for `app_shared.maintenance.retention` (SPEC-15 T029, US3,
contracts/retention-drop.md, FR-016/017/018/019/020).

Pure, DB-independent:

* `partition_eligible` — eligible only when the WHOLE half-open range is
  `<= cutoff` (FR-018, the boundary partition is deterministic: a
  partition whose `end` is still after cutoff is never eligible, even
  when its `start` already precedes cutoff).
* Per-table retention-window resolution via `retention_days` (obs/
  attempts 90, alerts 365, rollups 730 via the separate rollup-age
  setting — FR-017).
* `feeds_rollups=False` entries never need `rollups_cover` (FR-019) —
  asserted via the registry shape (mirrors `test_partition_registry`'s
  `feeds_rollups` assertion) plus a `run_retention` walk against a fake
  session proving no coverage query is issued for those tables.
* `rollups_cover`'s two gates (EPA C8, F13) — compiled SQL text
  assertions, no live DB (mirrors `test_partition_registry`'s
  `_compiled` helper): the per-key `EXCEPT` (gate 1) and the
  `rollup_completion` watermark gap query (gate 2).
* `run_retention` end-to-end against a minimal in-memory fake `Session`
  (no live DB, no SQLAlchemy engine): drop vs skip-pending-rollups vs
  in-window-untouched vs age-only tables vs the one sanctioned rollup-
  table bulk DELETE, plus idempotent re-run (FR-020).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app_shared.config import Settings
from app_shared.maintenance.partitions import PartitionBounds
from app_shared.maintenance.registry import PARTITIONED_TABLES
from app_shared.maintenance.retention import (
    _completion_watermark_gap_stmt,
    _rollup_age_delete_stmt,
    _rollups_cover_stmt,
    partition_eligible,
    rollups_cover,
    run_retention,
)

REQUIRED_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}


def _set_required_env(monkeypatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def _settings(monkeypatch) -> Settings:
    """Settings with EVERY retention class enabled.

    EPA C9 (F14) made `RETENTION_ENABLED_CLASSES` the owner-ratification
    switch, and it ships EMPTY -- so a default `Settings` now retains
    everything and these tests, which are about drop MECHANICS, would all
    assert on a no-op. The wildcard is what the owner sets after signing
    `docs/RETENTION_POLICY.md`; the default-empty posture has its own
    test (`test_run_retention_drops_nothing_when_no_class_is_enabled`).
    """
    _set_required_env(monkeypatch)
    monkeypatch.setenv("RETENTION_ENABLED_CLASSES", "*")
    return Settings(_env_file=None)


def _settings_observations_90d(monkeypatch) -> Settings:
    """`_settings`, but with the OBSERVATION window pinned back to 90 days.

    EPA C9 raised `RETENTION_PRICE_OBSERVATIONS_DAYS` to 180. The two
    tests below are about the verify-before-drop GATES, not about the
    window, and their fixtures are hand-built around a 90-day cutoff --
    pinning the setting keeps them testing what they were written to test
    instead of quietly turning into "180 days has not elapsed yet".
    """
    settings = _settings(monkeypatch)
    monkeypatch.setenv("RETENTION_PRICE_OBSERVATIONS_DAYS", "90")
    return Settings(_env_file=None)


def _settings_default_switch(monkeypatch) -> Settings:
    """Settings exactly as shipped -- `RETENTION_ENABLED_CLASSES` empty."""
    _set_required_env(monkeypatch)
    monkeypatch.delenv("RETENTION_ENABLED_CLASSES", raising=False)
    return Settings(_env_file=None)


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


# --- partition_eligible: whole-range-past-cutoff, deterministic (FR-018) ----


def test_partition_wholly_before_cutoff_is_eligible() -> None:
    part = PartitionBounds(
        name="price_observations_2026_01",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert partition_eligible(part, cutoff) is True


def test_partition_at_exact_cutoff_boundary_is_eligible() -> None:
    # end == cutoff satisfies `end <= cutoff` (half-open range wholly
    # excludes the cutoff instant itself).
    part = PartitionBounds(
        name="price_observations_2026_01",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    cutoff = datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert partition_eligible(part, cutoff) is True


def test_partition_straddling_cutoff_is_not_eligible() -> None:
    # start < cutoff < end -- part of the range is still in-window, so
    # the WHOLE partition must be retained (FR-018).
    part = PartitionBounds(
        name="price_observations_2026_02",
        start=datetime(2026, 2, 1, tzinfo=timezone.utc),
        end=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    cutoff = datetime(2026, 2, 15, tzinfo=timezone.utc)
    assert partition_eligible(part, cutoff) is False


def test_partition_entirely_after_cutoff_is_not_eligible() -> None:
    part = PartitionBounds(
        name="price_observations_2026_06",
        start=datetime(2026, 6, 1, tzinfo=timezone.utc),
        end=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert partition_eligible(part, cutoff) is False


# --- per-table retention-window resolution (FR-017) -------------------------


def test_retention_windows_resolve_per_table_and_rollup_setting(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    windows = {
        "price_observations": settings.RETENTION_PRICE_OBSERVATIONS_DAYS,
        "request_attempts": settings.RETENTION_REQUEST_ATTEMPTS_DAYS,
        "price_alert_events": settings.RETENTION_PRICE_ALERT_EVENTS_DAYS,
        "webhook_events": settings.RETENTION_WEBHOOK_EVENTS_DAYS,
    }
    assert windows == {
        # 180 since EPA C9 / owner decision 11 -- the observation is the
        # evidence a pricing decision traces back to, and 90 days put it
        # below the dispute horizon (docs/RETENTION_POLICY.md §2.3).
        "price_observations": 180,
        "request_attempts": 90,
        "price_alert_events": 365,
        "webhook_events": 90,
    }
    assert settings.RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS == 730


# --- feeds_rollups=False entries skip the coverage check (FR-019) ----------


def test_feeds_rollups_true_only_for_price_observations_registry_shape() -> None:
    for entry in PARTITIONED_TABLES:
        expected = entry.name == "price_observations"
        assert entry.feeds_rollups is expected


# --- rollups_cover EXCEPT query shape (FR-016/F13 gate 1), no live DB ------


def test_rollups_cover_query_is_a_per_key_except_over_scraped_at_date() -> None:
    from datetime import date

    stmt = _rollups_cover_stmt("price_observations_2026_01", date(2026, 1, 1), date(2026, 2, 1))
    sql = _compiled(stmt)
    assert "EXCEPT" in sql
    assert "price_observations_2026_01" in sql
    # UTC-pinned, not the session-TimeZone-dependent bare `scraped_at::date`
    # — the rollup rows this is compared against are keyed on the UTC day.
    assert "(scraped_at AT TIME ZONE 'UTC')::date" in sql
    assert "variant_price_daily_rollups" in sql
    assert "'2026-01-01'" in sql
    assert "'2026-02-01'" in sql
    # Per-key (EPA C8, F13): both sides of the EXCEPT carry workspace_id
    # and product_variant_id, not just the bare date -- a date-only match
    # would let one tenant's rollup mask every other tenant's gap.
    assert sql.count("workspace_id") >= 2
    assert sql.count("product_variant_id") >= 2


# --- completion watermark gap query shape (F13 gate 2), no live DB ---------


def test_completion_watermark_gap_query_scans_every_calendar_date_in_range() -> None:
    from datetime import date

    stmt = _completion_watermark_gap_stmt(date(2026, 1, 1), date(2026, 2, 1))
    sql = _compiled(stmt)
    assert "generate_series" in sql
    assert "rollup_completion" in sql
    assert "complete = true" in sql.lower()
    assert "'2026-01-01'" in sql
    assert "'2026-02-01'" in sql


def test_rollup_age_delete_query_targets_rollups_table_only() -> None:
    from datetime import date

    stmt = _rollup_age_delete_stmt(date(2024, 1, 1))
    sql = _compiled(stmt)
    assert sql.strip().upper().startswith("DELETE FROM VARIANT_PRICE_DAILY_ROLLUPS")
    assert "price_observations" not in sql
    assert "request_attempts" not in sql


# --- rollups_cover against a fake session (pure logic, no live DB) ----------


class _FakeCoverageResult:
    def __init__(self, rows: list[tuple], scalar_value: object = None) -> None:
        self._rows = rows
        self._scalar_value = scalar_value

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._scalar_value


class _FakeCoverageSession:
    """Answers `rollups_cover`'s two gates (EPA C8, F13) with canned
    results: the per-key EXCEPT (gate 1, keyed by partition name baked
    into the statement text), the `rollup_completion` existence probe,
    and the completion-watermark gap scan (gate 2, both keyed by the
    constructor flags since neither carries a partition name)."""

    def __init__(
        self,
        missing_by_partition: dict[str, list[tuple]],
        *,
        completion_available: bool = True,
        watermark_gap: bool = False,
    ) -> None:
        self._missing_by_partition = missing_by_partition
        self._completion_available = completion_available
        self._watermark_gap = watermark_gap

    def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        sql = _compiled(stmt)
        if "to_regclass" in sql:
            return _FakeCoverageResult([], scalar_value=self._completion_available)
        if "generate_series" in sql:
            return _FakeCoverageResult([("gap",)] if self._watermark_gap else [])
        # The per-key EXCEPT (gate 1): the partition name is baked into
        # the statement text (not a bind param, to avoid a dynamic-
        # identifier bind) -- recover it from the compiled SQL instead.
        for name, missing in self._missing_by_partition.items():
            if name in sql:
                return _FakeCoverageResult(missing)
        return _FakeCoverageResult([])


def test_rollups_cover_true_when_no_missing_keys_and_watermark_complete() -> None:
    part = PartitionBounds(
        name="price_observations_2026_01",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    session = _FakeCoverageSession({part.name: []})
    assert rollups_cover(session, part) is True


def test_rollups_cover_false_when_a_key_is_missing_its_rollup() -> None:
    part = PartitionBounds(
        name="price_observations_2026_01",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    session = _FakeCoverageSession(
        {part.name: [("ws-1", "variant-1", datetime(2026, 1, 15).date())]}
    )
    assert rollups_cover(session, part) is False


def test_rollups_cover_false_when_completion_watermark_has_a_gap() -> None:
    """Gate 1 (per-key) passes but gate 2 (watermark) finds an
    incomplete/missing day -- a batch run killed mid-day can leave every
    pair it reached correctly rolled up while the day itself never
    finished (EPA C8, F13)."""
    part = PartitionBounds(
        name="price_observations_2026_01",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    session = _FakeCoverageSession({part.name: []}, watermark_gap=True)
    assert rollups_cover(session, part) is False


def test_rollups_cover_false_when_completion_table_not_migrated() -> None:
    """Gate 2 fails closed (never drop on the absence of evidence) when
    `rollup_completion` itself does not exist yet."""
    part = PartitionBounds(
        name="price_observations_2026_01",
        start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    session = _FakeCoverageSession({part.name: []}, completion_available=False)
    assert rollups_cover(session, part) is False


# --- run_retention end-to-end against a fake session (no live DB) ----------


@dataclass
class _FakeRunResult:
    value: object = None
    rowcount: int = 0
    rows: list = field(default_factory=list)

    def scalar(self):
        return self.value

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


@dataclass
class _FakePartitionRow:
    partition_name: str
    bound_expr: str


class _FakeRunSession:
    """Minimal stand-in for a SQLAlchemy `Session` driving `run_retention`
    end-to-end: answers `to_regclass`, `pg_inherits` partition discovery,
    the coverage EXCEPT check, and records every DDL/DELETE statement's
    compiled SQL text -- without an engine/connection/live DB (mirrors
    `test_partition_bounds.py`'s `_FakeSession`)."""

    def __init__(
        self,
        *,
        existing_tables: set[str],
        partitions_by_table: dict[str, list[_FakePartitionRow]],
        covered_partitions: set[str],
        rollup_delete_rowcount: int = 0,
        completion_available: bool = True,
        watermark_gap_dates: set[str] = frozenset(),
    ) -> None:
        self.existing_tables = existing_tables
        self.partitions_by_table = partitions_by_table
        self.covered_partitions = covered_partitions
        self.rollup_delete_rowcount = rollup_delete_rowcount
        # F13 gate 2: whether `rollup_completion` exists, and (baked into
        # the d0/d_n bind params, since the watermark query carries no
        # partition name) which ranges report a gap.
        self.completion_available = completion_available
        self.watermark_gap_dates = watermark_gap_dates
        self.executed_ddl: list[str] = []
        self.deleted_rollups = False

    def execute(self, stmt, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        sql = _compiled(stmt)
        if "to_regclass" in sql:
            params = stmt.compile().params
            name = params["qualified_name"].split(".", 1)[1]
            if name == "rollup_completion":
                return _FakeRunResult(value=self.completion_available)
            return _FakeRunResult(value=name in self.existing_tables)
        if "pg_inherits" in sql:
            params = stmt.compile().params
            parent_name = params["parent_name"]
            rows = self.partitions_by_table.get(parent_name, [])
            return _FakeRunResult(rows=rows)
        if "generate_series" in sql:
            params = stmt.compile().params
            key = f"{params['d0'].isoformat()}..{params['d_n'].isoformat()}"
            if key in self.watermark_gap_dates:
                return _FakeRunResult(rows=[("gap",)])
            return _FakeRunResult(rows=[])
        if "EXCEPT" in sql:
            for name in self.covered_partitions:
                if name in sql:
                    return _FakeRunResult(rows=[])  # first() -> None -> covered
            return _FakeRunResult(rows=[("missing",)])  # first() truthy -> uncovered
        if sql.strip().upper().startswith("DROP TABLE"):
            self.executed_ddl.append(sql)
            return _FakeRunResult()
        if sql.strip().upper().startswith("DELETE FROM VARIANT_PRICE_DAILY_ROLLUPS"):
            self.deleted_rollups = True
            return _FakeRunResult(rowcount=self.rollup_delete_rowcount)
        raise AssertionError(f"unexpected statement: {sql}")


def _bound_expr(start_iso: str, end_iso: str) -> str:
    return f"FOR VALUES FROM ('{start_iso}') TO ('{end_iso}')"


def test_run_retention_drops_expired_rollup_covered_partition_only(monkeypatch) -> None:
    settings = _settings_observations_90d(monkeypatch)
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)  # 90-day cutoff -> ~2026-04-07

    expired_covered = _FakePartitionRow(
        "price_observations_2026_01", _bound_expr("2026-01-01", "2026-02-01")
    )
    expired_uncovered = _FakePartitionRow(
        "price_observations_2026_02", _bound_expr("2026-02-01", "2026-03-01")
    )
    in_window = _FakePartitionRow(
        "price_observations_2026_07", _bound_expr("2026-07-01", "2026-08-01")
    )

    session = _FakeRunSession(
        existing_tables={
            "price_observations",
            "request_attempts",
            "price_alert_events",
        },
        partitions_by_table={
            "price_observations": [expired_covered, expired_uncovered, in_window],
            "request_attempts": [],
            "price_alert_events": [],
        },
        covered_partitions={"price_observations_2026_01"},
    )

    report = run_retention(session, now_utc=now, settings=settings)

    # `network_operations` joined `PARTITIONED_TABLES` in EPA C9, and the
    # `ROW_DELETE` families it registered are absent from this fake too --
    # so this is a containment assertion, not an equality one. What the
    # test is actually about is that a registered-but-absent table is
    # SKIPPED rather than raising (FR-002).
    assert "webhook_events" in report.tables_skipped_absent
    assert "network_operations" in report.tables_skipped_absent
    assert report.partitions_dropped == ["price_observations_2026_01"]
    assert report.partitions_skipped_pending_rollups == ["price_observations_2026_02"]
    assert any("DROP TABLE IF EXISTS price_observations_2026_01" in ddl for ddl in session.executed_ddl)
    assert not any("price_observations_2026_02" in ddl for ddl in session.executed_ddl)
    assert not any("price_observations_2026_07" in ddl for ddl in session.executed_ddl)
    assert session.deleted_rollups is True


def test_run_retention_retains_partition_when_watermark_incomplete_despite_key_coverage(
    monkeypatch,
) -> None:
    """F13: per-key coverage (gate 1) passing is not enough -- a day whose
    `rollup_completion` watermark is missing/incomplete (gate 2) must
    still retain the partition, e.g. a batch run killed mid-day left the
    keys it reached correctly rolled up but never finished the day."""
    settings = _settings_observations_90d(monkeypatch)
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)

    expired_partial = _FakePartitionRow(
        "price_observations_2026_01", _bound_expr("2026-01-01", "2026-02-01")
    )
    session = _FakeRunSession(
        existing_tables={"price_observations", "request_attempts", "price_alert_events"},
        partitions_by_table={
            "price_observations": [expired_partial],
            "request_attempts": [],
            "price_alert_events": [],
        },
        covered_partitions={"price_observations_2026_01"},  # gate 1 passes
        watermark_gap_dates={"2026-01-01..2026-02-01"},  # gate 2 fails
    )

    report = run_retention(session, now_utc=now, settings=settings)

    assert report.partitions_skipped_pending_rollups == ["price_observations_2026_01"]
    assert report.partitions_dropped == []
    assert not session.executed_ddl


def test_run_retention_drops_non_rollup_tables_by_age_alone_no_coverage_check(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)

    expired_attempt = _FakePartitionRow(
        "request_attempts_2026_01", _bound_expr("2026-01-01", "2026-02-01")
    )
    session = _FakeRunSession(
        existing_tables={"price_observations", "request_attempts", "price_alert_events"},
        partitions_by_table={
            "price_observations": [],
            "request_attempts": [expired_attempt],
            "price_alert_events": [],
        },
        covered_partitions=set(),  # no coverage data at all -- must not matter
    )

    report = run_retention(session, now_utc=now, settings=settings)

    # Dropped by age alone -- no EXCEPT coverage query ever gated this
    # table's drop (feeds_rollups=False, FR-019).
    assert "request_attempts_2026_01" in report.partitions_dropped
    assert report.partitions_skipped_pending_rollups == []


def test_run_retention_idempotent_on_absent_partitions(monkeypatch) -> None:
    """A second pass over an already-clean state (no eligible partitions
    left) drops nothing and errors on nothing (FR-020)."""
    settings = _settings(monkeypatch)
    now = datetime(2026, 7, 6, tzinfo=timezone.utc)

    session = _FakeRunSession(
        existing_tables={"price_observations", "request_attempts", "price_alert_events"},
        partitions_by_table={
            "price_observations": [],
            "request_attempts": [],
            "price_alert_events": [],
        },
        covered_partitions=set(),
    )

    report = run_retention(session, now_utc=now, settings=settings)

    assert report.partitions_dropped == []
    assert report.partitions_skipped_pending_rollups == []
    # Part B runs whenever its class is enabled (here: the `*` wildcard).
    assert session.deleted_rollups is True


def test_run_retention_rejects_naive_datetime(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    session = _FakeRunSession(
        existing_tables=set(), partitions_by_table={}, covered_partitions=set()
    )
    with pytest.raises(ValueError):
        run_retention(session, now_utc=datetime(2026, 7, 6), settings=settings)
