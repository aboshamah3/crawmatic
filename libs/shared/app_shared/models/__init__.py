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
from app_shared.models.rls import (
    emit_fk_transitive_rls_policy,
    emit_global_readable_rls_policy,
    emit_rls_policy,
)

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

# The SPEC-06 ScrapeProfile model — re-exported so `Base.metadata` sees
# the table for Alembic autogenerate/offline-render (target_metadata),
# and so callers can `from app_shared.models import ScrapeProfile`.
# Dual-scope (research D2): deliberately NOT added to
# `app_shared.repository.WORKSPACE_OWNED_MODELS` — see
# `app_shared.profiles.repository` for the sanctioned dual-scope query
# path.
from app_shared.models.scrape_profiles import ScrapeProfile

# The SPEC-07 observation/current-price models — re-exported so
# `Base.metadata` sees all three tables for Alembic autogenerate/offline-
# render (target_metadata), and so callers can
# `from app_shared.models import PriceObservation, RequestAttempt,
# MatchCurrentPrice`. Workspace-owned (unlike ScrapeProfile): registered
# in `app_shared.repository.WORKSPACE_OWNED_MODELS`.
from app_shared.models.observations import MatchCurrentPrice, PriceObservation, RequestAttempt

# The SPEC-08 jobs/orchestration models — re-exported so `Base.metadata`
# sees both tables for Alembic autogenerate/offline-render
# (target_metadata), and so callers can `from app_shared.models import
# ScrapeJob, ScrapeJobTarget`. Both workspace-owned: registered in
# `app_shared.repository.WORKSPACE_OWNED_MODELS`.
from app_shared.models.jobs import ScrapeJob, ScrapeJobTarget

# The SPEC-09 alert/price-comparison models — re-exported so
# `Base.metadata` sees all three tables for Alembic autogenerate/offline-
# render (target_metadata), and so callers can `from app_shared.models
# import VariantPriceState, VariantAlertState, PriceAlertEvent`. All
# workspace-owned: registered in
# `app_shared.repository.WORKSPACE_OWNED_MODELS`.
from app_shared.models.alerts import PriceAlertEvent, VariantAlertState, VariantPriceState

# The SPEC-10 access/proxy models — re-exported so `Base.metadata` sees
# all three tables for Alembic autogenerate/offline-render
# (target_metadata), and so callers can `from app_shared.models import
# ProxyProvider, AccessPolicy, DomainAccessRule`. `ProxyProvider`/
# `AccessPolicy` are dual-scope (mirrors `ScrapeProfile`): deliberately
# NOT added to `app_shared.repository.WORKSPACE_OWNED_MODELS` — see
# `app_shared.access.repository` for the sanctioned dual-scope query
# path. `DomainAccessRule` is tenant-only and IS registered there.
from app_shared.models.access import AccessPolicy, DomainAccessRule, ProxyProvider

# The SPEC-12 domain strategy optimizer models — re-exported so
# `Base.metadata` sees all three tables for Alembic autogenerate/offline-
# render (target_metadata), and so callers can `from app_shared.models
# import DomainStrategyProfile, StrategyAttemptStats,
# StrategyDiscoveryRun`. `DomainStrategyProfile`/`StrategyDiscoveryRun`
# are workspace-owned: registered in
# `app_shared.repository.WORKSPACE_OWNED_MODELS`. `StrategyAttemptStats`
# has NO `workspace_id` column at all (unlike the nullable-workspace_id
# dual-scope tables above) — deliberately NOT added there; isolated
# transitively via `emit_fk_transitive_rls_policy` and queried only
# joined to its scoped parent profile via
# `app_shared.strategy.repository`.
from app_shared.models.strategy import (
    DomainStrategyProfile,
    StrategyAttemptStats,
    StrategyDiscoveryRun,
)

# The SPEC-13 RefreshRule model — re-exported so `Base.metadata` sees the
# table for Alembic autogenerate/offline-render (target_metadata), and so
# callers can `from app_shared.models import RefreshRule`. Workspace-owned:
# registered in `app_shared.repository.WORKSPACE_OWNED_MODELS`.
from app_shared.models.refresh_rules import RefreshRule

# The SPEC-15 US2 VariantPriceDailyRollup model — re-exported so
# `Base.metadata` sees the table for Alembic autogenerate/offline-render
# (target_metadata), and so callers can `from app_shared.models import
# VariantPriceDailyRollup`. Workspace-owned: registered in
# `app_shared.repository.WORKSPACE_OWNED_MODELS`.
from app_shared.models.rollups import VariantPriceDailyRollup

# The SPEC-16 webhook models — re-exported so `Base.metadata` sees both
# tables for Alembic autogenerate/offline-render (target_metadata), and
# so callers can `from app_shared.models import WebhookEndpoint,
# WebhookEvent`. Both workspace-owned: registered in
# `app_shared.repository.WORKSPACE_OWNED_MODELS`.
from app_shared.models.webhooks import WebhookEndpoint, WebhookEvent

__all__ = [
    "Base",
    "metadata",
    "NAMING_CONVENTION",
    "TimestampMixin",
    "TZDateTime",
    "WorkspaceScopedBase",
    "emit_rls_policy",
    "emit_global_readable_rls_policy",
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
    "ScrapeProfile",
    "PriceObservation",
    "RequestAttempt",
    "MatchCurrentPrice",
    "ScrapeJob",
    "ScrapeJobTarget",
    "VariantPriceState",
    "VariantAlertState",
    "PriceAlertEvent",
    "ProxyProvider",
    "AccessPolicy",
    "DomainAccessRule",
    "emit_fk_transitive_rls_policy",
    "DomainStrategyProfile",
    "StrategyAttemptStats",
    "StrategyDiscoveryRun",
    "RefreshRule",
    "VariantPriceDailyRollup",
    "WebhookEndpoint",
    "WebhookEvent",
]
