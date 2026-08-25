"""Match audit classification ORM model: ``match_audit_classifications`` (EPA A6).

A **sidecar**, not a column on ``competitor_product_matches`` — audit
classifications are versioned facts (supersede, never overwrite), so the
matches table stays untouched. One row per classification decision;
``superseded_at IS NULL`` marks the current row for a given ``match_id``
(app-enforced append-only invariant — a new classification is always an
INSERT of a fresh row plus an UPDATE of exactly one column
(``superseded_at``) on the row it replaces, never a mutation of
``state``/``evidence`` in place).

**No** ``workspace_id`` column of its own (mirrors
:class:`app_shared.models.strategy.StrategyAttemptStats`, research D3):
isolation is anchored *transitively* through the real FK to
``competitor_product_matches`` (itself workspace-owned) via
:func:`app_shared.models.rls.emit_fk_transitive_rls_policy` in the
creating Alembic migration, not here. **Excluded** from
``app_shared.repository.WORKSPACE_OWNED_MODELS`` for the same reason a
``scoped_select``/``scoped_get`` can't scope a column that doesn't
exist — query only joined to ``competitor_product_matches``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import ForeignKeyConstraint, Index, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import MatchClassificationState, enum_column
from app_shared.models.base import Base, TZDateTime


class MatchAuditClassification(Base):
    """``match_audit_classifications`` — one versioned audit verdict per match.

    * ``match_id`` — soft-anchored FK to ``competitor_product_matches.id``
      (plain, single-column — the parent's own composite
      ``(workspace_id, id)`` uniqueness makes a plain FK here sufficient
      for referential integrity; RLS isolation is transitive via this FK,
      not via a workspace-local composite).
    * ``state`` — :class:`MatchClassificationState` (``ACTIVE`` /
      ``CONFIRMED_DELISTED`` / ``INVALID_IDENTITY`` / ``UNKNOWN``).
    * ``classifier_version`` — plain text version tag (e.g. ``"1"``) so a
      later rule-set change never silently reinterprets old evidence.
    * ``evidence`` — JSONB bag of the facts that produced ``state``
      (observation ids, timestamps, error codes, fetch metadata) —
      never trusted blind, always re-derivable from ``evidence`` alone.
    * ``reviewer`` — nullable free-text identifying the human/process
      that produced or last touched the row (``NULL`` for a fully
      automated classification run).
    * ``effective_at`` — when this classification became the current
      one. ``superseded_at`` — ``NULL`` while current; set (once, only
      on the OLD row) the instant a newer classification supersedes it.
      Append-only: no other column is ever updated after insert.

    ``uq_mac_match_id_current`` is the single-current-row invariant: a
    partial unique index on ``match_id`` WHERE ``superseded_at IS
    NULL`` — at most one non-superseded classification per match at any
    time, enforced by the database, not just application discipline.
    """

    __tablename__ = "match_audit_classifications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["match_id"],
            ["competitor_product_matches.id"],
            name="fk_mac_match_id_competitor_product_matches",
        ),
        Index("ix_mac_match_id", "match_id"),
        Index(
            "uq_mac_match_id_current",
            "match_id",
            unique=True,
            postgresql_where=text("superseded_at IS NULL"),
        ),
    )

    match_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    state: Mapped[MatchClassificationState] = enum_column(
        MatchClassificationState, nullable=False
    )
    classifier_version: Mapped[str] = mapped_column(Text(), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB(), nullable=False, default=dict)
    reviewer: Mapped[str | None] = mapped_column(Text(), nullable=True)
    effective_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    superseded_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
