"""B6 regression: `_upsert_stats` must cast the success *ratio*, never the
success count, to NUMERIC(5, 4) (which only holds values < 10)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.dialects import postgresql

from app_shared.enums import AccessMethod, MethodType
from app_shared.strategy import flush, stats_buffer


class _Result:
    def one(self):
        return None


class _CaptureSession:
    def __init__(self) -> None:
        self.stmt = None

    def execute(self, stmt):
        self.stmt = stmt
        return _Result()


def _compiled_upsert() -> str:
    drained = stats_buffer.DrainedDelta(
        attempt=15,
        success=12,
        failure=3,
        rt_ms_sum=1500,
        conf_sum=90_000,
        qualifying_success=12,
        distinct_urls=5,
    )
    session = _CaptureSession()
    flush._upsert_stats(
        session,
        uuid.uuid4(),
        MethodType.ACCESS,
        AccessMethod.HTTP.value if hasattr(AccessMethod, "HTTP") else list(AccessMethod)[0].value,
        drained,
        datetime.now(timezone.utc),
    )
    assert session.stmt is not None
    return str(session.stmt.compile(dialect=postgresql.dialect()))


def test_success_rate_does_not_cast_count_to_numeric_5_4():
    sql = _compiled_upsert()
    set_clause = sql.split("DO UPDATE SET", 1)[1]
    sr = re.search(r"success_rate = (.*?)(?:, avg_response_time_ms|$)", set_clause, re.S)
    assert sr, set_clause
    expr = sr.group(1)
    # the count cast to NUMERIC(5, 4) is the overflow bug
    assert not re.search(r"CAST\([^)]*success_count[^)]*AS NUMERIC\(5, 4\)\)", expr)
    # numerator is cast to unbounded NUMERIC (no integer division) over nullif(attempts)
    assert re.search(r"CAST\([^()]*success_count[^()]* AS NUMERIC\)\s*/\s*(CAST\()?nullif\(", expr, re.S)


def test_numerator_of_12_of_15_is_not_bounded_by_numeric_5_4():
    # NUMERIC(5, 4) max is 9.9999: a count >= 10 cannot be cast to it, but the
    # ratio (12/15 = 0.8) fits. Model the arithmetic the SQL now performs.
    count, attempts = 12, 15
    assert Decimal(count) >= Decimal("10")
    ratio = Decimal(count) / Decimal(attempts)
    assert ratio < Decimal("10")
    assert ratio.quantize(Decimal("0.0001")) == Decimal("0.8000")
