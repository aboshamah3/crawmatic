"""Pure render tests for the set-based daily-rollup statement (EPA C7, F12).

`app_shared.maintenance.rollup_sql` is the whole physical shape of the
rollup: the ``DISTINCT ON`` collapse, the preserved half-open range
predicate, the keyset window, and the ``ON CONFLICT DO UPDATE``. All of
that is asserted here without a live database — the same
"render-and-assert" posture the statement builders this module replaced
already used (`_driver_pairs_stmt` &c.), because a regression in any of
those clauses is a silent correctness or performance failure that no
amount of green integration tests on a 5-row fixture would catch.

The behavioural proof (10-vs-1 skew, hand-computed averages, crash
resume) lives in `tests/integration/test_rollup_set_based.py`, which
needs a real Postgres.
"""

from __future__ import annotations

import re
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app_shared.maintenance.rollup_sql import (  # noqa: E402
    MIN_UUID,
    ROLLUP_BATCH_LIMIT,
    ROLLUP_COMPLETION_TABLE,
    render_rollup_batch_sql,
    rollup_batch_stmt,
)
from app_shared.maintenance.rollups import utc_day_bounds  # noqa: E402


# --- The latest-ELIGIBLE-per-match collapse -------------------------------


def test_collapses_to_one_row_per_workspace_variant_match() -> None:
    """The semantic core of F12: a competitor observed ten times counts once."""
    sql = render_rollup_batch_sql()
    assert (
        "SELECT DISTINCT ON (o.workspace_id, o.product_variant_id, o.match_id)" in sql
    )


def test_the_surviving_row_per_match_is_the_latest_one() -> None:
    collapse = _cte(render_rollup_batch_sql(), "latest_per_match")
    assert (
        "ORDER BY o.workspace_id, o.product_variant_id, o.match_id, o.scraped_at DESC"
        in collapse
    ), "DISTINCT ON without a matching ORDER BY picks an ARBITRARY row per match"


def test_eligibility_is_filtered_before_the_collapse_not_after() -> None:
    """'Latest ELIGIBLE', not 'latest, then check it'.

    If the filter moved outside this CTE, a competitor whose most recent
    read of the day happened to fail would drop out of the day entirely
    instead of contributing its most recent usable price.
    """
    collapse = _cte(render_rollup_batch_sql(), "latest_per_match")
    assert "o.success" in collapse
    assert "o.comparable" in collapse
    assert "o.price IS NOT NULL" in collapse
    assert "o.currency = s.currency" in collapse


def test_currency_eligibility_compares_against_the_clients_own_currency() -> None:
    """FR-011: the comparison currency comes from `variant_price_states`,
    joined per (workspace, variant) — never a constant, never the
    observation's own currency compared to itself."""
    collapse = _cte(render_rollup_batch_sql(), "latest_per_match")
    assert "JOIN variant_price_states s" in collapse
    assert "s.workspace_id = o.workspace_id" in collapse
    assert "s.product_variant_id = o.product_variant_id" in collapse


def test_aggregate_runs_over_the_collapsed_set_not_the_raw_rows() -> None:
    aggregated = _cte(render_rollup_batch_sql(), "aggregated")
    assert "FROM latest_per_match" in aggregated
    assert "MIN(price)" in aggregated
    assert "MAX(price)" in aggregated
    assert "COUNT(*)" in aggregated
    assert "GROUP BY workspace_id, product_variant_id" in aggregated


def test_average_is_rounded_to_the_money_column_scale() -> None:
    """`average_competitor_price` is NUMERIC(18,4); an unrounded AVG of
    three prices does not fit and would be rejected."""
    assert "ROUND(AVG(price), 4)" in _cte(render_rollup_batch_sql(), "aggregated")


# --- The measured range predicate is preserved exactly --------------------


@pytest.mark.parametrize("dry_run", [False, True])
def test_day_predicate_is_the_measured_half_open_range(dry_run: bool) -> None:
    sql = render_rollup_batch_sql(dry_run=dry_run)
    occurrences = sql.count("o.scraped_at >= :day_start AND o.scraped_at < :day_end")
    # Once in the driver scan, once in the collapse.
    assert occurrences == 2, f"expected the range predicate twice, found {occurrences}"


@pytest.mark.parametrize("dry_run", [False, True])
def test_day_predicate_never_casts_the_partition_key(dry_run: bool) -> None:
    """A `scraped_at::date = D` cast defeats BOTH partition pruning and
    the (workspace_id, scraped_at) index, and resolves "day" in the
    session timezone. See `utc_day_bounds`."""
    sql = render_rollup_batch_sql(dry_run=dry_run)
    assert "scraped_at::date" not in sql
    assert "DATE(o.scraped_at)" not in sql
    assert "date_trunc" not in sql


def test_bound_day_params_are_the_shared_utc_day_bounds() -> None:
    target = date(2026, 7, 5)
    day_start, day_end = utc_day_bounds(target)
    params = rollup_batch_stmt(target, day_start, day_end).compile().params
    assert params["day_start"] == datetime(2026, 7, 5, tzinfo=timezone.utc)
    assert params["day_end"] == datetime(2026, 7, 6, tzinfo=timezone.utc)


# --- The keyset window ----------------------------------------------------


def test_batch_is_a_keyset_window_not_an_offset() -> None:
    """OFFSET re-scans everything it skips, so batch N costs O(N * limit).
    A row-comparison keyset seeks straight to the cursor."""
    driver = _cte(render_rollup_batch_sql(), "driver")
    assert (
        "(o.workspace_id, o.product_variant_id)\n              > "
        "(:last_workspace_id, :last_product_variant_id)" in driver
    )
    assert "LIMIT :batch_limit" in driver
    assert "OFFSET" not in render_rollup_batch_sql()


def test_default_keyset_start_is_the_minimum_uuid() -> None:
    """So "start from the beginning" needs no second statement shape."""
    assert MIN_UUID == uuid.UUID("00000000-0000-0000-0000-000000000000")
    target = date(2026, 7, 5)
    params = rollup_batch_stmt(target, *utc_day_bounds(target)).compile().params
    assert params["last_workspace_id"] == MIN_UUID
    assert params["last_product_variant_id"] == MIN_UUID


def test_default_batch_limit_is_the_plans_five_thousand() -> None:
    assert ROLLUP_BATCH_LIMIT == 5000
    target = date(2026, 7, 5)
    params = rollup_batch_stmt(target, *utc_day_bounds(target)).compile().params
    assert params["batch_limit"] == 5000


def test_driver_yields_one_row_per_workspace_variant_pair() -> None:
    """Not `SELECT DISTINCT ws, variant, product`: a variant whose
    observations disagreed about `product_id` would then appear twice in
    one batch, and the second copy would hit `ON CONFLICT DO UPDATE`
    against a row this very statement inserted — which Postgres rejects
    outright ("cannot affect row a second time")."""
    driver = _cte(render_rollup_batch_sql(), "driver")
    assert "SELECT DISTINCT ON (o.workspace_id, o.product_variant_id)" in driver


def test_cursor_key_is_the_batchs_maximum_pair() -> None:
    cursor = _cte(render_rollup_batch_sql(), "cursor_key")
    assert "ORDER BY d.workspace_id DESC, d.product_variant_id DESC" in cursor
    assert "LIMIT 1" in cursor


def test_batch_limit_must_be_positive() -> None:
    target = date(2026, 7, 5)
    with pytest.raises(ValueError):
        rollup_batch_stmt(target, *utc_day_bounds(target), batch_limit=0)


# --- The upsert -----------------------------------------------------------


def test_statement_contains_exactly_one_insert_select() -> None:
    sql = render_rollup_batch_sql()
    assert sql.count("INSERT INTO") == 1
    assert "INSERT INTO variant_price_daily_rollups" in sql


def test_upsert_conflicts_on_the_natural_key_and_updates_every_column() -> None:
    sql = render_rollup_batch_sql()
    assert "ON CONFLICT (workspace_id, product_variant_id, date) DO UPDATE SET" in sql
    for column in (
        "product_id",
        "currency",
        "client_price",
        "cheapest_competitor_price",
        "average_competitor_price",
        "highest_competitor_price",
        "comparable_competitor_count",
        "latest_alert_type",
    ):
        assert f"{column} = EXCLUDED.{column}" in sql, column
    assert "updated_at = now()" in sql


def test_upsert_is_absolute_never_additive() -> None:
    """The whole idempotence argument: a re-run overwrites, so a resumed
    or retried batch can never double-count."""
    sql = render_rollup_batch_sql()
    do_update = sql.split("DO UPDATE SET", 1)[1].split("RETURNING", 1)[0]
    assert "comparable_competitor_count = EXCLUDED.comparable_competitor_count" in do_update
    # No `col = variant_price_daily_rollups.col + EXCLUDED.col` anywhere:
    # an additive DO UPDATE is what turns a resumed batch into a
    # double-count.
    assert "+" not in do_update
    assert "variant_price_daily_rollups." not in do_update


def test_a_variant_with_no_comparable_observation_still_gets_a_row() -> None:
    """FR-013 / US2 AS-3: `comparable_competitor_count = 0` with NULL
    min/avg/max is a valid row, not an omission."""
    eligible = _cte(render_rollup_batch_sql(), "eligible")
    assert "LEFT JOIN aggregated a" in eligible
    assert "COALESCE(a.comparable_count, 0)" in eligible


def test_a_variant_with_no_client_state_is_excluded_and_reported() -> None:
    """No `variant_price_states` row means no `client_price` for a NOT NULL
    column — an inner join drops it, and the `skipped` CTE names it."""
    sql = render_rollup_batch_sql()
    eligible = _cte(sql, "eligible")
    assert "JOIN variant_price_states s" in eligible
    assert "LEFT JOIN" not in eligible.split("LEFT JOIN aggregated")[0].replace(
        "JOIN variant_price_states s", ""
    )
    skipped = _cte(sql, "skipped")
    assert "WHERE s.workspace_id IS NULL" in skipped
    assert "variants_skipped_no_state" in sql


def test_every_join_pairs_workspace_id_with_product_variant_id() -> None:
    """FR-014. A join on `product_variant_id` alone would let one
    workspace's observation meet another's client price."""
    sql = render_rollup_batch_sql()
    joins = re.findall(r"JOIN \w+ (\w+)\n\s+ON (.*?)(?=\n\s{0,8}(?:WHERE|ORDER|LEFT|JOIN|\)))", sql, re.S)
    assert joins, "no joins found — the regex, not the SQL, is wrong"
    for alias, body in joins:
        assert "workspace_id" in body, f"join on {alias} is not workspace-scoped: {body}"
        assert "product_variant_id" in body, f"join on {alias} misses the variant: {body}"


# --- dry_run: no write statement exists at all ----------------------------


def test_dry_run_renders_no_write_statement() -> None:
    """`scripts/backfill_daily_rollups.py` runs inside `SET TRANSACTION
    READ ONLY`, which holds only if no write is ever ATTEMPTED."""
    sql = render_rollup_batch_sql(dry_run=True)
    assert "INSERT" not in sql
    assert "UPDATE" not in sql
    assert "ON CONFLICT" not in sql


def test_dry_run_still_reports_what_would_be_written() -> None:
    sql = render_rollup_batch_sql(dry_run=True)
    assert "(SELECT count(*) FROM eligible) AS rollups_upserted" in sql
    assert "AS driver_rows" in sql
    assert "AS variants_skipped_no_state" in sql


def test_dry_run_binds_no_target_date() -> None:
    """It has nowhere to put it — binding a parameter the text does not
    name is a SQLAlchemy error, not a silent no-op."""
    target = date(2026, 7, 5)
    params = (
        rollup_batch_stmt(target, *utc_day_bounds(target), dry_run=True).compile().params
    )
    assert "target_date" not in params
    assert rollup_batch_stmt(target, *utc_day_bounds(target)).compile().params[
        "target_date"
    ] == target


# --- The checkpoint table -------------------------------------------------


def test_completion_table_name_is_the_one_c8_reads() -> None:
    assert ROLLUP_COMPLETION_TABLE == "rollup_completion"


def _cte(sql: str, name: str) -> str:
    """Return the body of the named CTE, for clause-scoped assertions."""
    marker = f"{name} AS ("
    start = sql.index(marker) + len(marker)
    depth = 1
    for index in range(start, len(sql)):
        if sql[index] == "(":
            depth += 1
        elif sql[index] == ")":
            depth -= 1
            if depth == 0:
                return sql[start:index]
    raise AssertionError(f"unterminated CTE {name!r}")
