"""Application-generated UUIDv7 identifiers.

Per ``contracts/ids.md`` (SPEC-02 §21): identifiers are generated in the
application (not the database) so the id is available before flush, and
are time-ordered for good B-tree insert locality. Python 3.13 has no
stdlib ``uuid.uuid7()`` (lands in 3.14), so this wraps the pure-Python
``uuid6`` package (``uuid6>=2025.0.1,<2026``).
"""

from __future__ import annotations

import uuid

import uuid6
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column


def new_uuid7() -> uuid.UUID:
    """Return a new, time-ordered, version-7 :class:`uuid.UUID`.

    Backed by :func:`uuid6.uuid7`, which already returns a
    ``uuid.UUID``-compatible instance (``isinstance(x, uuid.UUID)`` is
    ``True``, ``x.version == 7``). Sequential calls are monotonically
    non-decreasing in string/lexicographic order.
    """
    return uuid6.uuid7()


def uuid7_pk() -> Mapped[uuid.UUID]:
    """Column factory for the standard UUIDv7 primary key.

    Returns a ``mapped_column`` configured as the table's primary key,
    stored as a Postgres native ``UUID`` and defaulted application-side
    via :func:`new_uuid7`. Optional convenience — :class:`app_shared.models.base.Base`
    wires this directly on its ``id`` column; provided here for any
    table needing an equivalent UUIDv7 column outside the PK.
    """
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=new_uuid7)
