"""Unit tests for `app_shared.maintenance.registry` + the `table_exists` gate
(SPEC-15 T006, FR-001/002).

Pure, DB-independent: asserts `PARTITIONED_TABLES`' shape/contents and
that `retention_days` resolves each entry's window from `Settings` by
name (FR-001, FR-017). `table_exists`'s existence-probe statement is
compiled to `postgresql`-dialect SQL text (mirroring
`tests/unit/test_catalog_upsert.py`'s `_compiled` helper) and asserted to
be built against `to_regclass` — never executed, no live DB (research
R4).
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql

from app_shared.config import Settings
from app_shared.maintenance.partitions import _to_regclass_stmt
from app_shared.maintenance.registry import PARTITIONED_TABLES, PartitionedTable, retention_days

# Mirrors `tests/unit/test_config.py`'s REQUIRED_ENV — `Settings()` fails
# fast (FR-017) unless every required variable is present, even for tests
# only interested in the (optional-with-defaults) RETENTION_* knobs.
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


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


# --- PARTITIONED_TABLES shape (FR-001) --------------------------------------


def test_partitioned_tables_has_exactly_four_entries() -> None:
    assert len(PARTITIONED_TABLES) == 4
    assert all(isinstance(entry, PartitionedTable) for entry in PARTITIONED_TABLES)


def test_partitioned_tables_names_and_partition_keys() -> None:
    by_name = {entry.name: entry for entry in PARTITIONED_TABLES}
    assert set(by_name) == {
        "price_observations",
        "request_attempts",
        "price_alert_events",
        "webhook_events",
    }
    assert by_name["price_observations"].partition_key == "scraped_at"
    assert by_name["request_attempts"].partition_key == "created_at"
    assert by_name["price_alert_events"].partition_key == "created_at"
    assert by_name["webhook_events"].partition_key == "created_at"


def test_feeds_rollups_true_only_for_price_observations() -> None:
    for entry in PARTITIONED_TABLES:
        expected = entry.name == "price_observations"
        assert entry.feeds_rollups is expected


def test_partitioned_tables_are_frozen() -> None:
    entry = PARTITIONED_TABLES[0]
    try:
        entry.name = "mutated"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("PartitionedTable must be frozen")


# --- retention_days resolves the Settings attr by name (FR-017) ------------


def test_retention_days_resolves_each_entry_via_settings(monkeypatch) -> None:
    _set_required_env(monkeypatch)
    settings = Settings(_env_file=None)

    expected = {
        "price_observations": 90,
        "request_attempts": 90,
        "price_alert_events": 365,
        "webhook_events": 90,
    }
    for entry in PARTITIONED_TABLES:
        assert retention_days(entry, settings) == expected[entry.name]


def test_retention_days_reflects_settings_override(monkeypatch) -> None:
    """Retention *durations* are DB/env-tunable (Principle IV) — overriding
    the `Settings` value changes what `retention_days` resolves, proving
    the lookup is by attribute name rather than a baked-in constant."""
    _set_required_env(monkeypatch)
    monkeypatch.setenv("RETENTION_PRICE_OBSERVATIONS_DAYS", "180")
    settings = Settings(_env_file=None)
    entry = next(e for e in PARTITIONED_TABLES if e.name == "price_observations")
    assert retention_days(entry, settings) == 180


# --- table_exists gate is built against to_regclass (FR-002, research R4) --


def test_table_exists_query_uses_to_regclass() -> None:
    stmt = _to_regclass_stmt("webhook_events")
    sql = _compiled(stmt)
    assert "to_regclass" in sql
    assert "IS NOT NULL" in sql


def test_table_exists_query_qualifies_with_public_schema() -> None:
    stmt = _to_regclass_stmt("price_observations")
    assert stmt.compile().params["qualified_name"] == "public.price_observations"


def test_table_exists_query_param_tracks_requested_name() -> None:
    for name in ("price_observations", "request_attempts", "webhook_events"):
        stmt = _to_regclass_stmt(name)
        assert stmt.compile().params["qualified_name"] == f"public.{name}"
