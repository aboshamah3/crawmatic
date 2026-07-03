"""Unit tests for `app_shared.matches.upsert` (T023, FR-013/017, SC-003/006).

Pure, DB-independent -- compiles every statement to `postgresql`-dialect
SQL text (`str(stmt.compile(dialect=postgresql.dialect()))`) and asserts
on the rendered `ON CONFLICT (...) DO UPDATE SET ...` clause; never
opens a session or hits a database.
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

from app_shared.matches.upsert import (
    build_matches_upsert,
    dedup_last_wins,
    match_conflict_key,
    prepare_match_urls,
    resolve_match_variants,
    variant_lookup_keys,
)

WS = uuid.uuid4()


def _compiled(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _row(**overrides) -> dict:
    base = {
        "workspace_id": WS,
        "product_id": uuid.uuid4(),
        "product_variant_id": uuid.uuid4(),
        "competitor_id": uuid.uuid4(),
        "competitor_url": "https://competitor.com/products/widget",
        "normalized_competitor_url": "https://competitor.com/products/widget",
        "url_pattern": "competitor.com/products/*",
        "url_pattern_version": 1,
        "competitor_variant_identifier": None,
        "competitor_variant_sku": None,
        "competitor_variant_options": None,
        "external_title": None,
        "scrape_profile_id": None,
        "access_policy_id": None,
        "priority": "NORMAL",
        "status": "ACTIVE",
    }
    base.update(overrides)
    return base


# --- build_matches_upsert: single-arbiter compiled SQL (SC-006) --------------


def test_build_matches_upsert_compiles_single_on_conflict_statement() -> None:
    rows = [_row()]
    stmt = build_matches_upsert(rows)
    sql = _compiled(stmt)

    assert "INSERT INTO competitor_product_matches" in sql
    assert (
        "ON CONFLICT (workspace_id, product_variant_id, competitor_id, "
        "normalized_competitor_url)" in sql
    )
    assert "DO UPDATE SET" in sql


def test_build_matches_upsert_set_clause_excludes_conflict_and_identity_columns() -> None:
    stmt = build_matches_upsert([_row()])
    sql = _compiled(stmt)

    # Conflict columns + identity/audit columns must never be re-assigned
    # from `excluded` in the SET clause.
    for column in (
        "workspace_id",
        "product_variant_id",
        "competitor_id",
        "normalized_competitor_url",
        "product_id",
        "id",
        "created_at",
    ):
        assert f"{column} = excluded.{column}" not in sql


def test_build_matches_upsert_set_clause_excludes_health_fields() -> None:
    stmt = build_matches_upsert([_row()])
    sql = _compiled(stmt)

    for column in (
        "health_status",
        "last_error_code",
        "consecutive_failures",
        "success_rate_7d",
        "current_price_id",
        "last_scraped_at",
        "last_success_at",
        "last_failed_at",
    ):
        assert f"{column} = excluded.{column}" not in sql


def test_build_matches_upsert_set_clause_includes_updatable_columns_and_updated_at() -> None:
    stmt = build_matches_upsert([_row()])
    sql = _compiled(stmt)

    for column in (
        "competitor_url",
        "url_pattern",
        "url_pattern_version",
        "competitor_variant_identifier",
        "competitor_variant_sku",
        "competitor_variant_options",
        "external_title",
        "scrape_profile_id",
        "access_policy_id",
        "priority",
        "status",
    ):
        assert f"{column} = excluded.{column}" in sql
    assert "updated_at = now()" in sql


def test_build_matches_upsert_is_exactly_one_statement_for_any_batch_size() -> None:
    """SC-006: bounded statements -- one INSERT regardless of row count, no
    per-row loop anywhere in the builder."""
    rows = [_row() for _ in range(25)]
    stmt = build_matches_upsert(rows)
    # A single Insert construct compiles to exactly one top-level
    # INSERT ... VALUES (...), (...), ... statement -- not 25 statements.
    sql = _compiled(stmt)
    assert sql.count("INSERT INTO competitor_product_matches") == 1


# --- match_conflict_key / dedup_last_wins (reused, not reimplemented) -------


def test_match_conflict_key_shape() -> None:
    variant_id = uuid.uuid4()
    competitor_id = uuid.uuid4()
    row = {
        "product_variant_id": variant_id,
        "competitor_id": competitor_id,
        "normalized_competitor_url": "https://competitor.com/x",
    }
    assert match_conflict_key(row) == (
        variant_id,
        competitor_id,
        "https://competitor.com/x",
    )


def test_dedup_last_wins_keeps_last_row_on_match_key() -> None:
    variant_id = uuid.uuid4()
    competitor_id = uuid.uuid4()
    rows = [
        {
            "product_variant_id": variant_id,
            "competitor_id": competitor_id,
            "normalized_competitor_url": "https://competitor.com/x",
            "external_title": "first",
        },
        {
            "product_variant_id": variant_id,
            "competitor_id": competitor_id,
            "normalized_competitor_url": "https://competitor.com/x",
            "external_title": "last",
        },
    ]
    result = dedup_last_wins(rows, match_conflict_key)
    assert len(result) == 1
    assert result[0]["external_title"] == "last"


def test_dedup_last_wins_does_not_collapse_distinct_keys() -> None:
    rows = [
        {
            "product_variant_id": uuid.uuid4(),
            "competitor_id": uuid.uuid4(),
            "normalized_competitor_url": "https://competitor.com/a",
        },
        {
            "product_variant_id": uuid.uuid4(),
            "competitor_id": uuid.uuid4(),
            "normalized_competitor_url": "https://competitor.com/b",
        },
    ]
    result = dedup_last_wins(rows, match_conflict_key)
    assert len(result) == 2


# --- prepare_match_urls: safe/unsafe split (FR-013 reject-and-report) ------


def test_prepare_match_urls_splits_safe_and_unsafe() -> None:
    rows = [
        {"competitor_url": "https://competitor.com/products/widget-123"},
        {"competitor_url": "http://localhost/admin"},
        {"competitor_url": "https://WWW.Competitor.com/ar/products/iphone-15/?utm=x#f"},
    ]
    safe, rejected = prepare_match_urls(rows)

    assert len(safe) == 2
    assert len(rejected) == 1

    assert rejected[0] == {
        "index": 1,
        "code": "UNSAFE_URL",
        "reason": "INTERNAL_HOSTNAME",
        "url": "http://localhost/admin",
    }

    # Safe rows are stamped with normalized/pattern/version, do not abort
    # the rest of the batch, and are absent from `rejected`.
    assert safe[0]["normalized_competitor_url"] == "https://competitor.com/products/widget-123"
    assert safe[0]["url_pattern_version"] == 1
    assert safe[1]["normalized_competitor_url"] == (
        "https://competitor.com/ar/products/iphone-15?utm=x"
    )
    assert safe[1]["url_pattern"] == "competitor.com/ar/products/*"


def test_prepare_match_urls_all_unsafe_yields_empty_safe_not_an_exception() -> None:
    rows = [
        {"competitor_url": "http://127.0.0.1/"},
        {"competitor_url": "ftp://competitor.com/"},
    ]
    safe, rejected = prepare_match_urls(rows)
    assert safe == []
    assert len(rejected) == 2
    assert [r["index"] for r in rejected] == [0, 1]


# --- variant_lookup_keys ------------------------------------------------------


def test_variant_lookup_keys_partitions_by_identity_kind() -> None:
    explicit_id = uuid.uuid4()
    rows = [
        {"product_variant_id": explicit_id},
        {"variant_external_id": "EXT-1"},
        {"variant_sku": "SKU-1"},
    ]
    external_ids, skus, variant_ids = variant_lookup_keys(rows)
    assert external_ids == {"EXT-1"}
    assert skus == {"SKU-1"}
    assert variant_ids == {explicit_id}


def test_variant_lookup_keys_includes_explicit_ids_needing_parent_lookup() -> None:
    """Unlike the catalog parent-resolution helper, a row with an explicit
    product_variant_id still needs a lookup (to resolve its parent
    product_id and confirm workspace membership)."""
    explicit_id = uuid.uuid4()
    external_ids, skus, variant_ids = variant_lookup_keys([{"product_variant_id": explicit_id}])
    assert variant_ids == {explicit_id}
    assert external_ids == set()
    assert skus == set()


# --- resolve_match_variants: fills variant_id + product_id, splits unresolved -


def test_resolve_match_variants_fills_product_id_from_parent_for_all_identity_kinds() -> None:
    variant_id_a = uuid.uuid4()
    product_id_a = uuid.uuid4()
    variant_id_b = uuid.uuid4()
    product_id_b = uuid.uuid4()
    explicit_variant_id = uuid.uuid4()
    explicit_product_id = uuid.uuid4()

    rows = [
        {"variant_external_id": "EXT-1"},
        {"variant_sku": "SKU-1"},
        {"product_variant_id": explicit_variant_id},
    ]
    resolved, unresolved = resolve_match_variants(
        rows,
        by_external_id={"EXT-1": (variant_id_a, product_id_a)},
        by_sku={"SKU-1": (variant_id_b, product_id_b)},
        by_id={explicit_variant_id: explicit_product_id},
    )

    assert unresolved == []
    assert len(resolved) == 3
    assert resolved[0]["product_variant_id"] == variant_id_a
    assert resolved[0]["product_id"] == product_id_a
    assert resolved[1]["product_variant_id"] == variant_id_b
    assert resolved[1]["product_id"] == product_id_b
    assert resolved[2]["product_variant_id"] == explicit_variant_id
    assert resolved[2]["product_id"] == explicit_product_id


def test_resolve_match_variants_unresolved_for_unknown_identity() -> None:
    rows = [
        {"variant_external_id": "UNKNOWN"},
        {"product_variant_id": uuid.uuid4()},
    ]
    resolved, unresolved = resolve_match_variants(
        rows, by_external_id={}, by_sku={}, by_id={}
    )
    assert resolved == []
    assert len(unresolved) == 2


def test_resolve_match_variants_row_with_no_identity_is_unresolved() -> None:
    resolved, unresolved = resolve_match_variants(
        [{}], by_external_id={}, by_sku={}, by_id={}
    )
    assert resolved == []
    assert len(unresolved) == 1
