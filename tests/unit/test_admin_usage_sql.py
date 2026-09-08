"""Usage-export aggregation + cursor (PLAN §7.2, risk P2).

No database: the query is asserted by compiling it to SQL text, which
is what actually guards "aggregate in SQL, not Python".
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.services.admin_usage import (
    MAX_WINDOW_DAYS,
    InvalidUsageCursor,
    InvalidUsageWindow,
    UsageCursor,
    UsageWindowTooLarge,
    build_usage_query,
    decode_usage_cursor,
    encode_usage_cursor,
    normalize_window,
    validate_window,
)

SINCE = datetime(2026, 8, 1, tzinfo=timezone.utc)
UNTIL = datetime(2026, 8, 8, tzinfo=timezone.utc)


def _sql(stmt) -> str:
    """Compiled SQL with values still bound — the production shape."""
    return str(stmt.compile(dialect=postgresql.dialect()))


def _sql_with_values(stmt) -> str:
    """Compiled SQL with bind params rendered inline.

    Values reach the query as bound parameters (correct), so they are
    invisible in the plain compilation. Assertions *about the values*
    render them here rather than asking production code to inline them.
    """
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_window_within_limit_is_accepted() -> None:
    assert validate_window(SINCE, UNTIL) is None


def test_window_over_31_days_is_rejected() -> None:
    with pytest.raises(UsageWindowTooLarge):
        validate_window(SINCE, SINCE + timedelta(days=MAX_WINDOW_DAYS, seconds=1))


def test_window_exactly_31_days_is_accepted() -> None:
    assert validate_window(SINCE, SINCE + timedelta(days=MAX_WINDOW_DAYS)) is None


def test_inverted_window_is_rejected() -> None:
    """CHANGED (review finding I6b): an inverted window (`since >= until`)
    used to raise `UsageWindowTooLarge`, which the router mapped to the
    misleading `422 WINDOW_TOO_LARGE` -- an inverted window isn't "too
    large", it's malformed. It now raises the distinct
    `InvalidUsageWindow`, mapped to `422 INVALID_WINDOW`.
    `WINDOW_TOO_LARGE` stays reserved for the real >31-day case (see
    `test_window_over_31_days_is_rejected` above, unchanged)."""
    with pytest.raises(InvalidUsageWindow):
        validate_window(UNTIL, SINCE)


def test_equal_since_and_until_is_an_invalid_window_not_too_large() -> None:
    """`since == until` is degenerate (empty window), not inverted, but
    must be rejected the same way -- `until <= since` is the exact
    condition, not `until < since`."""
    with pytest.raises(InvalidUsageWindow):
        validate_window(SINCE, SINCE)


def test_naive_and_utc_windows_normalize_to_the_same_query() -> None:
    """A naive `since`/`until` reaches Postgres as a bare `timestamp`,
    interpreted in the session TimeZone -- silently shifting the window
    (review finding I6c). `normalize_window` must treat a naive
    datetime as UTC, so the compiled query is byte-identical to the one
    built from the explicit-UTC equivalent."""
    naive_since = datetime(2026, 8, 1)
    naive_until = datetime(2026, 8, 8)
    normalized_since, normalized_until = normalize_window(naive_since, naive_until)

    assert normalized_since == SINCE
    assert normalized_until == UNTIL
    assert normalized_since.tzinfo is timezone.utc
    assert normalized_until.tzinfo is timezone.utc

    sql_naive = _sql_with_values(
        build_usage_query(since=normalized_since, until=normalized_until, after=None, limit=10)
    )
    sql_aware = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    assert sql_naive == sql_aware


def test_normalize_window_leaves_aware_datetimes_untouched() -> None:
    since, until = normalize_window(SINCE, UNTIL)
    assert since is SINCE
    assert until is UNTIL


def test_cursor_round_trips() -> None:
    cursor = UsageCursor(
        cycle_ts=datetime(2026, 8, 3, 14, tzinfo=timezone.utc),
        workspace_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
    )
    assert decode_usage_cursor(encode_usage_cursor(cursor)) == cursor


def test_garbage_cursor_raises() -> None:
    with pytest.raises(InvalidUsageCursor):
        decode_usage_cursor("!!!not-base64!!!")


def test_truncated_cursor_raises() -> None:
    with pytest.raises(InvalidUsageCursor):
        decode_usage_cursor("eyJjIjogIjIwMjYt")


def test_query_bounds_the_window_on_the_partition_key() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert "request_attempts.created_at >=" in sql
    assert "request_attempts.created_at <" in sql


def test_query_aggregates_in_sql_not_python() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert "count(" in sql.lower()
    assert "bool_or(" in sql.lower()
    assert "group by" in sql.lower()
    assert "date_trunc" in sql.lower()


def test_query_attributes_links_to_products_via_matches() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert "competitor_product_matches" in sql
    assert "product_id" in sql


def test_query_reads_success_from_price_observations() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert "price_observations" in sql


def test_query_classifies_protected_by_access_method() -> None:
    sql = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    assert "PROXY_HTTP" in sql
    assert "PLAYWRIGHT_PROXY" in sql


def test_query_excludes_discovery_origin_attempts() -> None:
    """Task 2.3 fix round 1 (Important finding): discovery probes
    (`tasks_strategy._probe_sample`) write `RequestAttempt` rows tagged
    `origin='discovery'` with real `match_id`s, so before this filter
    they silently inflated `links_total`/`protected_links_attempted` --
    counters this module's docstring says the SaaS prices from.
    Discovery probes are internal COGS, not customer activity, so the
    `per_link` CTE (the sole source of every link/protected-link counter)
    must exclude them."""
    sql = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    assert "request_attempts.origin = 'scrape'" in sql, sql


def test_query_still_bounds_origin_as_a_partition_prunable_predicate() -> None:
    """The origin filter must sit in the SAME `per_link` CTE `WHERE`
    clause as the `created_at` partition-key bounds (docstring risk P2:
    "the only predicate on the partitioned request_attempts ... is a
    bounded range on their partition keys") -- i.e. it must not force a
    second, separate scan or subquery over `request_attempts`."""
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert sql.count("FROM request_attempts") == 1, sql


def test_query_orders_by_the_cursor_key() -> None:
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)).lower()
    order_by = sql.split("order by", 1)[1]
    assert order_by.index("cycle_ts") < order_by.index("workspace_id")
    assert order_by.index("workspace_id") < order_by.index("product_id")


def test_query_fetches_one_extra_row_to_detect_a_next_page() -> None:
    assert "LIMIT 11" in _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )


def test_cursor_predicate_is_a_keyset_tuple_comparison() -> None:
    after = UsageCursor(
        cycle_ts=datetime(2026, 8, 3, 14, tzinfo=timezone.utc),
        workspace_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
    )
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=after, limit=10))
    assert ">" in sql.split("HAVING")[-1] or "(cycle_ts, workspace_id, product_id) >" in sql.replace('"', "")


def test_query_reports_proxied_transport_facts_from_network_operations() -> None:
    """Task B3 (engine half): each row must additionally report
    `proxied_http_attempted`, `proxied_browser_attempted`, `proxy_bytes` —
    per (workspace, product, cycle) counts/bytes of the underlying
    `network_operations` (B2) PROXY/BROWSER transport rows."""
    sql = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    assert "network_operations" in sql
    assert "proxied_http_attempted" in sql
    assert "proxied_browser_attempted" in sql
    assert "proxy_bytes" in sql
    assert "'PROXY'" in sql
    assert "'BROWSER'" in sql
    # Joined on the physical-operation identity, not by workspace/product
    # columns network_operations does not have (it is fleet-owned, C1).
    # Task C6 routes that join through the shared `attempt_scan` CTE (the
    # single scan of the partitioned table) rather than naming
    # `request_attempts` twice, and follows children as well as the
    # attempt's own operation.
    assert (
        "network_operations.network_request_id = attempt_scan.network_operation_id"
        in sql
    )
    assert (
        "network_operations.parent_operation_id = attempt_scan.network_operation_id"
        in sql
    )


def test_query_still_single_scan_of_request_attempts_with_transport_join() -> None:
    """The B3 network_operations join must ride the SAME `per_link` scan
    of `request_attempts` (added as another LEFT JOIN before the
    match-folding GROUP BY), not a second CTE re-reading the partitioned
    table — the docstring's risk-P2 partition-pruning guarantee must
    survive this addition unchanged."""
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert sql.count("FROM request_attempts") == 1, sql


def test_query_sums_bytes_compressed_over_both_proxy_and_browser_transports() -> None:
    sql = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    lowered = sql.lower()
    # Task C6: bytes are summed over `per_op` -- one row per DISTINCT
    # physical operation -- not once per logical attempt, so an operation
    # three matches shared contributes its bytes exactly once.
    assert "network_operations.bytes_compressed as bytes_compressed" in lowered
    assert "sum(per_op.bytes_compressed) filter (where per_op.proxied)" in lowered
    assert "coalesce" in lowered


def test_query_casts_proxied_sums_to_integer_not_left_as_numeric() -> None:
    """`SUM(bigint)` renders as Postgres `numeric`, which psycopg decodes
    as `Decimal` — the export's JSON-facing contract requires plain
    ints, so both the inner (`bytes_compressed`) and outer (re-summed
    per-match totals) aggregates must be `CAST(..., BigInteger)`-wrapped."""
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)).upper()
    assert sql.count("CAST(") >= 4, sql


def test_cycle_ts_expression_is_identical_in_select_and_group_by() -> None:
    """Regression: the GROUP BY must reuse the SELECT's `cycle_ts` object.

    Building the expression twice yields two distinct trees, each with
    its own bind parameter for `'hour'`. Postgres then sees
    `date_trunc($1, ...)` in the SELECT and `date_trunc($5, ...)` in the
    GROUP BY, refuses to match them, and rejects the whole query with
    "column scrape_jobs.created_at must appear in the GROUP BY clause".
    A live gate run caught this; compile-only assertions did not.
    """
    sql = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    lowered = sql.lower()
    # Every date_trunc rendering in the statement must be byte-identical,
    # which is what proves a single shared expression object per CTE.
    renderings = set()
    index = lowered.find("date_trunc(")
    while index != -1:
        depth, cursor = 0, index + len("date_trunc")
        while cursor < len(lowered):
            if lowered[cursor] == "(":
                depth += 1
            elif lowered[cursor] == ")":
                depth -= 1
                if depth == 0:
                    break
            cursor += 1
        renderings.add(lowered[index : cursor + 1])
        index = lowered.find("date_trunc(", cursor)
    assert renderings, "no date_trunc found in the compiled query"
    # Two CTEs => at most two distinct renderings (attempts, observations).
    assert len(renderings) <= 2, sorted(renderings)
    for rendering in renderings:
        assert "'hour'" in rendering, rendering


# --- Task C6 (F17): provider dimension, distinct physical operations ---


def test_proxied_is_the_provider_dimension_not_the_transport() -> None:
    """A direct browser navigation costs no proxy money. `transport` says
    how a fetch was shaped, `provider`/`proxy_provider_id` say who was
    paid -- so the paid predicate is the conjunction, and `transport IN
    ('PROXY','BROWSER')` must no longer appear as the discriminator."""
    sql = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    assert "network_operations.provider != 'direct'" in sql
    assert "attempt_scan.proxy_provider_id IS NOT NULL" in sql
    # The old discriminator: an IN-list over the two transports used as
    # the *paid* test. Transport survives only as the HTTP/browser split
    # inside the already-proxied set (`per_op.proxied AND ... = 'PROXY'`).
    assert "transport IN ('PROXY', 'BROWSER')" not in sql
    assert "per_op.proxied AND per_op.transport = 'PROXY'" in sql
    assert "per_op.proxied AND per_op.transport = 'BROWSER'" in sql


def test_proxied_counts_are_over_distinct_physical_operations() -> None:
    """One physical fetch shared by three matches is ONE operation. The
    counters must be `COUNT(DISTINCT network_request_id)`, never a
    per-match `COUNT(*)` re-summed in the outer aggregate."""
    sql = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    assert sql.count("count(DISTINCT per_op.network_request_id)") == 2
    # `per_op` folds to one row per operation before anything is summed.
    assert "GROUP BY attempt_scan.workspace_id, attempt_scan.product_id, " in sql
    assert "network_operations.network_request_id" in sql


def test_child_operations_are_included_via_parent_operation_id() -> None:
    """A browser page's subresources are `network_operations` rows with a
    `parent_operation_id` and no `request_attempts` row of their own --
    an equi-join on `network_operation_id` alone loses every one."""
    sql = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    assert "network_operations.parent_operation_id = attempt_scan.network_operation_id" in sql


def test_cost_is_allocated_through_cost_allocations_not_summed_per_attempt() -> None:
    """Money comes from `network_operation_allocations` -- the only table
    that says what share of one physical operation a workspace owes --
    joined on (operation, workspace), and is exposed as
    `allocated_cost_micro_units`."""
    sql = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    assert "network_operation_allocations" in sql
    assert (
        "network_operation_allocations.operation_id = "
        "network_operations.network_request_id" in sql
    )
    assert (
        "network_operation_allocations.workspace_id = attempt_scan.workspace_id"
        in sql
    )
    assert "allocated_cost_micro_units" in sql


def test_operation_facts_are_maxed_not_resummed_across_matches() -> None:
    """`op_totals` holds at most one row per (workspace, product, cycle),
    so the outer aggregate takes MAX of it. A SUM there would multiply
    every physical fact by the cycle's match count -- the exact
    over-count F17 removes."""
    sql = _sql_with_values(
        build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10)
    )
    for column in (
        "proxied_http_attempted",
        "proxied_browser_attempted",
        "proxy_bytes",
        "allocated_cost_micro_units",
    ):
        assert f"max(op_totals.{column})" in sql, column
        assert f"sum(op_totals.{column})" not in sql, column


def test_query_still_reads_the_partitioned_table_exactly_once() -> None:
    """Two CTEs now read the attempt scan (`per_link`, `per_op`), but the
    scan itself is built once -- the risk-P2 partition-pruning guarantee
    is a property of `FROM request_attempts` appearing once."""
    sql = _sql(build_usage_query(since=SINCE, until=UNTIL, after=None, limit=10))
    assert sql.count("FROM request_attempts") == 1, sql
    # `per_link` and `per_op` both read the scan -- twice from the CTE,
    # once from the table. That is the point of hoisting the scan out.
    assert sql.count("FROM attempt_scan") == 2, sql
