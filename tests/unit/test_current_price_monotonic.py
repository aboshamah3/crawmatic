"""Monotonic `match_current_prices` projection (READY-013-g).

`match_current_prices` is a **projection**: the latest-known truth per
match, derived from the `price_observations` stream. Its writer is
`scrape_core.pipelines._flush_batch`, and jobs do not finish in the order
they started — a slow spider, a retried attempt, or a re-queued batch can
land its result *after* a newer scrape of the same match has already
projected. Without an ordering guard the late writer wins purely because
it committed last, and the customer-visible current price silently goes
backwards in time.

The guard has to hold in two places, and this file pins both:

* **Across flushes / across workers** — the `ON CONFLICT DO UPDATE` must
  carry a `WHERE` comparing the stored `scraped_at` with the incoming
  one, so the refusal is decided by Postgres inside the same statement.
  A read-then-write in Python could not hold: two concurrent workers
  would both read the old row and both decide they are newer.
* **Within one batch** — the batch collapses to one row per match before
  it is sent, and that collapse must pick the newest *observation*, not
  whichever item happened to sit last in the list.

The A2 cancellation fence is the neighbouring, different rule: a result
belonging to a **cancelled** job never projects at all. The case that
actually needs monotonicity is the late result from a slow job that was
never cancelled — pinned here alongside the fence regression.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from app_shared.enums import AccessMethod, ExtractionMethod, ScrapeErrorCode, StockStatus

from scrape_core import pipelines as pipelines_mod
from scrape_core.items import ScrapeResult
from scrape_core.pipelines import _flush_batch

WORKSPACE_ID = uuid.uuid4()
MATCH_ID = uuid.uuid4()

_T0 = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


class _FakeSettings:
    PRICE_ANALYSIS_DEDUP_TTL_SECONDS = 21600
    STRATEGY_STATS_KEY_TTL_SECONDS = 3600
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None):
        if nx and name in self.store:
            return None
        self.store[name] = value
        return True


class _EmptyResult:
    def scalars(self) -> "_EmptyResult":
        return self

    def all(self) -> list[Any]:
        return []

    def first(self) -> None:
        return None

    def scalar_one_or_none(self) -> None:
        return None


class _FakeSession:
    """Records what the flush executed. Reads answer empty; writes are kept."""

    def __init__(self) -> None:
        self.added: list[list[Any]] = []
        self.executed: list[Any] = []
        self.selects: list[Any] = []

    def add_all(self, items: Any) -> None:
        self.added.append(list(items))

    def execute(self, stmt: Any) -> Any:
        if isinstance(stmt, Select):
            self.selects.append(stmt)
            return _EmptyResult()
        self.executed.append(stmt)
        return None


class _FakeWorkspaceTxn:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    def __call__(self, workspace_id: Any) -> "_FakeWorkspaceTxn":
        return self

    def __enter__(self) -> _FakeSession:
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        return False


@pytest.fixture(autouse=True)
def _stub_seams(monkeypatch: Any) -> None:
    """Same seam stubs `test_persistence_batching.py` uses — this file
    tests the projection's ordering, not the target/outbox/stats seams."""
    monkeypatch.setattr(pipelines_mod, "mark_target", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "write_outbox_message", lambda *a, **k: None)
    monkeypatch.setattr(pipelines_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: _FakeRedis())


@pytest.fixture
def session(monkeypatch: Any) -> _FakeSession:
    fake = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn(fake))
    return fake


def _result(
    *,
    scraped_at: datetime,
    price: Decimal | None = Decimal("9.99"),
    success: bool = True,
    stock_status: StockStatus | None = StockStatus.IN_STOCK,
    match_id: uuid.UUID | None = None,
    scrape_job_id: uuid.UUID | None = None,
) -> ScrapeResult:
    return ScrapeResult(
        workspace_id=WORKSPACE_ID,
        match_id=match_id or MATCH_ID,
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=scrape_job_id or uuid.uuid4(),
        url="https://competitor.com/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        scraped_at=scraped_at,
        success=success,
        price=price if success else None,
        currency="SAR" if success else None,
        stock_status=stock_status,
        extraction_method=ExtractionMethod.JSON_LD if success else None,
        extraction_confidence=Decimal("0.9500") if success else None,
        error_code=None if success else ScrapeErrorCode.PRICE_NOT_FOUND,
        error_message=None if success else "no price candidate found",
    )


def _sql(stmt: Any) -> str:
    return str(stmt.compile(dialect=postgresql.dialect()))


def _upserts(session: _FakeSession) -> list[Any]:
    return [stmt for stmt in session.executed if "match_current_prices" in _sql(stmt)]


def _conflict_where(sql: str) -> str:
    """The text of the `ON CONFLICT ... DO UPDATE SET ... WHERE ...` clause."""
    match = re.search(r"ON CONFLICT.*?DO UPDATE SET (.*)$", sql, re.S)
    assert match, f"statement is not an ON CONFLICT DO UPDATE:\n{sql}"
    tail = match.group(1)
    where = re.search(r"\bWHERE\b(.*)$", tail, re.S)
    return where.group(1) if where else ""


def _values_of(stmt: Any) -> list[dict[str, Any]]:
    """The row dicts a `pg_insert(...).values([...])` was built from."""
    params = stmt.compile(dialect=postgresql.dialect()).params
    rows: dict[int, dict[str, Any]] = {}
    for name, value in params.items():
        match = re.match(r"^(.*?)_m(\d+)$", name)
        index = int(match.group(2)) if match else 0
        column = match.group(1) if match else name
        rows.setdefault(index, {})[column] = value
    return [rows[index] for index in sorted(rows)]


# --------------------------------------------------------------------------
# 1. The projection update is a compare-and-set, decided by Postgres
# --------------------------------------------------------------------------


def test_success_upsert_refuses_an_older_observation_in_sql(
    session: _FakeSession,
) -> None:
    """`ON CONFLICT DO UPDATE` must be guarded by the stored vs incoming
    `scraped_at`, so a late writer's UPDATE simply matches no row."""
    _flush_batch(WORKSPACE_ID, [_result(scraped_at=_T0)])

    (stmt,) = _upserts(session)
    where = _conflict_where(_sql(stmt))

    assert where, "the current-price upsert overwrites unconditionally — a late job wins"
    assert "match_current_prices.scraped_at" in where
    assert "excluded.scraped_at" in where


def test_out_of_stock_upsert_is_guarded_by_the_same_rule(
    session: _FakeSession,
) -> None:
    """The narrowed out-of-stock upsert is the same projection and needs
    the same guard — otherwise a stale 'unavailable' overwrites a newer
    in-stock observation's availability."""
    _flush_batch(
        WORKSPACE_ID,
        [
            _result(
                scraped_at=_T0,
                success=False,
                price=None,
                stock_status=StockStatus.OUT_OF_STOCK,
            )
        ],
    )

    (stmt,) = _upserts(session)
    where = _conflict_where(_sql(stmt))

    assert where, "the out-of-stock upsert overwrites unconditionally"
    assert "match_current_prices.scraped_at" in where
    assert "excluded.scraped_at" in where


def test_equal_timestamp_tie_break_is_deterministic(session: _FakeSession) -> None:
    """Two observations sharing a `scraped_at` must resolve the same way
    every time — the guard breaks the tie on the observation id (uuid7,
    so the later-issued observation is the larger one), never on which
    statement happened to run second."""
    _flush_batch(WORKSPACE_ID, [_result(scraped_at=_T0)])

    (stmt,) = _upserts(session)
    where = _conflict_where(_sql(stmt))

    assert "observation_id" in where, "equal timestamps resolve by statement order"


def test_projection_is_never_read_then_written(session: _FakeSession) -> None:
    """No SELECT of the projection precedes the write.

    A read-then-write cannot be correct here: two workers flushing
    concurrently would both read the same old row, both conclude they are
    newer, and the later COMMIT would still win. The comparison has to
    happen inside the UPDATE.
    """
    _flush_batch(WORKSPACE_ID, [_result(scraped_at=_T0)])

    projection_reads = [
        stmt for stmt in session.selects if "match_current_prices" in _sql(stmt)
    ]
    assert projection_reads == []


# --------------------------------------------------------------------------
# 2. Within one batch, the newest observation wins — not the last item
# --------------------------------------------------------------------------


def test_stale_item_later_in_the_batch_does_not_beat_a_newer_one(
    session: _FakeSession,
) -> None:
    """A batch collapses to one row per match. When a slow attempt's
    older result is appended after a fresher one, the collapse must keep
    the fresher one."""
    fresh = _result(scraped_at=_T0 + timedelta(minutes=5), price=Decimal("50.00"))
    stale = _result(scraped_at=_T0, price=Decimal("10.00"))

    _flush_batch(WORKSPACE_ID, [fresh, stale])

    (stmt,) = _upserts(session)
    (row,) = _values_of(stmt)
    assert row["price"] == Decimal("50.00")
    assert row["scraped_at"] == _T0 + timedelta(minutes=5)


def test_newer_item_later_in_the_batch_still_wins(session: _FakeSession) -> None:
    """The mirror case — ordering by observation time, not a blanket
    'keep the first'."""
    stale = _result(scraped_at=_T0, price=Decimal("10.00"))
    fresh = _result(scraped_at=_T0 + timedelta(minutes=5), price=Decimal("50.00"))

    _flush_batch(WORKSPACE_ID, [stale, fresh])

    (stmt,) = _upserts(session)
    (row,) = _values_of(stmt)
    assert row["price"] == Decimal("50.00")


def test_cross_kind_collapse_uses_observation_time_not_batch_order(
    session: _FakeSession,
) -> None:
    """One match can produce both a priced success and an out-of-stock
    failure in the same batch. Which one survives must be the newer
    observation, even when it is listed first."""
    fresh_success = _result(
        scraped_at=_T0 + timedelta(minutes=5), price=Decimal("50.00")
    )
    stale_oos = _result(
        scraped_at=_T0,
        success=False,
        price=None,
        stock_status=StockStatus.OUT_OF_STOCK,
    )

    _flush_batch(WORKSPACE_ID, [fresh_success, stale_oos])

    upserts = _upserts(session)
    assert len(upserts) == 1, "the stale kind was still sent as a second statement"
    (row,) = _values_of(upserts[0])
    assert row["scraped_at"] == _T0 + timedelta(minutes=5)
    assert row["price"] == Decimal("50.00")


def test_fresher_out_of_stock_beats_a_stale_success_in_the_same_batch(
    session: _FakeSession,
) -> None:
    """The reverse direction: the product really did go unavailable after
    the stale priced observation, so the out-of-stock row is the one that
    survives the collapse."""
    stale_success = _result(scraped_at=_T0, price=Decimal("10.00"))
    fresh_oos = _result(
        scraped_at=_T0 + timedelta(minutes=5),
        success=False,
        price=None,
        stock_status=StockStatus.OUT_OF_STOCK,
    )

    _flush_batch(WORKSPACE_ID, [stale_success, fresh_oos])

    upserts = _upserts(session)
    assert len(upserts) == 1
    (row,) = _values_of(upserts[0])
    assert row["stock_status"] == StockStatus.OUT_OF_STOCK
    assert row["scraped_at"] == _T0 + timedelta(minutes=5)


def test_distinct_matches_are_not_collapsed_against_each_other(
    session: _FakeSession,
) -> None:
    """Ordering is per match — an older observation of a *different*
    match must still be projected."""
    other_match = uuid.uuid4()
    newer = _result(scraped_at=_T0 + timedelta(minutes=5))
    older_other = _result(scraped_at=_T0, match_id=other_match)

    _flush_batch(WORKSPACE_ID, [newer, older_other])

    (stmt,) = _upserts(session)
    rows = _values_of(stmt)
    assert {row["match_id"] for row in rows} == {MATCH_ID, other_match}


# --------------------------------------------------------------------------
# 3. Interaction with the A2 cancellation fence
# --------------------------------------------------------------------------


def test_late_result_from_a_non_cancelled_job_still_reaches_the_guarded_upsert(
    session: _FakeSession,
) -> None:
    """The core case. A slow job that was never cancelled is not fenced —
    its result is written, and it is the SQL guard (not the fence) that
    stops it overwriting newer truth. So the statement must be emitted
    *and* carry the guard.
    """
    _flush_batch(WORKSPACE_ID, [_result(scraped_at=_T0 - timedelta(hours=2))])

    (stmt,) = _upserts(session)
    assert _conflict_where(_sql(stmt)), "an un-fenced late result overwrites freely"


def test_cancelled_job_result_never_projects_at_all(monkeypatch: Any) -> None:
    """Regression pin for the neighbouring A2 rule: a `late_after_cancel`
    result is refused outright — no current-price statement at all, guard
    or no guard."""
    fake = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn(fake))

    cancelled_job = uuid.uuid4()
    monkeypatch.setattr(
        pipelines_mod,
        "cancelled_scrape_job_ids",
        lambda session, job_ids: {cancelled_job} & set(job_ids),
    )

    _flush_batch(
        WORKSPACE_ID,
        [_result(scraped_at=_T0, scrape_job_id=cancelled_job)],
    )

    assert _upserts(fake) == []


def test_cancelled_and_live_results_in_one_batch_only_project_the_live_one(
    monkeypatch: Any,
) -> None:
    """The fence filters by match, so a live result for a *different*
    match still projects — with the monotonic guard on it."""
    fake = _FakeSession()
    monkeypatch.setattr(pipelines_mod, "workspace_txn", _FakeWorkspaceTxn(fake))

    cancelled_job = uuid.uuid4()
    live_match = uuid.uuid4()
    monkeypatch.setattr(
        pipelines_mod,
        "cancelled_scrape_job_ids",
        lambda session, job_ids: {cancelled_job} & set(job_ids),
    )

    _flush_batch(
        WORKSPACE_ID,
        [
            _result(scraped_at=_T0, scrape_job_id=cancelled_job),
            _result(scraped_at=_T0, match_id=live_match),
        ],
    )

    (stmt,) = _upserts(fake)
    rows = _values_of(stmt)
    assert [row["match_id"] for row in rows] == [live_match]
    assert _conflict_where(_sql(stmt))
