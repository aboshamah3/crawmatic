"""Workspace-namespaced Redis key builders (contracts/rate-limiter.md,
contracts/match-lock.md; FR-002/003/009/010).

Pure stdlib string formatting — no Redis import, no I/O. Every key
family is prefixed with ``workspace_id`` immediately after the family
prefix so two workspaces on the same domain never share a bucket,
semaphore, or lock (Principle II, US1 AS5).
"""

from __future__ import annotations

import uuid

from app_shared.enums import AccessMethod

__all__ = ["match_lock_key", "rate_key", "semaphore_key"]


def rate_key(workspace_id: uuid.UUID | str, domain: str, access_method: AccessMethod) -> str:
    """Token-bucket key: ``rate:{workspace_id}:{domain}:{ACCESS_METHOD}`` (FR-002)."""
    return f"rate:{workspace_id}:{domain}:{access_method.value}"


def semaphore_key(
    workspace_id: uuid.UUID | str, domain: str, access_method: AccessMethod
) -> str:
    """Concurrency-semaphore key: ``semaphore:{workspace_id}:{domain}:{access_method}`` (FR-003)."""
    return f"semaphore:{workspace_id}:{domain}:{access_method.value}"


def match_lock_key(workspace_id: uuid.UUID | str, match_id: uuid.UUID | str) -> str:
    """Match-lock key: ``lock:scrape:{workspace_id}:{match_id}`` (FR-010)."""
    return f"lock:scrape:{workspace_id}:{match_id}"
