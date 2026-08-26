"""``api_abuse_limit_counters`` ORM model -- the fail-closed abuse-limit
counter store behind ``apps/api/app/abuse_limit.py`` (EPA W5.5-L1 item 2).

See :mod:`app.abuse_limit` for the full rationale (why this store is
Postgres-authoritative and fail-CLOSED, unlike the fail-open
``app.rate_limit``) and for every actual read/write, which go through raw
:func:`sqlalchemy.text` statements guarded by a ``to_regclass`` capability
probe -- **not** through this model, per the W5.5-L2 ``rollup_watermarks``
precedent (:class:`app_shared.models.rollup_watermarks.RollupWatermark`).
This module exists so :data:`app_shared.models.metadata` sees the table for
Alembic autogenerate/offline-render (``target_metadata``); nothing in the
runtime path imports or instantiates :class:`ApiAbuseLimitCounter`.

## Shape: global, no RLS, keyed by its own natural key

Deliberately **no** ``workspace_id`` and **no** RLS: the bucket key is a
salted-free ``sha256`` of a credential, not a workspace, and the limiter
must be able to count an attempt made by a caller whose workspace is not
yet resolved (the middleware runs BEFORE the auth seam). See
``scripts/rls_table_manifest.txt``'s entry for the full non-tenant
reasoning.

Like ``RollupWatermark``, this table's own migration
(``e92029e9902c_abuse_limit_counters``) makes the natural key --
``(bucket_key, window_starts_at)`` -- the primary key, with no surrogate
``id`` column at all. ``Base.id`` is therefore suppressed below (``id =
None``) rather than left in place as a second, physically-nonexistent PK
member. There is also no ``TimestampMixin``: the physical table carries
no ``created_at``/``updated_at`` columns -- rows expire by becoming
unreachable as the window moves on (see the RETENTION reasoning in the
migration), not by being touched again after insert.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TZDateTime


class ApiAbuseLimitCounter(Base):
    """``api_abuse_limit_counters`` -- one row per (bucket, window).

    Global (no ``workspace_id``, no RLS) -- see the module docstring.
    Not workspace-owned: **must not** be added to
    ``app_shared.repository.WORKSPACE_OWNED_MODELS``.
    """

    __tablename__ = "api_abuse_limit_counters"

    # This table's natural key IS its identity -- see the module
    # docstring. Suppress Base's inherited UUIDv7 `id`: the physical
    # table (migration e92029e9902c) has no `id` column at all.
    id = None  # type: ignore[assignment]

    #: ``abuse:<surface>:<sha256(credential)[:32]>`` -- never a raw credential.
    bucket_key: Mapped[str] = mapped_column(Text(), primary_key=True)
    #: Start of this counter's fixed window (see
    #: ``app.abuse_limit.window_start_for``).
    window_starts_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)
    #: Attempts counted in this window. Starts at 1 -- the attempt that
    #: creates the row is itself an attempt.
    count: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)


__all__ = ["ApiAbuseLimitCounter"]
