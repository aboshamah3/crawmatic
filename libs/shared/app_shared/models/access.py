"""``ProxyProvider``/``AccessPolicy``/``DomainAccessRule`` ORM models (SPEC-10).

Per ``contracts/models-access.md`` / ``data-model.md`` §22 — three new
tables in exactly two isolation shapes (research D2):

* ``ProxyProvider`` + ``AccessPolicy`` — **dual-scope**, the SPEC-06
  ``ScrapeProfile`` pattern: ``Base + TimestampMixin`` (**not**
  ``WorkspaceScopedBase``, whose ``workspace_id`` is ``NOT NULL`` and
  would forbid a global row); nullable indexed ``workspace_id``
  (``NULL`` = a global system default readable by every workspace,
  writable by none through the tenant path); **not** registered in
  ``app_shared.repository.WORKSPACE_OWNED_MODELS`` (its
  ``scoped_select``/``scoped_get`` would hide global rows) — every
  query goes through ``app_shared.access.repository`` instead; RLS is
  enabled via ``app_shared.models.rls.emit_global_readable_rls_policy``
  in the creating Alembic migration, not here (this module declares ORM
  shape only).
* ``DomainAccessRule`` — **tenant-only**: ``WorkspaceScopedBase`` (its
  ``workspace_id`` is ``NOT NULL``) + ``TimestampMixin``; registered in
  ``WORKSPACE_OWNED_MODELS``; RLS via the standard
  ``app_shared.models.rls.emit_rls_policy``.

Soft references (``provider_id``, ``access_policy_id``, ``competitor_id``)
are plain ``Uuid`` columns with **no** FK (§22's soft-reference
philosophy — tolerate a disabled/deleted referent; readers must degrade
gracefully). Only ``workspace_id`` gets a real FK (the RLS anchor). All
enum-like columns render as ``VARCHAR`` via ``enum_column`` (never a
Postgres-native ``ENUM``). All constraint/index names are well under the
63-byte Postgres identifier cap.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, ForeignKeyConstraint, Index, Integer, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import (
    AccessStrategy,
    ProxyProviderStatus,
    ProxyType,
    enum_column,
)
from app_shared.models.base import Base, TimestampMixin, WorkspaceScopedBase


class ProxyProvider(Base, TimestampMixin):
    """``proxy_providers`` — dual-scope proxy endpoint config (§22, FR-002).

    ``workspace_id IS NULL`` marks a global/shared default proxy
    provider, readable by every workspace, writable by none through the
    tenant path (mirrors ``ScrapeProfile``, FR-006).
    """

    __tablename__ = "proxy_providers"
    __table_args__ = (
        Index(
            "uq_proxy_providers_workspace_id_name",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index(
            "uq_proxy_providers_name_global",
            "name",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_proxy_providers_workspace_id_workspaces",
        ),
    )

    # NULL = global default. Indexed (WorkspaceScopedBase-style), but
    # nullable — cannot use the mixin itself (its column is NOT NULL).
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    type: Mapped[ProxyType] = enum_column(ProxyType, nullable=False)
    base_url: Mapped[str] = mapped_column(Text(), nullable=False)
    username: Mapped[str | None] = mapped_column(Text(), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(Text(), nullable=True)
    password_key_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    country_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[ProxyProviderStatus] = enum_column(
        ProxyProviderStatus, nullable=False, default=ProxyProviderStatus.ACTIVE
    )
    monthly_budget_limit: Mapped[int | None] = mapped_column(Integer(), nullable=True)


class AccessPolicy(Base, TimestampMixin):
    """``access_policies`` — dual-scope named access strategy (§22, FR-001).

    Same nullable-``workspace_id`` dual-scope shape as ``ProxyProvider``.
    ``provider_id`` is a plain soft reference (no FK) so a disabled/
    deleted provider degrades gracefully at resolution time instead of
    raising an integrity error.
    """

    __tablename__ = "access_policies"
    __table_args__ = (
        Index(
            "uq_access_policies_workspace_id_name",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index(
            "uq_access_policies_name_global",
            "name",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_access_policies_workspace_id_workspaces",
        ),
    )

    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    strategy: Mapped[AccessStrategy] = enum_column(AccessStrategy, nullable=False)
    # Soft ref -> proxy_providers.id (no FK, §22) — disabled/deleted tolerated.
    provider_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    country_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    use_proxy_on_first_attempt: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False
    )
    use_proxy_on_retry: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    allow_browser_fallback: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False
    )
    max_retries: Mapped[int] = mapped_column(Integer(), nullable=False, default=2)
    rotate_per_request: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    sticky_session: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    session_ttl_minutes: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    max_requests_per_minute: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    max_requests_per_hour: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    max_requests_per_day: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    timeout_ms: Mapped[int] = mapped_column(Integer(), nullable=False, default=30000)


class DomainAccessRule(Base, WorkspaceScopedBase, TimestampMixin):
    """``domain_access_rules`` — tenant-only per-domain policy override (§22, FR-004).

    Unlike the two dual-scope tables above, a domain rule binds a
    workspace-owned ``competitor_id`` — a global row would be
    nonsensical, so this is a standard workspace-owned table
    (``WorkspaceScopedBase``, ``workspace_id NOT NULL``, registered in
    ``WORKSPACE_OWNED_MODELS``).
    """

    __tablename__ = "domain_access_rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_domain_access_rules_workspace_id_workspaces",
        ),
        Index(
            "ix_domain_access_rules_workspace_id_competitor_id_domain",
            "workspace_id",
            "competitor_id",
            "domain",
        ),
        # A NULL url_pattern is the domain-only rule; Postgres treats NULLs
        # as distinct under a plain UNIQUE constraint, so COALESCE(...) folds
        # every domain-only rule for the same (workspace, competitor, domain)
        # onto the same expression value, forbidding more than one.
        Index(
            "uq_domain_access_rules_ws_cid_domain_pattern",
            "workspace_id",
            "competitor_id",
            "domain",
            text("COALESCE(url_pattern, '')"),
            unique=True,
        ),
    )

    # Soft ref -> competitors.id (no FK, §22); indexed via the composite
    # lookup index above.
    competitor_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    url_pattern: Mapped[str | None] = mapped_column(Text(), nullable=True)
    url_pattern_override: Mapped[str | None] = mapped_column(Text(), nullable=True)
    # Soft ref -> access_policies.id (no FK, §22).
    access_policy_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    max_concurrent_requests: Mapped[int] = mapped_column(Integer(), nullable=False)
    max_requests_per_minute: Mapped[int] = mapped_column(Integer(), nullable=False)
    cooldown_seconds: Mapped[int] = mapped_column(Integer(), nullable=False)
    block_detection_rules: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
