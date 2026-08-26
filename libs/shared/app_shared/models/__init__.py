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
    PARTITION_RLS_INHERITANCE_SQL,
    emit_fk_transitive_rls_policy,
    emit_global_readable_rls_policy,
    emit_partition_rls_inheritance,
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
from app_shared.models.scrape_profiles import ScrapeProfile, ScrapeProfileRevision

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
    DomainStrategyMethod,
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

# 2026-08-11 proxy-cost Fix 4: fully-global curated per-domain scraping
# playbook (no workspace column at all — operator-seeded reference data;
# see the model's module docstring). Not workspace-owned, no RLS.
from app_shared.models.domain_playbooks import DomainPlaybook

# 2026-08-15 audit risk H3: durable (non-Redis) proxy-spend circuit
# breaker state. Also fully global, no workspace column, no RLS — a
# platform-wide financial kill switch over one shared proxy account
# (see the model's module docstring).
from app_shared.models.proxy_breaker import (
    ProxyBreakerState,
    ProxyBreakerTrip,
    ProxyCircuitBreaker,
)

# 2026-08-15 audit risk H1: the transactional outbox. Workspace-owned
# (registered in `app_shared.repository.WORKSPACE_OWNED_MODELS`, RLS in
# its own migration); deliberately NOT partitioned — see the model's
# module docstring for the drain-to-empty-queue rationale.
from app_shared.models.outbox import OutboxMessage

# 2026-08-15 readiness cycle: durable deadlines for the scheduler's daily
# maintenance cadences (the in-process float accumulators they replace
# reset to 0.0 on every container restart, so on Railway the 86400s
# cadences could — and did — never fire). Global, no RLS, same shape as
# `domain_playbooks`/`proxy_circuit_breakers`.
from app_shared.models.maintenance_cadence import MaintenanceCadence

# EPA A6 (2026-08-25): match-audit-classification sidecar over
# competitor_product_matches. No workspace_id column of its own — same
# shape as StrategyAttemptStats above; isolated transitively via its FK
# to competitor_product_matches and deliberately NOT added to
# `app_shared.repository.WORKSPACE_OWNED_MODELS`. Append-only
# (supersede, never overwrite) — see the model's module docstring.
from app_shared.models.match_audit import MatchAuditClassification

# EPA B1 (2026-08-25): the durable dispatch intent — the authority the
# Redis `dispatched:*` guard was previously pretending to be. Workspace-
# owned (own workspace_id + composite FK to its job, the ScrapeJobTarget
# shape): registered in `app_shared.repository.WORKSPACE_OWNED_MODELS`,
# RLS via the standard `emit_rls_policy` in its own migration. See the
# model's module docstring for why it is NOT transitively scoped.
from app_shared.models.dispatch import DispatchIntent

# EPA B4 (2026-08-25): typed competitor identifiers — the child table
# that replaces the single untyped
# `competitor_product_matches.competitor_variant_identifier` slot (whose
# misreading as a Shopify variant id produced 26 false NOT_LISTED
# verdicts on S-Tech in the 2026-08-24 canary). No workspace_id column of
# its own — same shape as MatchAuditClassification above; isolated
# transitively via its FK to competitor_product_matches and deliberately
# NOT added to `app_shared.repository.WORKSPACE_OWNED_MODELS`.
from app_shared.models.competitor_identifiers import MatchCompetitorIdentifier

# EPA C1 (2026-08-25): the PHYSICAL network-operation ledger — three
# separate append-only tables (operations / allocations / settlements),
# not one mutable row. Re-exported so `Base.metadata` sees all three for
# Alembic autogenerate/offline-render (`target_metadata`); without that,
# a later autogenerate would read them as tables to DROP.
# `NetworkOperation`/`NetworkOperationSettlement` are FLEET-owned and
# carry NO `workspace_id` at all — deliberately NOT added to
# `app_shared.repository.WORKSPACE_OWNED_MODELS`, and deliberately not
# transitively scoped either (see the module docstring).
# `NetworkOperationAllocation` IS workspace-owned and RLS'd; registering
# it in `WORKSPACE_OWNED_MODELS` belongs with the C4 query paths that
# will actually read it.
from app_shared.models.network_operations import (
    NetworkOperation,
    NetworkOperationAllocation,
    NetworkOperationSettlement,
    NetworkTransport,
    SettlementMethod,
)

# EPA C3 (2026-08-25): the cost-authorization store — reservations, the
# tenant/fleet budget counters they lock, and the durable local
# entitlement evidence the denial path reads. Re-exported so
# `Base.metadata` sees all four tables for Alembic autogenerate/offline-
# render (`target_metadata`); without that a later autogenerate would
# read them as tables to DROP. `CostReservation`, `CostBudget` and
# `WorkspaceEntitlement` are workspace-owned and RLS'd; `FleetCostBudget`
# is global with no `workspace_id` at all (the `proxy_circuit_breakers`
# shape — one shared provider balance, so every workspace's scrape path
# must be able to read and decrement the same counter). Registering the
# workspace-owned three in `app_shared.repository.WORKSPACE_OWNED_MODELS`
# is deliberately NOT done here: every read/write goes through
# `app_shared.costauth.service`, which asserts the workspace predicate in
# its own SQL.
from app_shared.models.cost_authorization import (
    AuthorizationPurpose,
    CostBudget,
    CostReservation,
    EntitlementState,
    FleetCostBudget,
    ReservationState,
    WorkspaceEntitlement,
)

# EPA C5 (2026-08-26): raw provider usage evidence — the OTHER side of
# reconciliation against C1's ledger. Re-exported so `Base.metadata` sees
# the table for Alembic autogenerate/offline-render (`target_metadata`).
# Fleet-owned like `NetworkOperation`/`NetworkOperationSettlement`: no
# `workspace_id` at all, deliberately NOT added to
# `app_shared.repository.WORKSPACE_OWNED_MODELS`. Filed GAP (not SYSTEM)
# in `scripts/rls_table_manifest.txt` for the same reason
# `network_operations` is GAP rather than SYSTEM — see that model's
# module docstring for the contrast this mirrors.
from app_shared.models.provider_usage import ProviderUsageGranularity, ProviderUsageRecord

# EPA C6 (2026-08-26): the durable, bounded-cardinality cost rollups that
# make `GET /ops/metrics` (and the new tenant-scoped `/v1/cost-rollups`
# read) O(rollup rows) rather than a synchronous high-cardinality
# aggregation over `network_operations`/`network_operation_allocations`
# on every request. `FleetNetworkCostRollup` is global (no
# `workspace_id`, no RLS) — the same shape as `network_operations` it
# summarises, but SYSTEM rather than GAP in
# `scripts/rls_table_manifest.txt` (see that model's own docstring for
# why a fleet SUM is not tenant-linked the way a raw ledger row is).
# `NetworkCostRollup` is workspace-owned and RLS'd, and IS registered in
# `app_shared.repository.WORKSPACE_OWNED_MODELS` below — unlike
# `NetworkOperationAllocation`, this table's own read path
# (`apps/api/app/routers/cost_rollups.py`) ships in this same change.
from app_shared.models.network_cost_rollups import FleetNetworkCostRollup, NetworkCostRollup

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
    "ScrapeProfileRevision",
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
    "emit_partition_rls_inheritance",
    "PARTITION_RLS_INHERITANCE_SQL",
    "DomainStrategyProfile",
    "DomainStrategyMethod",
    "StrategyAttemptStats",
    "StrategyDiscoveryRun",
    "RefreshRule",
    "VariantPriceDailyRollup",
    "WebhookEndpoint",
    "WebhookEvent",
    "DomainPlaybook",
    "ProxyBreakerState",
    "ProxyBreakerTrip",
    "ProxyCircuitBreaker",
    "OutboxMessage",
    "MaintenanceCadence",
    "MatchAuditClassification",
    "DispatchIntent",
    "MatchCompetitorIdentifier",
    "NetworkOperation",
    "NetworkOperationAllocation",
    "NetworkOperationSettlement",
    "NetworkTransport",
    "SettlementMethod",
    "AuthorizationPurpose",
    "CostBudget",
    "CostReservation",
    "EntitlementState",
    "FleetCostBudget",
    "ReservationState",
    "WorkspaceEntitlement",
    "ProviderUsageRecord",
    "ProviderUsageGranularity",
    "FleetNetworkCostRollup",
    "NetworkCostRollup",
]
