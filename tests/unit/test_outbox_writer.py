"""`app_shared.outbox.writer` unit tests (2026-08-15 audit risk H1).

The producer half of the transactional outbox. What has to be true:

1. the message is written through the **caller's** session, as a single
   INSERT, with no commit/flush of its own — that is what puts it in the
   caller's transaction;
2. a caller that rolls back leaves **no** orphan message (the property
   the old post-commit `enqueue` could not provide in reverse: it could
   emit a message for work that never committed);
3. the insert is `ON CONFLICT DO NOTHING` against the partial unique
   index, so a duplicate logical message can never abort the caller's
   domain transaction;
4. no `dedup_key` means no conflict clause at all (a plain insert).

Exercised against a recording fake session + SQLAlchemy statement
compilation — no Postgres, no broker. Statement *shape* is asserted by
compiling against the postgresql dialect, which is what makes assertions
about `ON CONFLICT`/`WHERE` meaningful without a live server.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.dialects import postgresql

from app_shared.enums import OutboxStatus
from app_shared.models.outbox import OutboxMessage
from app_shared.outbox import write_outbox_message

WORKSPACE_ID = uuid.uuid4()
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


class _RecordingSession:
    """Minimal `Session` stand-in that records executed statements.

    Deliberately has no working ``commit``: if the writer ever tried to
    commit or flush on its own, these tests would fail loudly rather than
    silently accept a message that escaped the caller's transaction.
    """

    def __init__(self) -> None:
        self.statements: list[Any] = []
        self.committed = False
        self.rolled_back = False

    def execute(self, statement: Any) -> None:
        self.statements.append(statement)

    def commit(self) -> None:  # pragma: no cover - must never be called
        raise AssertionError("write_outbox_message must not commit the caller's session")

    def rollback(self) -> None:
        self.rolled_back = True
        self.statements.clear()


def _compiled(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def _params(statement: Any) -> dict[str, Any]:
    compiled = statement.compile(dialect=postgresql.dialect())
    return dict(compiled.params)


def test_write_goes_through_the_callers_session_as_one_insert() -> None:
    session = _RecordingSession()

    message_id = write_outbox_message(
        session,
        workspace_id=WORKSPACE_ID,
        task_name="maintenance.finalize_jobs",
        queue="maintenance",
        kwargs={"a": 1},
        now=NOW,
    )

    assert len(session.statements) == 1
    sql = _compiled(session.statements[0])
    assert sql.startswith("INSERT INTO outbox_messages")
    assert isinstance(message_id, uuid.UUID)


def test_written_row_is_pending_available_now_and_unattempted() -> None:
    session = _RecordingSession()

    write_outbox_message(
        session,
        workspace_id=WORKSPACE_ID,
        task_name="price_analysis.recompute_variant",
        queue="price_analysis",
        kwargs={"product_variant_id": "v"},
        now=NOW,
    )

    params = _params(session.statements[0])
    assert params["status"] == OutboxStatus.PENDING.value
    assert params["attempts"] == 0
    assert params["available_at"] == NOW
    assert params["published_at"] is None
    assert params["last_error"] is None
    assert params["queue"] == "price_analysis"
    assert params["task_name"] == "price_analysis.recompute_variant"
    assert params["payload"] == {"product_variant_id": "v"}
    assert params["workspace_id"] == WORKSPACE_ID


def test_workspace_id_accepts_a_string_and_is_normalised_to_uuid() -> None:
    session = _RecordingSession()

    write_outbox_message(
        session,
        workspace_id=str(WORKSPACE_ID),
        task_name="t",
        queue="q",
        now=NOW,
    )

    assert _params(session.statements[0])["workspace_id"] == WORKSPACE_ID


def test_dedup_key_renders_on_conflict_do_nothing_on_the_partial_index() -> None:
    session = _RecordingSession()

    write_outbox_message(
        session,
        workspace_id=WORKSPACE_ID,
        task_name="webhook_events.create_webhook_event",
        queue="webhook_events",
        dedup_key="job:abc:COMPLETED",
        now=NOW,
    )

    sql = _compiled(session.statements[0])
    assert "ON CONFLICT" in sql
    assert "DO NOTHING" in sql
    # Arbiter must be the *partial* index, or Postgres cannot infer it and
    # a duplicate would raise instead of being ignored -- which would abort
    # the caller's domain transaction.
    assert "workspace_id" in sql and "dedup_key" in sql
    assert "WHERE status = 'PENDING'" in sql


def test_no_dedup_key_means_a_plain_insert_with_no_conflict_clause() -> None:
    session = _RecordingSession()

    write_outbox_message(
        session,
        workspace_id=WORKSPACE_ID,
        task_name="t",
        queue="q",
        now=NOW,
    )

    assert "ON CONFLICT" not in _compiled(session.statements[0])


def test_rollback_leaves_no_orphan_message() -> None:
    """The rollback half of "same transaction".

    A producer that fails after recording its message must not leave the
    message behind — otherwise the dispatcher would publish work for a
    domain change that never happened. Here the fake session models the
    transaction boundary directly: rolling back discards everything the
    writer put in it, and no separate connection/commit was ever used
    (the writer's `commit` would have raised).
    """
    session = _RecordingSession()

    write_outbox_message(
        session,
        workspace_id=WORKSPACE_ID,
        task_name="webhook_events.create_webhook_event",
        queue="webhook_events",
        dedup_key="alert:1:CREATED:api",
        now=NOW,
    )
    assert session.statements  # recorded inside the transaction

    session.rollback()

    assert session.statements == []
    assert session.committed is False


def test_model_declares_the_partial_unique_dedup_index() -> None:
    """The writer's ON CONFLICT arbiter must exist as a real index."""
    indexes = {index.name: index for index in OutboxMessage.__table__.indexes}

    dedup = indexes["ix_outbox_messages_pending_dedup"]
    assert dedup.unique is True
    assert [column.name for column in dedup.columns] == ["workspace_id", "dedup_key"]
    assert "PENDING" in str(dedup.dialect_options["postgresql"]["where"])

    claim = indexes["ix_outbox_messages_claim"]
    assert claim.unique is False
    assert [column.name for column in claim.columns] == ["available_at", "id"]
    assert "PENDING" in str(claim.dialect_options["postgresql"]["where"])
