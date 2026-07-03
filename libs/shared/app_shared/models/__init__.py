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

# Import so `SmokeFoundation` registers on `Base.metadata` — required for
# both Alembic autogenerate/offline-render (`target_metadata`) and the
# first migration (T026) to see the table. Not re-exported: the demo
# table is test/migration support, not part of the public model surface.
from app_shared.models import _smoke  # noqa: F401

# The SPEC-03 identity models (workspaces/users/refresh_tokens/api_keys) —
# re-exported so `Base.metadata` sees all four tables for Alembic
# autogenerate/offline-render (target_metadata), and so callers can
# `from app_shared.models import User, ApiKey, ...`.
from app_shared.models.identity import ApiKey, RefreshToken, User, Workspace

# The SPEC-04 catalog models (products/variants/groups/group-items) —
# re-exported so `Base.metadata` sees all four tables for Alembic
# autogenerate/offline-render (target_metadata), and so callers can
# `from app_shared.models import Product, ...`.
from app_shared.models.catalog import Product, ProductGroup, ProductGroupItem, ProductVariant

# The SPEC-05 competitor/match models — re-exported so `Base.metadata`
# sees both tables for Alembic autogenerate/offline-render
# (target_metadata), and so callers can
# `from app_shared.models import Competitor, CompetitorProductMatch`.
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch

__all__ = [
    "Base",
    "metadata",
    "NAMING_CONVENTION",
    "TimestampMixin",
    "TZDateTime",
    "WorkspaceScopedBase",
    "emit_rls_policy",
    "Workspace",
    "User",
    "RefreshToken",
    "ApiKey",
    "Product",
    "ProductVariant",
    "ProductGroup",
    "ProductGroupItem",
    "Competitor",
    "CompetitorProductMatch",
]
