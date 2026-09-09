"""Unit tests for the per-data-class retention registry and the
owner-ratification switch (EPA C9, F14, plan §9.1).

Pure and DB-independent. Four things are asserted here, and each of them
is a property somebody could break without any test noticing:

1. **The switch defaults CLOSED.** `Settings.RETENTION_ENABLED_CLASSES`
   ships empty, `retention_class_enabled` answers `False` for every
   registered family, and a full `run_retention` walk against a fake
   session issues NO drop and NO delete. This is the single most
   important assertion in the file: the whole design of C9 is "a window
   in `config.py` is a proposal until the owner signs for it", and that
   is only true while this stays true.
2. **Every family is well-formed** — its `retention_setting` resolves to
   a real positive `Settings` field, its `class_key` is unique, and the
   `RETENTION_CLASS_KEYS` set the validator reads matches the registry
   exactly (a family missing from that set could never be enabled; a key
   in the set with no family could be "enabled" and silently do nothing).
3. **Every `ROW_DELETE` family that needs a terminal-state predicate has
   one**, and the predicates never name a live state. A retention sweep
   that deletes a `RESERVED` cost lease, a `PENDING` scrape target or a
   `POSTED` dispatch intent is not a retention bug, it is a data-loss
   bug in the operational path.
4. **The decided defaults match `docs/RETENTION_POLICY.md`.** The
   numbers are the ratification artifact; a drift between the document
   the owner signs and the constant the code uses would make the
   signature meaningless.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from app_shared.config import Settings
from app_shared.maintenance.registry import (
    PARTITIONED_TABLES,
    RETENTION_ALL_CLASSES,
    RETENTION_CLASS_KEYS,
    RETENTION_FAMILIES,
    RetentionFamily,
    RetentionMechanism,
    retention_class_enabled,
    retention_family,
    retention_family_days,
)
from app_shared.maintenance.retention import _row_delete_stmt, run_retention

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

#: The decided defaults `docs/RETENTION_POLICY.md` §2.9.c tabulates and
#: the owner signs against. Duplicated here ON PURPOSE rather than read
#: back out of `Settings`: a test that asks the code what the code says
#: cannot catch the code changing.
RATIFIED_WINDOWS = {
    "price_observations": 180,
    "request_attempts": 90,
    "price_alert_events": 365,
    "webhook_events": 90,
    "network_operations": 730,
    "network_operation_children": 30,
    "cost_allocations": 730,
    "variant_price_daily_rollups": 730,
    "scrape_job_targets": 90,
    "dispatch_intents": 30,
    "costauth_reservations": 30,
}

#: States a retention sweep must NEVER touch, per table. Each one is live
#: operational state whose removal breaks something outside retention.
FORBIDDEN_STATES = {
    "scrape_job_targets": ("PENDING", "STARTED", "DEFERRED"),
    "dispatch_intents": ("PLANNED", "POSTED"),
    "cost_reservations": ("RESERVED",),
}


def _set_required_env(monkeypatch) -> None:
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def _settings(monkeypatch, enabled: str | None = None) -> Settings:
    _set_required_env(monkeypatch)
    if enabled is None:
        monkeypatch.delenv("RETENTION_ENABLED_CLASSES", raising=False)
    else:
        monkeypatch.setenv("RETENTION_ENABLED_CLASSES", enabled)
    return Settings(_env_file=None)


# --- 1. the switch defaults CLOSED -----------------------------------------


def test_retention_enabled_classes_ships_empty(monkeypatch) -> None:
    """The shipped default is an empty list, not a wildcard and not None."""
    assert _settings(monkeypatch).RETENTION_ENABLED_CLASSES == []


def test_no_family_is_enabled_by_default(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    for family in RETENTION_FAMILIES:
        assert retention_class_enabled(family.class_key, settings) is False, (
            f"{family.class_key} is enabled on a default deployment — the "
            "owner-ratification switch is not closed"
        )


def test_wildcard_enables_every_family(monkeypatch) -> None:
    settings = _settings(monkeypatch, RETENTION_ALL_CLASSES)
    assert all(
        retention_class_enabled(f.class_key, settings) for f in RETENTION_FAMILIES
    )


def test_named_class_enables_only_itself(monkeypatch) -> None:
    settings = _settings(monkeypatch, "request_attempts")
    assert retention_class_enabled("request_attempts", settings) is True
    assert retention_class_enabled("price_observations", settings) is False


def test_comma_separated_env_is_parsed_not_json(monkeypatch) -> None:
    """The repo's pool convention, never JSON — and whitespace-tolerant."""
    settings = _settings(monkeypatch, " request_attempts , dispatch_intents ")
    assert settings.RETENTION_ENABLED_CLASSES == [
        "request_attempts",
        "dispatch_intents",
    ]


def test_unknown_class_name_fails_settings_construction(monkeypatch) -> None:
    """A typo must fail the DEPLOY, not silently disable a family the
    owner believes they enabled."""
    with pytest.raises(Exception) as excinfo:
        _settings(monkeypatch, "price_observation")  # missing trailing 's'
    assert "RETENTION_ENABLED_CLASSES" in str(excinfo.value)


# --- 2. every family is well-formed ----------------------------------------


def test_class_keys_are_unique() -> None:
    keys = [family.class_key for family in RETENTION_FAMILIES]
    assert len(keys) == len(set(keys))


def test_class_keys_set_matches_the_registry_exactly() -> None:
    """A family missing from `RETENTION_CLASS_KEYS` could never be
    enabled (the validator would reject its own key); a key with no
    family could be "enabled" and silently do nothing."""
    assert RETENTION_CLASS_KEYS == frozenset(
        family.class_key for family in RETENTION_FAMILIES
    )


def test_every_family_resolves_a_positive_window(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    for family in RETENTION_FAMILIES:
        days = retention_family_days(family, settings)
        assert isinstance(days, int) and days > 0, family.class_key


def test_families_are_frozen() -> None:
    family = RETENTION_FAMILIES[0]
    with pytest.raises(Exception):
        family.class_key = "mutated"  # type: ignore[misc]


def test_retention_family_lookup_raises_on_unknown_key() -> None:
    assert isinstance(retention_family("request_attempts"), RetentionFamily)
    with pytest.raises(KeyError):
        retention_family("not_a_class")


def test_every_partitioned_table_has_a_matching_family() -> None:
    """The two registries must not drift: a partitioned table with no
    family is a table the switch cannot gate, so it would drop
    unconditionally."""
    family_by_table = {
        f.table: f
        for f in RETENTION_FAMILIES
        if f.mechanism is RetentionMechanism.PARTITION_DROP
    }
    for entry in PARTITIONED_TABLES:
        family = family_by_table[entry.name]
        assert family.class_key == entry.name
        assert family.retention_setting == entry.retention_setting
        assert family.feeds_rollups is entry.feeds_rollups
        assert family.timestamp_column == entry.partition_key


def test_only_price_observations_feeds_rollups() -> None:
    """Every family EPA C9 added is `feeds_rollups=False` — nothing
    downstream aggregates the network ledger into
    `variant_price_daily_rollups`, so a coverage gate there would be a
    gate on evidence that does not exist."""
    for family in RETENTION_FAMILIES:
        assert family.feeds_rollups is (family.class_key == "price_observations")


# --- 3. terminal-state predicates ------------------------------------------


@pytest.mark.parametrize("table,forbidden", sorted(FORBIDDEN_STATES.items()))
def test_row_delete_predicate_excludes_live_states(table, forbidden) -> None:
    family = next(f for f in RETENTION_FAMILIES if f.table == table)
    assert family.row_predicate is not None, f"{table} sweeps rows with no state filter"
    for state in forbidden:
        assert f"'{state}'" not in family.row_predicate, (
            f"{family.class_key}'s retention predicate names the LIVE state "
            f"{state!r} — this would delete work still in flight"
        )


def test_cost_allocations_batches_by_operation_not_by_row() -> None:
    """`trg_noa_allocation_total` is a DEFERRED constraint trigger that
    re-checks a whole operation's allocations at COMMIT. A batch boundary
    inside one operation's set reads as a shortfall and aborts the pass,
    so this family must batch by `operation_id`."""
    family = retention_family("cost_allocations")
    assert family.group_column == "operation_id"
    sql = str(_row_delete_stmt(family, 100))
    assert "GROUP BY operation_id" in sql
    assert "HAVING max(created_at) < :cutoff" in sql


def test_row_delete_statement_is_bounded_by_a_limit() -> None:
    """An unbounded DELETE over a multi-million-row table is one enormous
    transaction and one lock held for its whole duration."""
    for family in RETENTION_FAMILIES:
        if family.mechanism is not RetentionMechanism.ROW_DELETE:
            continue
        assert "LIMIT 250" in str(_row_delete_stmt(family, 250)), family.class_key


def test_children_family_is_summarize_then_delete() -> None:
    """The distinction from `ROW_DELETE` is the whole point of the
    family: the parent's byte breakdown survives as a summary row."""
    family = retention_family("network_operation_children")
    assert family.mechanism is RetentionMechanism.SUMMARIZE_THEN_DELETE
    assert family.table == "network_operations"
    assert family.row_predicate == "parent_operation_id IS NOT NULL"


# --- 4. the decided defaults match the ratified document -------------------


def test_decided_defaults_match_the_retention_policy_document(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    actual = {
        family.class_key: retention_family_days(family, settings)
        for family in RETENTION_FAMILIES
    }
    assert actual == RATIFIED_WINDOWS


def test_allocations_never_outlive_their_operations(monkeypatch) -> None:
    """An allocation outliving its operation is an orphan; an operation
    outliving its allocations is a cost nobody owns. If the two windows
    ever diverge, the allocation one must be the SHORTER."""
    settings = _settings(monkeypatch)
    assert settings.RETENTION_COST_ALLOCATIONS_DAYS <= (
        settings.RETENTION_NETWORK_OPERATIONS_DAYS
    )


def test_children_expire_before_their_parents(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    assert settings.RETENTION_NETWORK_OPERATION_CHILDREN_DAYS < (
        settings.RETENTION_NETWORK_OPERATIONS_DAYS
    )


def test_rollups_outlive_the_observations_they_aggregate(monkeypatch) -> None:
    """`rollups_cover` exists so an aggregate survives its raw source; a
    window ordering that reversed that would make the gate pointless."""
    settings = _settings(monkeypatch)
    assert settings.RETENTION_VARIANT_PRICE_DAILY_ROLLUPS_DAYS > (
        settings.RETENTION_PRICE_OBSERVATIONS_DAYS
    )


# --- the end-to-end proof: a default deployment removes nothing ------------


@dataclass
class _Result:
    rowcount: int = 0
    value: object = None
    _rows: list = field(default_factory=list)

    def scalar(self):
        return self.value

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class _RecordingSession:
    """Answers `to_regclass` with "yes, every table exists" and records
    every other statement. If retention is properly gated, the recorder
    stays empty — the point of the fake is that ANY statement other than
    an existence probe is a failure."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.commits = 0

    def execute(self, stmt, params=None):
        sql = str(stmt)
        if "to_regclass" in sql:
            return _Result(value=True)
        self.statements.append(sql)
        return _Result(rowcount=0)

    def commit(self) -> None:
        self.commits += 1


def test_run_retention_drops_nothing_when_no_class_is_enabled(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    session = _RecordingSession()

    report = run_retention(
        session, now_utc=datetime(2030, 1, 1, tzinfo=timezone.utc), settings=settings
    )

    assert session.statements == [], (
        "a default deployment issued retention statements: " f"{session.statements}"
    )
    assert report.partitions_dropped == []
    assert report.rollup_rows_deleted == 0
    assert report.rows_deleted_by_class == {}
    # Every class `run_retention` owns. `network_operation_children` is
    # deliberately absent: it belongs to
    # `MAINTENANCE_LEDGER_SUMMARIZE_CHILDREN`, which reports its own
    # `class_not_enabled` — see
    # `tests/integration/test_ledger_child_summarization.py`.
    assert set(report.classes_skipped_not_enabled) == set(RATIFIED_WINDOWS) - {
        "network_operation_children"
    }


def test_run_retention_reports_each_skipped_class_exactly_once(monkeypatch) -> None:
    settings = _settings(monkeypatch)
    report = run_retention(
        _RecordingSession(),
        now_utc=datetime(2030, 1, 1, tzinfo=timezone.utc),
        settings=settings,
    )
    skipped = report.classes_skipped_not_enabled
    assert len(skipped) == len(set(skipped))
