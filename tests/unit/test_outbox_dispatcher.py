"""`app_shared.outbox.dispatcher` unit tests (2026-08-15 audit risk H1).

The consumer half of the transactional outbox. What has to be true:

* the claim query is a `FOR UPDATE ... SKIP LOCKED` single-row claim, and
  every message gets its **own** session/transaction — that is what makes
  concurrent drains safe (two passes never publish the same row);
* publish happens **before** the status flip commits (at-least-once): a
  crash in that gap must replay the message, never lose it;
* a failed publish is still committed (attempt counter + backoff durable)
  so a broker outage cannot spin on one row;
* backoff grows exponentially and is capped;
* `max_attempts` turns a message into an alertable `DEAD` dead letter
  rather than retrying forever;
* a healthy pass stops at `batch_limit` and stops early when the backlog
  is empty.

All against an in-memory fake store — no Postgres, no broker.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.dialects import postgresql

from app_shared.enums import OutboxStatus
from app_shared.outbox.dispatcher import (
    MAX_BACKOFF_SECONDS,
    drain_outbox,
    next_backoff_seconds,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


class _Message:
    """Stand-in for one `OutboxMessage` row."""

    def __init__(
        self,
        *,
        task_name: str = "price_analysis.recompute_variant",
        queue: str = "price_analysis",
        payload: dict[str, Any] | None = None,
        attempts: int = 0,
        available_at: datetime = NOW,
    ) -> None:
        self.id = uuid.uuid4()
        self.workspace_id = uuid.uuid4()
        self.task_name = task_name
        self.queue = queue
        self.payload = payload if payload is not None else {"k": "v"}
        self.dedup_key = None
        self.status = OutboxStatus.PENDING
        self.attempts = attempts
        self.available_at = available_at
        self.published_at: datetime | None = None
        self.last_error: str | None = None
        self.updated_at = NOW
        self.created_at = NOW


class _Store:
    """Shared "database" behind the fake sessions.

    ``locked`` models ``FOR UPDATE ... SKIP LOCKED``: a row held by an
    uncommitted session is invisible to any other claimant, exactly as
    Postgres would behave for concurrent drain passes.
    """

    def __init__(self, messages: list[_Message]) -> None:
        self.messages = messages
        self.locked: set[uuid.UUID] = set()
        self.commits = 0
        self.sessions_opened = 0

    def claimable(self, now: datetime) -> list[_Message]:
        return sorted(
            (
                m
                for m in self.messages
                if m.status == OutboxStatus.PENDING
                and m.available_at <= now
                and m.id not in self.locked
            ),
            key=lambda m: (m.available_at, m.id),
        )


class _FakeSession:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self._held: uuid.UUID | None = None
        self.claim_statements: list[Any] = []

    # -- context manager -------------------------------------------------
    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self._release()
        return False

    def _release(self) -> None:
        if self._held is not None:
            self._store.locked.discard(self._held)
            self._held = None

    # -- session surface used by the dispatcher --------------------------
    def execute(self, statement: Any) -> "_FakeResult":
        self.claim_statements.append(statement)
        candidates = self._store.claimable(_claim_now(statement))
        if not candidates:
            return _FakeResult(None)
        message = candidates[0]
        self._store.locked.add(message.id)
        self._held = message.id
        return _FakeResult(message)

    def commit(self) -> None:
        self._store.commits += 1
        self._release()

    def rollback(self) -> None:
        self._release()


def _claim_now(statement: Any) -> datetime:
    """Recover the `available_at <= :now` bind value from the claim query."""
    for value in statement.compile().params.values():
        if isinstance(value, datetime):
            return value
    return NOW  # pragma: no cover - the claim always binds `now`


class _FakeResult:
    def __init__(self, message: _Message | None) -> None:
        self._message = message

    def scalars(self) -> "_FakeResult":
        return self

    def first(self) -> _Message | None:
        return self._message


def _factory(store: _Store):
    def make() -> _FakeSession:
        store.sessions_opened += 1
        return _FakeSession(store)

    return make


class _RecordingPublish:
    def __init__(self, fail_times: int = 0, fail_forever: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail_times = fail_times
        self._fail_forever = fail_forever

    def __call__(self, name: str, *, queue: str, kwargs: dict[str, Any] | None = None) -> None:
        self.calls.append({"name": name, "queue": queue, "kwargs": kwargs})
        if self._fail_forever or len(self.calls) <= self._fail_times:
            raise RuntimeError("simulated broker outage")


def _drain(store: _Store, publish, *, batch_limit: int = 10, max_attempts: int = 3, now=NOW):
    return drain_outbox(
        _factory(store),
        now=now,
        batch_limit=batch_limit,
        max_attempts=max_attempts,
        backoff_base_seconds=10,
        publish=publish,
    )


# --- happy path -------------------------------------------------------------


def test_publishes_pending_messages_and_marks_them_published() -> None:
    message = _Message()
    store = _Store([message])
    publish = _RecordingPublish()

    report = _drain(store, publish)

    assert report.published == 1 and report.failed == 0 and report.dead_lettered == 0
    assert publish.calls == [
        {"name": message.task_name, "queue": message.queue, "kwargs": {"k": "v"}}
    ]
    assert message.status == OutboxStatus.PUBLISHED
    assert message.published_at == NOW
    assert message.attempts == 1


def test_each_message_is_claimed_in_its_own_transaction() -> None:
    """One session (one transaction) per message — never one big batch txn.

    This is what bounds the blast radius of a mid-drain crash: everything
    already published is committed, and only the in-flight row replays.
    """
    store = _Store([_Message(), _Message(), _Message()])

    report = _drain(store, _RecordingPublish())

    assert report.published == 3
    # 3 successful claims + 1 final empty probe that ends the loop.
    assert store.sessions_opened == 4
    assert store.commits == 3


def test_claim_uses_for_update_skip_locked_and_orders_oldest_first() -> None:
    store = _Store([_Message()])
    session_seen: list[Any] = []

    def factory() -> _FakeSession:
        session = _FakeSession(store)
        session_seen.append(session)
        return session

    drain_outbox(
        factory,
        now=NOW,
        batch_limit=1,
        max_attempts=3,
        backoff_base_seconds=10,
        publish=_RecordingPublish(),
    )

    # `SKIP LOCKED` only renders on the postgresql dialect -- compiling
    # with the default dialect would silently drop it and make this
    # assertion vacuous.
    sql = str(session_seen[0].claim_statements[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql
    assert "SKIP LOCKED" in sql
    assert "ORDER BY" in sql
    assert "LIMIT" in sql


def test_a_locked_row_is_skipped_by_a_concurrent_pass() -> None:
    """Concurrency safety: two overlapping drains never publish one row twice."""
    message = _Message()
    store = _Store([message])
    store.locked.add(message.id)  # another pass holds it

    report = _drain(store, _RecordingPublish())

    assert report.claimed == 0 and report.published == 0


def test_batch_limit_bounds_one_pass() -> None:
    store = _Store([_Message() for _ in range(5)])

    report = _drain(store, _RecordingPublish(), batch_limit=2)

    assert report.claimed == 2 and report.published == 2
    assert sum(1 for m in store.messages if m.status == OutboxStatus.PENDING) == 3


def test_messages_not_yet_available_are_not_claimed() -> None:
    store = _Store([_Message(available_at=NOW + timedelta(seconds=30))])

    report = _drain(store, _RecordingPublish())

    assert report.claimed == 0


# --- at-least-once ----------------------------------------------------------


def test_publish_happens_before_the_status_flip_is_committed() -> None:
    """Ordering IS the at-least-once guarantee.

    If the process died between `send_task` and `COMMIT`, the row must
    still be PENDING so the next pass republishes it (a duplicate the
    consumer absorbs), rather than PUBLISHED-but-never-sent (a permanent
    loss). Modelled by a publish that inspects the row it was handed.
    """
    message = _Message()
    store = _Store([message])
    observed: list[Any] = []

    def publish(name: str, *, queue: str, kwargs: dict[str, Any] | None = None) -> None:
        observed.append((message.status, message.published_at, store.commits))

    _drain(store, publish)

    status_at_publish, published_at_publish, commits_at_publish = observed[0]
    assert status_at_publish == OutboxStatus.PENDING
    assert published_at_publish is None
    assert commits_at_publish == 0


def test_crash_between_publish_and_commit_replays_the_message() -> None:
    """The replay itself: a hard failure after publishing leaves it PENDING."""
    message = _Message()
    store = _Store([message])
    publish = _RecordingPublish()

    class _Boom(Exception):
        pass

    def exploding_publish(name: str, *, queue: str, kwargs: dict[str, Any] | None = None) -> None:
        publish(name, queue=queue, kwargs=kwargs)
        raise _Boom("process died after send_task, before commit")

    # The dispatcher treats any publish exception as a failed attempt: the
    # row stays PENDING and is retried, which is exactly the replay
    # behaviour a real crash produces.
    _drain(store, exploding_publish)
    assert message.status == OutboxStatus.PENDING

    # Second pass (after the backoff window) delivers it again -- the
    # message is never lost, and the duplicate is the consumer's problem
    # to absorb idempotently.
    later = NOW + timedelta(seconds=3600)
    _drain(store, publish, now=later)
    assert message.status == OutboxStatus.PUBLISHED
    assert len(publish.calls) == 2


# --- failure handling -------------------------------------------------------


def test_failed_publish_is_committed_with_attempt_and_backoff() -> None:
    message = _Message()
    store = _Store([message])

    report = _drain(store, _RecordingPublish(fail_forever=True), batch_limit=1)

    assert report.failed == 1 and report.published == 0
    assert message.status == OutboxStatus.PENDING
    assert message.attempts == 1
    assert message.available_at == NOW + timedelta(seconds=10)
    assert "simulated broker outage" in (message.last_error or "")
    # Durable: the attempt/backoff was committed, so a broker outage does
    # not spin on the same row.
    assert store.commits == 1


def test_backoff_grows_exponentially_and_is_capped() -> None:
    assert next_backoff_seconds(1, base_seconds=10) == 10
    assert next_backoff_seconds(2, base_seconds=10) == 20
    assert next_backoff_seconds(3, base_seconds=10) == 40
    assert next_backoff_seconds(99, base_seconds=10) == MAX_BACKOFF_SECONDS
    assert next_backoff_seconds(0, base_seconds=10) == 10


def test_max_attempts_dead_letters_instead_of_retrying_forever() -> None:
    message = _Message(attempts=2)  # one attempt away from the limit
    store = _Store([message])

    report = _drain(store, _RecordingPublish(fail_forever=True), max_attempts=3, batch_limit=1)

    assert report.dead_lettered == 1
    assert message.status == OutboxStatus.DEAD
    assert message.attempts == 3
    assert message.last_error is not None
    assert store.commits == 1


def test_a_dead_message_is_never_claimed_again() -> None:
    message = _Message(attempts=2)
    store = _Store([message])
    _drain(store, _RecordingPublish(fail_forever=True), max_attempts=3, batch_limit=1)
    assert message.status == OutboxStatus.DEAD

    publish = _RecordingPublish()
    report = _drain(store, publish, now=NOW + timedelta(days=1))

    assert report.claimed == 0
    assert publish.calls == []


def test_a_transient_failure_recovers_on_a_later_pass() -> None:
    message = _Message()
    store = _Store([message])
    publish = _RecordingPublish(fail_times=1)

    _drain(store, publish, batch_limit=1)
    assert message.status == OutboxStatus.PENDING

    _drain(store, publish, now=NOW + timedelta(seconds=60), batch_limit=1)
    assert message.status == OutboxStatus.PUBLISHED
    assert message.attempts == 2
    assert message.last_error is None
