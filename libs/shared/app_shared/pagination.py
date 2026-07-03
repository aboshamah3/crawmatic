"""Cursor pagination: opaque keyset cursor over ``(created_at, id)`` (SPEC-04).

Per ``contracts/pagination.md`` (FR-015, SC-009): pure and
framework-agnostic (no FastAPI) — the cursor encode/decode is stdlib
only (``base64``/``json``); :func:`keyset_predicate` builds a SQLAlchemy
tuple-comparison seek expression, since that is the mechanism the
catalog routers (``apps/api``) use to page a ``Select`` — SQLAlchemy
itself is framework-agnostic (no web framework dependency), unlike
FastAPI/Pydantic which stay out of ``app_shared`` entirely.

Algorithm (router-side, see contract):

1. ``limit = clamp_limit(query.limit)``.
2. If ``cursor``: ``after = decode_cursor(cursor)``; add
   ``keyset_predicate(Model, after)`` to the ``WHERE`` clause.
3. ``scoped_select(Model, ws).order_by(Model.created_at, Model.id).limit(limit + 1)``.
4. Pass the fetched rows + ``limit`` to :func:`paginate` for the
   ``{items, next_cursor}`` envelope.
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime
from typing import Any, Sequence

from sqlalchemy import ColumnElement, tuple_

DEFAULT_LIMIT = 50
MAX_LIMIT = 500


class InvalidCursor(ValueError):
    """Raised by :func:`decode_cursor` on a malformed/garbage token.

    Routers map this to a ``422`` response (contract).
    """


def clamp_limit(requested: int | None) -> int:
    """Clamp a requested page size to ``[1, MAX_LIMIT]``, defaulting to ``DEFAULT_LIMIT``.

    ``None`` -> ``DEFAULT_LIMIT``; any other value is capped at
    ``MAX_LIMIT`` and floored at ``1`` (so ``0`` and negative requests
    never yield an empty/invalid page size instead of the minimum
    sensible one).
    """
    limit = DEFAULT_LIMIT if requested is None else requested
    limit = min(limit, MAX_LIMIT)
    return max(limit, 1)


def encode_cursor(created_at: datetime, id: uuid.UUID) -> str:  # noqa: A002 - contract name
    """Encode ``(created_at, id)`` as an opaque base64url token.

    ``base64url(json({"c": created_at.isoformat(), "id": str(id)}))``,
    padding stripped (re-added on decode).
    """
    payload = json.dumps(
        {"c": created_at.isoformat(), "id": str(id)}, separators=(",", ":")
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_cursor(token: str) -> tuple[datetime, uuid.UUID]:
    """Decode a token produced by :func:`encode_cursor`.

    Raises :class:`InvalidCursor` (never a bare ``KeyError``/``ValueError``/
    decode error) on any malformed, non-base64, or wrong-shape input.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        data = json.loads(raw)
        created_at = datetime.fromisoformat(data["c"])
        id_ = uuid.UUID(data["id"])
    except (
        TypeError,
        ValueError,
        KeyError,
        binascii.Error,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise InvalidCursor(f"malformed pagination cursor: {token!r}") from exc
    return created_at, id_


def keyset_predicate(model: Any, after: tuple[datetime, uuid.UUID]) -> ColumnElement[bool]:
    """Build the ``(created_at, id) > (c, id)`` tuple-comparison seek predicate."""
    created_at, id_ = after
    return tuple_(model.created_at, model.id) > tuple_(created_at, id_)


def paginate(rows: Sequence[Any], limit: int) -> dict[str, Any]:
    """Build the ``{items, next_cursor}`` envelope from a ``limit + 1``-row fetch.

    When more than ``limit`` rows were fetched, the ``(limit + 1)``th row
    proves another page exists: ``next_cursor`` is set from the last
    *returned* item and the extra row is trimmed off. Otherwise
    ``next_cursor`` is ``None`` (SC-009).
    """
    if len(rows) > limit:
        items = list(rows[:limit])
        last = items[-1]
        next_cursor: str | None = encode_cursor(last.created_at, last.id)
    else:
        items = list(rows)
        next_cursor = None
    return {"items": items, "next_cursor": next_cursor}
