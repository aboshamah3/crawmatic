"""`app_shared/profiles/upsert.py` unit tests (SPEC-06 US1 T021, FR-020, SC-008).

Pure — compiles statements to the `postgresql` dialect, never executes.
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from app_shared.profiles.upsert import build_profiles_upsert, dedup_last_wins, prepare_profiles

_PG_DIALECT = postgresql.dialect()


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=_PG_DIALECT))


def _row(**overrides):
    base = {
        "workspace_id": uuid.uuid4(),
        "name": "profile-a",
        "mode": "HTTP",
        "adapter_key": "default_http",
        "jsonld_enabled": True,
        "platform_patterns_enabled": True,
        "embedded_json_enabled": True,
        "price_selector": None,
        "price_xpath": None,
        "price_regex": None,
        "old_price_selector": None,
        "old_price_xpath": None,
        "old_price_regex": None,
        "currency_selector": None,
        "currency_xpath": None,
        "currency_regex": None,
        "stock_selector": None,
        "stock_xpath": None,
        "stock_regex": None,
        "title_selector": None,
        "title_xpath": None,
        "variant_strategy": "PAGE_SINGLE_PRICE",
        "variant_selector_config": None,
        "price_transform_rules": None,
        "validation_rules": None,
        "confidence_rules": None,
        "wait_for_selector": None,
        "request_timeout_ms": 30000,
        "browser_timeout_ms": None,
        "headers": None,
        "cookies": None,
    }
    base.update(overrides)
    return base


# --- build_profiles_upsert: single-statement compile-to-SQL (SC-008) --------


def test_build_profiles_upsert_compiles_to_one_on_conflict_statement() -> None:
    ws = uuid.uuid4()
    rows = [_row(workspace_id=ws, name="a"), _row(workspace_id=ws, name="b")]

    stmt = build_profiles_upsert(rows)
    sql = _compiled(stmt)

    assert sql.count("INSERT INTO scrape_profiles") == 1
    assert "ON CONFLICT (workspace_id, name)" in sql
    assert "WHERE workspace_id IS NOT NULL" in sql
    assert "DO UPDATE SET" in sql


def test_build_profiles_upsert_excludes_immutable_columns_from_update_set() -> None:
    stmt = build_profiles_upsert([_row()])
    update_values = stmt._post_values_clause.update_values_to_set  # type: ignore[attr-defined]
    update_cols = {
        (col.name if hasattr(col, "name") else col) for col, _value in update_values
    }

    assert "id" not in update_cols
    assert "workspace_id" not in update_cols
    assert "created_at" not in update_cols
    assert "updated_at" in update_cols
    assert "name" in update_cols
    assert "mode" in update_cols


# --- dedup_last_wins (reused from catalog.upsert) ----------------------------


def test_dedup_last_wins_keeps_last_on_workspace_id_name() -> None:
    ws = uuid.uuid4()
    first = _row(workspace_id=ws, name="dup", price_selector=".old")
    second = _row(workspace_id=ws, name="dup", price_selector=".new")
    other = _row(workspace_id=ws, name="distinct")

    deduped = dedup_last_wins([first, other, second], lambda r: (r["workspace_id"], r["name"]))

    assert len(deduped) == 2
    dup_row = next(r for r in deduped if r["name"] == "dup")
    assert dup_row["price_selector"] == ".new"


# --- prepare_profiles: valid/rejected split + reject-report shape ----------


def test_prepare_profiles_splits_valid_and_rejected() -> None:
    ws = uuid.uuid4()
    rows = [
        _row(name="good"),
        {"name": "bad", "mode": "NOT_A_MODE"},
    ]

    valid, rejected = prepare_profiles(rows, workspace_id=ws)

    assert len(valid) == 1
    assert valid[0]["name"] == "good"
    assert valid[0]["workspace_id"] == ws

    assert len(rejected) == 1
    entry = rejected[0]
    assert entry["index"] == 1
    assert entry["name"] == "bad"
    assert entry["field"] == "mode"
    assert entry["code"] == "INVALID_ENUM"
    assert "reason" in entry


def test_prepare_profiles_never_aborts_batch_on_invalid_row() -> None:
    ws = uuid.uuid4()
    rows = [
        {"name": "bad-1", "price_regex": "(a+)+"},
        _row(name="good-1"),
        {"name": "bad-2", "mode": "NOPE"},
        _row(name="good-2"),
    ]

    valid, rejected = prepare_profiles(rows, workspace_id=ws)

    assert {r["name"] for r in valid} == {"good-1", "good-2"}
    assert {r["name"] for r in rejected} == {"bad-1", "bad-2"}


def test_prepare_profiles_dedups_last_wins_on_workspace_id_name() -> None:
    ws = uuid.uuid4()
    rows = [
        _row(name="dup", price_selector=".old"),
        _row(name="dup", price_selector=".new"),
    ]

    valid, rejected = prepare_profiles(rows, workspace_id=ws)

    assert rejected == []
    assert len(valid) == 1
    assert valid[0]["price_selector"] == ".new"
