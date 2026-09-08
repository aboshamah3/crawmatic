"""``network_operation_resource_summaries`` — what survives a browser
navigation's subresource children after they are aged out (EPA C9, F14).

A browser navigation produces one PARENT ``network_operations`` row and
one CHILD row per subresource it pulled in (``parent_operation_id``
points at the navigation). On a page with a few hundred assets the
children outnumber every other row in the ledger by an order of
magnitude, and after thirty days they answer exactly one question worth
asking: *how much did that navigation actually cost us on the wire?*

That question has an answer far smaller than the rows it is derived
from. This table is that answer: **one row per parent operation**,
written by ``app_shared.maintenance.ledger_summaries`` immediately
before the children are deleted, in the same transaction. The children
are therefore *compressed*, never merely *dropped* —
``RetentionMechanism.SUMMARIZE_THEN_DELETE``, and the distinction is the
whole point of the family existing.

## What is kept, and why exactly these four things

``child_count``
    How many physical fetches the navigation made. Without it a
    summarised parent is indistinguishable from a parent that never had
    children at all.
``bytes_by_host_class``
    A JSONB map of host class -> transport-observed bytes (``"first_party"``,
    ``"third_party"``, ``"unknown"``). Kept as a MAP rather than a single
    total because the one thing an operator actually does with this
    number months later is ask whether the spend was on the page we
    wanted or on somebody's analytics bundle — and a scalar total
    cannot be re-split once the rows are gone.
``duration_ms_sum``
    Summed child wall time. Not a mean: a mean cannot be re-aggregated
    across parents, a sum can.
``bytes_total``
    The plain scalar, denormalised alongside the map so the ordinary
    "what did this cost" read needs no JSONB arithmetic. It is always
    the sum of ``bytes_by_host_class``'s values; the summariser writes
    both from the same scan.

**The PARENT's own totals are not touched.** The parent row's
``bytes_compressed``/``bytes_decompressed``/``duration_ms`` and its
``estimated_cost_micro_units`` are immutable-by-trigger facts about the
navigation itself, and summarising its children does not restate them.
This table is additive evidence, not a replacement.

## Shape: fleet-owned, no RLS, no foreign key

Fleet-owned like the ``network_operations`` parent it describes: no
``workspace_id``, and therefore no row-level security policy — tenant
ownership of a physical fetch is a derived allocation living in
``network_operation_allocations``, exactly as
``app_shared.models.network_operations`` explains at length. Filed
``GAP`` in ``scripts/rls_table_manifest.txt`` for the same reason its
parent is: it carries no tenant column, but it is tenant-LINKED
evidence by way of the operation it names.

``parent_operation_id`` carries **no** foreign key. Two independent
reasons, either sufficient: ``network_operations`` is partitioned since
this same task, so its only UNIQUE constraint now includes
``created_at`` and cannot be the target of a single-column reference;
and a summary must outlive a parent whose own partition is eventually
dropped at 730 days — a cascade here would delete the compressed
evidence at exactly the moment it becomes the only evidence left.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, Integer, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TZDateTime

#: The host classes ``bytes_by_host_class`` is keyed by. A closed set on
#: purpose: an open-ended map keyed by raw hostname would reintroduce the
#: unbounded cardinality the summarisation exists to remove (and would
#: make the summary of a hostile page larger than the rows it replaced).
HOST_CLASS_FIRST_PARTY = "first_party"
HOST_CLASS_THIRD_PARTY = "third_party"
HOST_CLASS_UNKNOWN = "unknown"

HOST_CLASSES: tuple[str, ...] = (
    HOST_CLASS_FIRST_PARTY,
    HOST_CLASS_THIRD_PARTY,
    HOST_CLASS_UNKNOWN,
)


class NetworkOperationResourceSummary(Base):
    """``network_operation_resource_summaries`` — one row per summarised parent.

    Fleet-owned (no ``workspace_id``, no RLS); written exactly once per
    parent by ``app_shared.maintenance.ledger_summaries``. **Must not**
    be added to ``app_shared.repository.WORKSPACE_OWNED_MODELS``.

    The ``UNIQUE (parent_operation_id)`` constraint is what makes the
    summarise-then-delete step idempotent: a re-run of a parent that was
    already summarised conflicts instead of double-counting, and because
    the children were deleted in the same transaction as the insert,
    "summary exists" and "children are gone" can never disagree.
    """

    __tablename__ = "network_operation_resource_summaries"
    __table_args__ = (
        UniqueConstraint(
            "parent_operation_id", name="uq_nors_parent_operation_id"
        ),
        # Very short suffixes on purpose: the naming convention prefixes
        # `ck_network_operation_resource_summaries_`, which already spends
        # 40 of Postgres's 63 identifier bytes.
        CheckConstraint("child_count > 0", name="count_positive"),
        CheckConstraint("bytes_total >= 0", name="bytes_nonneg"),
        CheckConstraint("duration_ms_sum >= 0", name="duration_nonneg"),
    )

    #: The ``network_operations.network_request_id`` of the navigation
    #: whose children this summarises. Soft reference, no FK — see the
    #: module docstring.
    parent_operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False
    )
    #: How many child operations were folded into this row.
    child_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    #: ``{host_class: bytes}`` over :data:`HOST_CLASSES`. A class with no
    #: bytes is omitted rather than written as ``0`` — absence and zero
    #: are the same fact here and one of them costs nothing to store.
    bytes_by_host_class: Mapped[dict] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    #: ``sum(bytes_by_host_class.values())``, denormalised for the common read.
    bytes_total: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    #: Summed child ``duration_ms``. A sum, not a mean — see the module
    #: docstring.
    duration_ms_sum: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    #: The parent's own ``created_at``, copied so a summary can be aged
    #: out on the parent's clock without joining back to a table whose
    #: partition may already be gone.
    parent_created_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TZDateTime(), nullable=False, server_default=text("now()")
    )


__all__ = [
    "HOST_CLASSES",
    "HOST_CLASS_FIRST_PARTY",
    "HOST_CLASS_THIRD_PARTY",
    "HOST_CLASS_UNKNOWN",
    "NetworkOperationResourceSummary",
]
