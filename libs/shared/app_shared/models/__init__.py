"""Public surface of ``app_shared.models``.

This is what Alembic's ``target_metadata`` and every later ORM model
import from — re-exports the shared declarative base, metadata, naming
convention, correctness mixins, and the RLS DDL emitter.
"""

from __future__ import annotations

from app_shared.models.base import (
    NAMING_CONVENTION,
    Base,
    TimestampMixin,
    TZDateTime,
    WorkspaceScopedBase,
    metadata,
)
from app_shared.models.rls import emit_rls_policy

__all__ = [
    "Base",
    "metadata",
    "NAMING_CONVENTION",
    "TimestampMixin",
    "TZDateTime",
    "WorkspaceScopedBase",
    "emit_rls_policy",
]
