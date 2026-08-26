"""``provider_usage_records`` — raw provider usage evidence (EPA C5, READY-005 part 2).

C1 built the physical ledger; C4 populates it from the fleet's own
transport observations. Neither of them knows what the *provider*
(DataImpulse today, any metered provider tomorrow) says it billed —
and a reconciliation that only ever looks at one side of an invoice
dispute is not a reconciliation. This module is the other side: the
durable, immutable landing zone for a provider's own usage export,
imported by :mod:`app_shared.netledger.reconcile` /
``scripts/import_dataimpulse_usage.py``.

Design, in one sentence: **a provider export is a fact, not an input to
be merged away.** Every row of an imported export becomes its own
``provider_usage_records`` row, unedited, alongside the parsed columns
used for reconciliation. A later, different export for an overlapping
window (a correction, a wider pull, a re-export with finer granularity)
is a SECOND set of rows with a different ``source_hash`` — it never
overwrites or deletes the first. Reconciliation
(:func:`app_shared.netledger.reconcile.reconcile_window`) is what
decides, later and separately, how conflicting evidence is resolved (by
appending a new ``network_operation_settlements`` version,
``method=CORRECTION`` — see that module).

Idempotent import, not idempotent facts
----------------------------------------
Re-running the SAME export file must not duplicate rows: ``source_hash``
(a ``sha256:`` digest over the exact bytes the importer read) plus
``row_ordinal`` (the row's 0-based position within that file) are
UNIQUE together, so a re-import of byte-identical input is a no-op at
the database level, while any genuinely different file — even one
covering the identical time window — gets a new hash and lands as new
rows. This is the same "content hash de-dupes, content addresses the
version" shape ``NETWORK_OPERATION_SETTLEMENTS_APPEND_ONLY_SQL``'s
caller-side version bumping relies on, just one layer earlier.

Append-only, enforced by trigger
---------------------------------
UPDATE and DELETE are both rejected by
:data:`PROVIDER_USAGE_RECORDS_APPEND_ONLY_SQL`, the identical pattern
:data:`app_shared.models.network_operations.
NETWORK_OPERATION_SETTLEMENTS_APPEND_ONLY_SQL` uses: "append-only" is a
database property here, not an application convention that a bug or an
ad-hoc `UPDATE ... WHERE` could quietly violate.

Fleet-owned, no ``workspace_id``
----------------------------------
A provider usage row is billed against a **provider account**, not a
workspace — the same shape as :class:`~app_shared.models.
network_operations.NetworkOperation`. It carries no ``workspace_id`` at
all. Unlike ``fleet_cost_budgets``/``domain_playbooks`` (filed SYSTEM in
``scripts/rls_table_manifest.txt``: no tenant-identifying column
whatsoever), this table's ``target_host`` is exactly the kind of
column that got ``network_operations`` filed **GAP** instead of SYSTEM —
a domain name is tenant-linked evidence even when no single tenant
row-level-owns it, because it reveals what is being scraped and how
heavily. This table is filed GAP in the manifest for the identical
reason, not SYSTEM: see that file's ``network_operations`` entry for the
contrast this decision deliberately mirrors.

Credential-adjacent columns
-----------------------------
The DataImpulse export runbook
(``/srv/crawmatic/evidence/canary-2026-08-24/
OWNER_RUNBOOK_dataimpulse_export.md``) warns that a provider export MAY
carry proxy **sub-user / login identifiers** — credential-**adjacent**,
though not themselves passwords or tokens. ``sub_user`` /
``pool_identifier`` exist to carry those identifiers because they are
useful correlation keys; the importer (``scripts/
import_dataimpulse_usage.py``) MUST NEVER accept a column containing an
actual password or bearer token into this table — that is a data-entry
contract on the importer, not something this schema can enforce.

``raw_row`` (JSONB) keeps the ENTIRE original row exactly as read,
untouched by the parser's column mapping — the immutable-evidence
guarantee the module docstring promises would be hollow if the typed
columns were the only surviving record of what the provider actually
said.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import enum_column
from app_shared.models.base import Base, TZDateTime


class ProviderUsageGranularity(StrEnum):
    """The finest time resolution one imported row represents.

    Mirrors the runbook's "choose the finest granularity available"
    instruction: ``PER_REQUEST`` when the provider's export is a
    per-request log, ``HOURLY``/``DAILY`` when only an aggregate report
    is available. ``request_count`` on an aggregate row is the number of
    billed requests THAT ROW represents, not always 1.
    """

    PER_REQUEST = "PER_REQUEST"
    HOURLY = "HOURLY"
    DAILY = "DAILY"


class ProviderUsageRecord(Base):
    """``provider_usage_records`` — one immutable line of a provider's usage export.

    See the module docstring for the full rationale. ``window_id`` groups
    every row of ONE import together (one file, one ``source_hash``); it
    is generated by the importer, not by the provider, and is what
    :func:`app_shared.netledger.reconcile.reconcile_window` re-reads a
    window's rows by.
    """

    __tablename__ = "provider_usage_records"
    __table_args__ = (
        UniqueConstraint(
            "source_hash", "row_ordinal", name="uq_pur_source_hash_row_ordinal"
        ),
        CheckConstraint(
            "bytes_up IS NULL OR bytes_up >= 0", name="pur_bytes_up_non_negative"
        ),
        CheckConstraint(
            "bytes_down IS NULL OR bytes_down >= 0", name="pur_bytes_down_non_negative"
        ),
        CheckConstraint("total_bytes >= 0", name="pur_total_bytes_non_negative"),
        CheckConstraint("request_count >= 0", name="pur_request_count_non_negative"),
        Index(
            "ix_pur_provider_account_window_start",
            "provider",
            "provider_account",
            "window_start",
        ),
        Index("ix_pur_window_id", "window_id"),
        Index("ix_pur_occurred_at", "occurred_at"),
    )

    #: Groups every row of one import. Generated by the importer
    #: (:func:`app_shared.netledger.reconcile.import_provider_usage`),
    #: not sourced from the provider.
    window_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    #: 0-based position of this row within its source file — half of the
    #: idempotency key alongside ``source_hash``.
    row_ordinal: Mapped[int] = mapped_column(Integer(), nullable=False)
    provider: Mapped[str] = mapped_column(Text(), nullable=False)
    provider_account: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: The EXPORT's stated window (same for every row of one import) —
    #: distinct from ``occurred_at``, which is this ROW's own timestamp
    #: when the granularity is per-request.
    window_start: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    window_end: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    granularity: Mapped[ProviderUsageGranularity] = enum_column(
        ProviderUsageGranularity, nullable=False
    )
    occurred_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    target_host: Mapped[str | None] = mapped_column(Text(), nullable=True)
    bytes_up: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    bytes_down: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    total_bytes: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    #: How many billed requests THIS ROW represents (>1 on an
    #: hourly/daily aggregate row; ordinarily 1 on a per-request row).
    request_count: Mapped[int] = mapped_column(
        BigInteger(), nullable=False, server_default=text("1")
    )
    http_status: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    success: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    #: Credential-ADJACENT (an identifier, never a secret) — see the
    #: module docstring's warning to the importer.
    sub_user: Mapped[str | None] = mapped_column(Text(), nullable=True)
    pool_identifier: Mapped[str | None] = mapped_column(Text(), nullable=True)
    country: Mapped[str | None] = mapped_column(Text(), nullable=True)
    billing_line_item: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: The row exactly as read, before any column mapping — the actual
    #: immutable-evidence guarantee.
    raw_row: Mapped[dict] = mapped_column(JSONB(), nullable=False)
    #: Where this row came from (a file path today; a report name is also
    #: valid for a non-file source), for a human tracing a figure back.
    source_ref: Mapped[str] = mapped_column(Text(), nullable=False)
    #: ``sha256:<hex>`` over the exact bytes of the source export. Half
    #: of the idempotency key; also what lets two rows be proven to come
    #: from the same physical file even when ``source_ref`` (a path) has
    #: moved.
    source_hash: Mapped[str] = mapped_column(Text(), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        TZDateTime(), nullable=False, server_default=text("now()")
    )


#: Append-only, identical pattern to
#: ``NETWORK_OPERATION_SETTLEMENTS_APPEND_ONLY_SQL`` — a provider export
#: is evidence, and evidence that can be quietly edited after the fact is
#: not evidence.
PROVIDER_USAGE_RECORDS_APPEND_ONLY_SQL = """
CREATE OR REPLACE FUNCTION provider_usage_records_reject_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION USING
        ERRCODE = '23514',
        MESSAGE = 'provider_usage_records is append-only',
        DETAIL  = 'UPDATE and DELETE are rejected. A different export (a correction, '
                  'a wider pull) lands as new rows under its own source_hash; '
                  'reconciliation resolves conflicting evidence by appending a new '
                  'network_operation_settlements version, never by editing this table.';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_provider_usage_records_append_only
BEFORE UPDATE OR DELETE ON provider_usage_records
FOR EACH ROW EXECUTE FUNCTION provider_usage_records_reject_mutation();
"""

#: Dropped by the creating migration's downgrade.
PROVIDER_USAGE_RECORDS_DROP_SQL = """
DROP TRIGGER IF EXISTS trg_provider_usage_records_append_only ON provider_usage_records;
DROP FUNCTION IF EXISTS provider_usage_records_reject_mutation();
"""
