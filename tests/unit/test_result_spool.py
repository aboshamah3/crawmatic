"""Durable result spool (F05, plan task B1) — `scrape_core.result_spool`.

The spool is the thing standing between "a flush failed" and "a paid-for
fetch disappeared", so the properties tested here are the ones that make
that claim true:

- a ``ScrapeResult`` survives the round trip through SQLite **exactly**,
  including the value types JSON does not have (UUID, datetime, Decimal,
  enum) — a price that came back as a float would be a price that changed;
- a row is deleted only by :meth:`ResultSpool.resolve` (the "the
  transaction committed" signal), never by reading it;
- :meth:`ResultSpool.defer` keeps the row queued and counts the attempt,
  which is what the pipeline compares against its quarantine threshold;
- :meth:`ResultSpool.quarantine` MOVES a row rather than deleting it: out
  of the replay set, still on disk for an operator;
- the file is durable across process-lifetime boundaries (a second
  ``ResultSpool`` over the same path sees what the first appended), which
  is the whole point of not keeping the queue in a Python list.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app_shared.enums import AccessMethod, ExtractionMethod, ScrapeErrorCode, StockStatus

from scrape_core.items import ScrapeResult
from scrape_core.result_spool import (
    KIND_QUARANTINED,
    SPOOL_SCHEMA_VERSION,
    ResultSpool,
    _encode,
)

WORKSPACE_ID = uuid.uuid4()


def _make_result(*, success: bool = True) -> ScrapeResult:
    return ScrapeResult(
        workspace_id=WORKSPACE_ID,
        match_id=uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=uuid.uuid4(),
        url="https://shop.example.com/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=success,
        price=Decimal("1234.5678") if success else None,
        old_price=Decimal("1999.0000") if success else None,
        currency="SAR" if success else None,
        stock_status=StockStatus.IN_STOCK if success else StockStatus.OUT_OF_STOCK,
        extraction_method=ExtractionMethod.JSON_LD if success else None,
        extraction_confidence=Decimal("0.9500") if success else None,
        error_code=None if success else ScrapeErrorCode.PRICE_NOT_FOUND,
        error_message=None if success else "no price candidate found",
        scraped_at=datetime(2026, 9, 7, 12, 30, 45, 123456, tzinfo=UTC),
        attempt_id=uuid.uuid4(),
        response_time_ms=812,
    )


@pytest.fixture()
def spool(tmp_path: Path) -> ResultSpool:
    return ResultSpool(tmp_path / "spool.sqlite")


def test_a_result_round_trips_through_the_spool_exactly(spool: ResultSpool) -> None:
    result = _make_result()

    spool.append_batch([result])
    (entry,) = spool.pending()

    assert entry.result == result
    assert entry.workspace_id == WORKSPACE_ID
    # The types JSON does not have, checked by identity of type as well as
    # value -- `Decimal("1234.5678") == 1234.5678` would pass on value.
    assert isinstance(entry.result.price, Decimal)
    assert entry.result.price == Decimal("1234.5678")
    assert entry.result.scraped_at == result.scraped_at
    assert entry.result.stock_status is StockStatus.IN_STOCK
    assert isinstance(entry.result.attempt_id, uuid.UUID)


def test_a_failure_result_round_trips_too(spool: ResultSpool) -> None:
    result = _make_result(success=False)
    spool.append_batch([result])
    (entry,) = spool.pending()
    assert entry.result == result
    assert entry.result.error_code is ScrapeErrorCode.PRICE_NOT_FOUND


def test_append_batch_returns_ids_positionally_and_preserves_order(
    spool: ResultSpool,
) -> None:
    results = [_make_result() for _ in range(3)]

    ids = spool.append_batch(results)

    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert [entry.row_id for entry in spool.pending()] == ids
    assert [entry.result.match_id for entry in spool.pending()] == [
        result.match_id for result in results
    ]


def test_reading_never_removes_a_row_only_resolve_does(spool: ResultSpool) -> None:
    ids = spool.append_batch([_make_result(), _make_result()])

    spool.pending()
    spool.pending()
    assert spool.pending_count() == 2  # reading is not draining

    spool.resolve(ids[:1])
    assert spool.pending_count() == 1
    assert [entry.row_id for entry in spool.pending()] == ids[1:]


def test_defer_keeps_the_row_and_counts_the_attempt(spool: ResultSpool) -> None:
    ids = spool.append_batch([_make_result(), _make_result()])

    assert spool.defer_many(ids, "conn reset") == {ids[0]: 1, ids[1]: 1}
    assert spool.defer(ids[0], "conn reset again") == 2
    assert spool.pending_count() == 2  # still queued -- a defer is not a drop
    assert {entry.row_id: entry.attempts for entry in spool.pending()} == {ids[0]: 2, ids[1]: 1}


def test_defer_of_a_vanished_row_reports_zero_attempts(spool: ResultSpool) -> None:
    (row_id,) = spool.append_batch([_make_result()])
    spool.resolve([row_id])

    assert spool.defer(row_id, "too late") == 0


def test_quarantine_moves_a_row_out_of_replay_without_deleting_it(
    spool: ResultSpool,
) -> None:
    result = _make_result()
    ids = spool.append_batch([result, _make_result()])

    moved = spool.quarantine(ids[:1])

    assert len(moved) == 1
    assert spool.pending_count() == 1  # only the untouched row replays
    assert spool.quarantined_count() == 1
    assert [entry.row_id for entry in spool.pending()] == ids[1:]
    # The evidence survives verbatim -- an operator can still see WHICH
    # fetch was paid for and never persisted.
    (quarantined,) = spool._buffer.pending(limit=10, kind=KIND_QUARANTINED)
    assert quarantined.payload["result"]["match_id"] == str(result.match_id)


def test_load_returns_only_the_requested_rows_and_tolerates_missing_ones(
    spool: ResultSpool,
) -> None:
    ids = spool.append_batch([_make_result() for _ in range(3)])
    spool.resolve([ids[1]])

    loaded = spool.load([ids[0], ids[1], ids[2]])

    assert [entry.row_id for entry in loaded] == [ids[0], ids[2]]


def test_the_spool_outlives_the_object_that_wrote_it(tmp_path: Path) -> None:
    """The property a Python list cannot have."""
    path = tmp_path / "durable.sqlite"
    writer = ResultSpool(path)
    result = _make_result()
    writer.append_batch([result])
    writer.close()

    reader = ResultSpool(path)

    assert reader.pending_count() == 1
    assert reader.pending()[0].result == result


def test_a_row_from_an_unknown_schema_version_is_never_replayed(
    spool: ResultSpool,
) -> None:
    """Forward compatibility: mis-decoding is worse than not decoding."""
    payload = _encode(_make_result())
    payload["v"] = SPOOL_SCHEMA_VERSION + 1
    row_id = spool._buffer.append("SCRAPE_RESULT", payload)

    assert spool.pending() == []
    # Left in place with the reason recorded, not deleted.
    assert spool.pending_count() == 1
    (event,) = spool._buffer.pending(limit=10, kind="SCRAPE_RESULT")
    assert event.row_id == row_id
    assert event.attempts == 1


def test_the_encoded_envelope_carries_the_schema_version() -> None:
    payload = _encode(_make_result())

    assert payload["v"] == SPOOL_SCHEMA_VERSION
    assert payload["result"]["currency"] == "SAR"
    # Decimals are strings, never floats: a price that round-trips through
    # binary floating point is a price that changed.
    assert payload["result"]["price"] == "1234.5678"
    assert payload["result"]["access_method"] == AccessMethod.DIRECT_HTTP.value
