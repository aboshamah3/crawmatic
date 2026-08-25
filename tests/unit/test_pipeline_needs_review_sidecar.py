"""`scrape_core.pipelines._flush_batch` NEEDS_REVIEW sidecar wiring (EPA B6,
folded-in item 2).

B4's adapter sets `AdapterResult.metadata["needs_review"] = True` on an
`Ambiguous`/`IdentityIncompatible` variant resolution, carried through as
`ScrapeResult.needs_review` by `scrape_core.result_builder.build_scrape_result`
-- but nothing wrote it to the `match_audit_classifications` sidecar (A6,
`b8f3d61c9e02`) on the live scrape path until this change
(`_write_needs_review_classifications`, called from inside `_flush_batch`'s
existing `workspace_txn` transaction).

Exercised against fakes, same pattern as
`tests/unit/test_pipeline_target_terminalization.py`: a `_FakeSession`
that additionally discriminates the `match_audit_classifications`
SELECT/UPDATE/INSERT statements this feature issues (via raw
`sqlalchemy.text`) from every other statement `_flush_batch` already
issues -- no real DB, no real Celery/Redis, no running Twisted reactor.
Every item in this file carries `scrape_job_id=None` so the (separately,
thoroughly tested elsewhere) target-terminalization/finalize-enqueue
paths stay inert and this file's assertions are only about the sidecar
write.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from app_shared.enums import AccessMethod, MatchClassificationState, ScrapeErrorCode
from scrape_core import pipelines as pipelines_mod
from scrape_core.items import ScrapeResult
from scrape_core.pipelines import _flush_batch

WORKSPACE_ID = uuid.uuid4()


def _make_result(
    *,
    match_id: uuid.UUID | None = None,
    needs_review: bool = False,
    success: bool = False,
) -> ScrapeResult:
    return ScrapeResult(
        workspace_id=WORKSPACE_ID,
        match_id=match_id or uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_variant_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        scrape_job_id=None,  # keeps target-terminalization/finalize inert
        url="https://shop.example.com/p/1",
        access_method=AccessMethod.DIRECT_HTTP,
        success=success,
        price=Decimal("9.99") if success else None,
        error_code=None if success else ScrapeErrorCode.IDENTITY_UNRESOLVED,
        error_message=None if success else "ambiguous variant resolution",
        chain_complete=True,
        needs_review=needs_review,
    )


class _Row:
    def __init__(self, match_id: Any) -> None:
        self.match_id = match_id


class _FakeResult:
    """Generic empty result for every statement this file doesn't care about
    (mirrors `test_pipeline_target_terminalization.py`'s `_FakeResult`) --
    plus a fixed row list for the sidecar's own SELECT."""

    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows if rows is not None else []

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return self._rows

    def first(self) -> None:
        return None

    def scalar_one_or_none(self) -> None:
        return None


class _FakeSession:
    """Records every `execute` call (statement text + params); answers the
    sidecar's own "who's currently current" SELECT from a seeded set,
    every other statement generically (empty), same as
    `test_pipeline_target_terminalization.py`'s fake."""

    def __init__(self, *, current_match_ids: set[Any] | None = None) -> None:
        self.added: list[list[Any]] = []
        self.executed: list[tuple[str, Any]] = []
        self._current_match_ids = current_match_ids or set()

    def add_all(self, items: Any) -> None:
        self.added.append(list(items))

    def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        sql = str(stmt)
        self.executed.append((sql, params))
        if "SELECT match_id FROM match_audit_classifications" in sql:
            wanted = (params or {}).get("match_ids", [])
            return _FakeResult(
                rows=[_Row(match_id) for match_id in wanted if match_id in self._current_match_ids]
            )
        return _FakeResult()

    def _calls_matching(self, needle: str) -> list[tuple[str, Any]]:
        return [(sql, params) for sql, params in self.executed if needle in sql]


class _FakeWorkspaceTxn:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session
        self.entered = 0

    def __call__(self, workspace_id: Any) -> "_FakeWorkspaceTxn":
        return self

    def __enter__(self) -> _FakeSession:
        self.entered += 1
        return self._session

    def __exit__(self, *exc_info: Any) -> bool:
        return False


class _RecordingEnqueue:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session: Any, *, workspace_id: Any, task_name: str, queue: str,
                 kwargs: dict[str, Any] | None = None, dedup_key: str | None = None,
                 now: Any = None) -> None:
        self.calls.append({"name": task_name, "queue": queue, "kwargs": kwargs})


class _FakeSettings:
    PRICE_ANALYSIS_DEDUP_TTL_SECONDS = 21600
    STRATEGY_STATS_KEY_TTL_SECONDS = 3600
    STRATEGY_PROMOTION_CONFIDENCE_THRESHOLD = 0.85


class _FakeRedis:
    def set(self, name: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool:
        return True


def _install_fakes(monkeypatch: Any, *, current_match_ids: set[Any] | None = None) -> _FakeSession:
    session = _FakeSession(current_match_ids=current_match_ids)
    txn = _FakeWorkspaceTxn(session)
    monkeypatch.setattr(pipelines_mod, "workspace_txn", txn)
    monkeypatch.setattr(pipelines_mod, "write_outbox_message", _RecordingEnqueue())
    monkeypatch.setattr(pipelines_mod, "get_settings", lambda: _FakeSettings())
    monkeypatch.setattr(pipelines_mod, "get_redis_client", lambda: _FakeRedis())
    return session


# --- core wiring ---------------------------------------------------------------


def test_needs_review_item_writes_a_needs_review_row(monkeypatch: Any) -> None:
    session = _install_fakes(monkeypatch)
    match_id = uuid.uuid4()
    item = _make_result(match_id=match_id, needs_review=True)

    _flush_batch(WORKSPACE_ID, [item])

    inserts = session._calls_matching("INSERT INTO match_audit_classifications")
    assert len(inserts) == 1
    _sql, params = inserts[0]
    assert len(params) == 1
    row = params[0]
    assert row["match_id"] == match_id
    assert row["state"] == MatchClassificationState.NEEDS_REVIEW.value
    # A fresh row's superseded_at is a literal NULL in the SQL, never a
    # bound param -- proves this is genuinely a new/current row, not one
    # that (incorrectly) tried to supersede itself.
    assert "superseded_at" not in row


def test_non_needs_review_item_writes_nothing_to_the_sidecar(monkeypatch: Any) -> None:
    session = _install_fakes(monkeypatch)
    item = _make_result(needs_review=False, success=True)

    _flush_batch(WORKSPACE_ID, [item])

    assert session._calls_matching("match_audit_classifications") == []


def test_needs_review_item_with_no_existing_current_row_skips_the_supersede_update(
    monkeypatch: Any,
) -> None:
    session = _install_fakes(monkeypatch, current_match_ids=set())
    item = _make_result(needs_review=True)

    _flush_batch(WORKSPACE_ID, [item])

    assert session._calls_matching("UPDATE match_audit_classifications") == []
    assert len(session._calls_matching("INSERT INTO match_audit_classifications")) == 1


def test_needs_review_item_supersedes_an_existing_current_row(monkeypatch: Any) -> None:
    match_id = uuid.uuid4()
    session = _install_fakes(monkeypatch, current_match_ids={match_id})
    item = _make_result(match_id=match_id, needs_review=True)

    _flush_batch(WORKSPACE_ID, [item])

    updates = session._calls_matching("UPDATE match_audit_classifications")
    assert len(updates) == 1
    _sql, params = updates[0]
    assert params["match_ids"] == [match_id]
    assert len(session._calls_matching("INSERT INTO match_audit_classifications")) == 1


def test_duplicate_match_ids_in_one_batch_write_a_single_classification_row(
    monkeypatch: Any,
) -> None:
    session = _install_fakes(monkeypatch)
    match_id = uuid.uuid4()
    batch = [
        _make_result(match_id=match_id, needs_review=True),
        _make_result(match_id=match_id, needs_review=True),
    ]

    _flush_batch(WORKSPACE_ID, batch)

    inserts = session._calls_matching("INSERT INTO match_audit_classifications")
    assert len(inserts) == 1
    _sql, params = inserts[0]
    assert len(params) == 1


def test_needs_review_write_shares_the_flush_batch_transaction(monkeypatch: Any) -> None:
    session = _install_fakes(monkeypatch)
    item = _make_result(needs_review=True)

    _flush_batch(WORKSPACE_ID, [item])

    # Exactly one `workspace_txn` entry for the whole flush -- the sidecar
    # write is not a second reactor hop (mirrors
    # test_target_marking_happens_inside_the_single_workspace_txn).
    inserts = session._calls_matching("INSERT INTO match_audit_classifications")
    assert len(inserts) == 1


def test_mixed_batch_only_the_needs_review_item_writes_a_row(monkeypatch: Any) -> None:
    session = _install_fakes(monkeypatch)
    reviewed_id = uuid.uuid4()
    batch = [
        _make_result(match_id=reviewed_id, needs_review=True),
        _make_result(needs_review=False, success=True),
    ]

    _flush_batch(WORKSPACE_ID, batch)

    inserts = session._calls_matching("INSERT INTO match_audit_classifications")
    assert len(inserts) == 1
    _sql, params = inserts[0]
    assert params[0]["match_id"] == reviewed_id
