"""Typed competitor identifier ORM model: ``match_competitor_identifiers`` (EPA B4).

A **child table**, not more columns on ``competitor_product_matches``.
The legacy ``competitor_variant_identifier`` is one untyped text slot, so
a match could hold exactly one identifier and nothing recorded what kind
of thing it was. The Shopify adapter read it as a ``variants[].id``; on
S-Tech it is in fact a product handle, a barcode, or a supplier SKU, and
26 of 30 canary targets were declared ``NOT_LISTED`` off healthy HTTP 200
product JSON (2026-08-24, ``PRODUCTION_READINESS_REPORT_2026-08-24.md``).

One row per (type, value) means a match can simultaneously carry a
handle **and** a SKU **and** a barcode **and** a variant id, each with
its own provenance (``source``), its own trust (``verified_at`` /
``confidence``) and its own validity window (``effective_from`` /
``effective_to``). History is kept by closing a row (setting
``effective_to``), never by overwriting one — so "what did we think this
match's identity was last month, and on what evidence?" stays answerable.

``competitor_product_matches.canonical_variant_ref`` (nullable) points at
the single identifier row currently selected as *the* variant identity.
Nullable on purpose: "we do not yet know which identifier is canonical"
is a real and common state, and inventing one is the bug this table
exists to fix.

The legacy ``competitor_variant_identifier`` column is **retained,
read-only** — it is the rollback anchor for
``scripts/migrate_stech_identifiers.py`` and the audit trail for every
typed row derived from it. Nothing writes it any more.

**No** ``workspace_id`` column of its own (the same shape as
:class:`app_shared.models.match_audit.MatchAuditClassification` and
``strategy_attempt_stats``): isolation is anchored *transitively*
through the real FK to ``competitor_product_matches`` (itself
workspace-owned) via
:func:`app_shared.models.rls.emit_fk_transitive_rls_policy` in the
creating Alembic migration, not here. Deliberately **excluded** from
``app_shared.repository.WORKSPACE_OWNED_MODELS`` for the same reason a
``scoped_select`` cannot scope a column that does not exist — query it
joined to ``competitor_product_matches``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKeyConstraint, Index, Numeric, Text, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import (
    CompetitorIdentifierSource,
    CompetitorIdentifierType,
    enum_column,
)
from app_shared.models.base import Base, TZDateTime

__all__ = ["MatchCompetitorIdentifier"]


class MatchCompetitorIdentifier(Base):
    """``match_competitor_identifiers`` — one typed identifier for one match.

    * ``match_id`` — plain, single-column FK to
      ``competitor_product_matches.id`` (the parent's own composite
      ``(workspace_id, id)`` uniqueness makes a plain FK sufficient for
      referential integrity; RLS isolation is transitive via this FK).
      ``ON DELETE CASCADE``: an identifier has no meaning without its
      match.
    * ``identifier_type`` — :class:`CompetitorIdentifierType`. Assigned
      from **evidence** (the value was found in that field of a real
      fetched product JSON), never from a regex or a string length.
      ``UNKNOWN`` is a first-class answer, not a failure to try.
    * ``value`` — the identifier text exactly as the store renders it.
    * ``source`` — :class:`CompetitorIdentifierSource` provenance.
    * ``verified_at`` — when this value was last confirmed present in a
      live store response. ``NULL`` means "never confirmed"; only a
      confirmed identifier's later disappearance is evidence about the
      *store* rather than about the string.
    * ``confidence`` — ``NUMERIC(5,4)`` in ``[0,1]``, mirroring
      ``competitor_product_matches.success_rate_7d``'s shape. Not money,
      so plain ``Numeric``, not ``app_shared.money.Money``.
    * ``effective_from`` / ``effective_to`` — the validity window.
      ``effective_to IS NULL`` marks the currently-believed rows, which
      is what ``uq_mci_match_type_value_current`` enforces uniqueness
      over: a match may hold at most one *current* row per (type, value),
      while any number of closed historical rows for the same pair are
      welcome.
    """

    __tablename__ = "match_competitor_identifiers"
    __table_args__ = (
        ForeignKeyConstraint(
            ["match_id"],
            ["competitor_product_matches.id"],
            name="fk_mci_match_id_competitor_product_matches",
            ondelete="CASCADE",
        ),
        Index("ix_mci_match_id", "match_id"),
        # The hot lookup: "every currently-believed identifier for this
        # match", and the backfill's own idempotency probe.
        Index(
            "ix_mci_match_id_current",
            "match_id",
            postgresql_where=text("effective_to IS NULL"),
        ),
        # Reverse lookup: "which match(es) claim this value?" — used by
        # the backfill to spot a value shared across matches.
        Index("ix_mci_type_value", "identifier_type", "value"),
        Index(
            "uq_mci_match_type_value_current",
            "match_id",
            "identifier_type",
            "value",
            unique=True,
            postgresql_where=text("effective_to IS NULL"),
        ),
    )

    match_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    identifier_type: Mapped[CompetitorIdentifierType] = enum_column(
        CompetitorIdentifierType, nullable=False
    )
    value: Mapped[str] = mapped_column(Text(), nullable=False)
    source: Mapped[CompetitorIdentifierSource] = enum_column(
        CompetitorIdentifierSource, nullable=False
    )
    verified_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(precision=5, scale=4), nullable=True
    )
    effective_from: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    effective_to: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
