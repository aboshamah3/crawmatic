"""Redis-cached user/workspace status checks (`contracts/security-cache.md`, FR-022).

Framework-agnostic: takes a sync ``redis.Redis``-shaped client and a
``session_factory`` (a zero-arg callable returning a context manager that
yields a SQLAlchemy ``Session`` — e.g. ``app_shared.database.get_auth_session``).
This lookup runs during the authentication dependency, *before* any
workspace context has been established for the request, so the caller is
expected to pass the BYPASSRLS ``get_auth_session`` factory — the same
pre-context credential-resolution boundary as the login/api-key lookups
(research D4). The user-by-id lookup is therefore a sanctioned unscoped
``User`` access (annotated ``# noqa: workspace-scope``), analogous to the
existing pre-auth email/prefix lookups.

**Hit** → return the cached status string, no DB read. **Miss** → a
single DB read, then repopulate the cache with
``Settings.STATUS_CACHE_TTL_SECONDS`` TTL → steady-state requests are
cache hits (0 per-request status DB reads, FR-022/SC-007).

**Fail-safe**: any Redis error, or a missing row, returns
:data:`STATUS_UNAVAILABLE` — callers must treat anything other than the
literal ``"active"`` string as not-active (deny), never assume active on
a failure.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app_shared.config import get_settings
from app_shared.models import User, Workspace

SessionFactory = Callable[[], AbstractContextManager[Session]]

# Sentinel returned on any failure (Redis error, missing row) or an
# explicitly non-active status read from the DB. Deliberately never
# equal to any real status value ("active"/"suspended") so callers'
# ``status == "active"`` checks fail safe to deny.
STATUS_UNAVAILABLE = "unavailable"


def _cache_get(redis: object, key: str) -> str | None:
    try:
        value = redis.get(key)  # type: ignore[attr-defined]
    except Exception:
        return None
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _cache_set(redis: object, key: str, value: str, *, ttl_seconds: int) -> None:
    try:
        redis.set(key, value, ex=ttl_seconds)  # type: ignore[attr-defined]
    except Exception:
        # Best-effort repopulate — the freshly-read value is still
        # returned to the caller for this request even if the write
        # itself fails.
        pass


def _get_cached_status(
    redis: object,
    session_factory: SessionFactory,
    cache_key: str,
    loader: Callable[[Session], str | None],
) -> str:
    cached = _cache_get(redis, cache_key)
    if cached is not None:
        return cached

    try:
        with session_factory() as session:
            status = loader(session)
    except Exception:
        return STATUS_UNAVAILABLE

    if status is None:
        return STATUS_UNAVAILABLE

    ttl_seconds = get_settings().STATUS_CACHE_TTL_SECONDS
    _cache_set(redis, cache_key, status, ttl_seconds=ttl_seconds)
    return status


def get_user_status(redis: object, session_factory: SessionFactory, user_id: object) -> str:
    """Return the cached (or freshly-read) status string for user ``user_id``."""

    def _load(session: Session) -> str | None:
        user = session.execute(
            select(User).where(User.id == user_id)  # noqa: workspace-scope
        ).scalar_one_or_none()
        return str(user.status) if user is not None else None

    return _get_cached_status(redis, session_factory, f"status:user:{user_id}", _load)


def get_workspace_status(
    redis: object, session_factory: SessionFactory, workspace_id: object
) -> str:
    """Return the cached (or freshly-read) status string for workspace ``workspace_id``."""

    def _load(session: Session) -> str | None:
        # Workspace is the tenant root — never in WORKSPACE_OWNED_MODELS,
        # no workspace_id predicate applies to it.
        workspace = session.execute(
            select(Workspace).where(Workspace.id == workspace_id)
        ).scalar_one_or_none()
        return str(workspace.status) if workspace is not None else None

    return _get_cached_status(
        redis, session_factory, f"status:ws:{workspace_id}", _load
    )


def invalidate_user(redis: object, user_id: object) -> None:
    """Clear the cached status for ``user_id`` for immediate propagation on suspend."""
    try:
        redis.delete(f"status:user:{user_id}")  # type: ignore[attr-defined]
    except Exception:
        pass


def invalidate_workspace(redis: object, workspace_id: object) -> None:
    """Clear the cached status for ``workspace_id`` for immediate propagation on suspend."""
    try:
        redis.delete(f"status:ws:{workspace_id}")  # type: ignore[attr-defined]
    except Exception:
        pass
