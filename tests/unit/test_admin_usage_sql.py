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
    UsageCursor,
    UsageWindowTooLarge,
    build_usage_query,
    decode_usage_cursor,
    encode_usage_cursor,
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
    with pytest.raises(UsageWindowTooLarge):
        validate_window(UNTIL, SINCE)


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
