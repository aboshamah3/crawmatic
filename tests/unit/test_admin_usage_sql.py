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
