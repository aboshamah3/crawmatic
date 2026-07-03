"""``ScrapeProfile`` ORM model: ``scrape_profiles`` (SPEC-06).

Per ``contracts/models-scrape-profiles.md`` / ``data-model.md`` — the
project's first **dual-scope** table (research D2): workspace rows
(``workspace_id NOT NULL``) are RLS-isolated exactly like every prior
workspace-owned table; global rows (``workspace_id IS NULL``) are
readable by every workspace and writable by none through the tenant
path (a shared/default extraction profile, §9).

Three deliberate departures from the SPEC-03/04/05 workspace-owned
mould (each required by the dual-scope semantics, not a relaxation of
Principle II — see plan.md Complexity Tracking):

* ``Base + TimestampMixin`` — **not** :class:`~app_shared.models.base.WorkspaceScopedBase`,
  whose ``workspace_id`` column is ``NOT NULL`` and would forbid a
  global (``NULL``) row.
* **Not** registered in :data:`app_shared.repository.WORKSPACE_OWNED_MODELS`
  — that set's ``scoped_select``/``scoped_get`` constrain to
  ``workspace_id = ctx``, which would hide global rows. Every profile
  query goes through the dedicated dual-scope helpers in
  ``app_shared.profiles.repository`` instead.
* RLS is enabled via the custom
  :func:`app_shared.models.rls.emit_global_readable_rls_policy` in the
  creating Alembic migration (not here — this module only declares ORM
  shape), not the standard :func:`~app_shared.models.rls.emit_rls_policy`.

Two **partial** unique indexes enforce name uniqueness per scope
(research D3/D9, also the bulk-upsert conflict arbiter for tenant rows):
``uq_scrape_profiles_workspace_id_name`` on ``(workspace_id, name)``
``WHERE workspace_id IS NOT NULL`` (per-tenant), and
``uq_scrape_profiles_name_global`` on ``(name)`` ``WHERE workspace_id IS
NULL`` (single global namespace). All constraint/index names here are
well under the 63-byte Postgres identifier cap.
"""

from __future__ import annotations

import uuid

from sqlalchemy import ForeignKeyConstraint, Index, Integer, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import AdapterKey, ScrapeProfileMode, VariantStrategy, enum_column
from app_shared.models.base import Base, TimestampMixin


class ScrapeProfile(Base, TimestampMixin):
    """``scrape_profiles`` — dual-scope extraction configuration (§22).

    ``workspace_id IS NULL`` marks a global/shared default profile,
    readable by every workspace, writable by none through the tenant
    path (FR-001, FR-021).
    """

    __tablename__ = "scrape_profiles"
    __table_args__ = (
        Index(
            "uq_scrape_profiles_workspace_id_name",
            "workspace_id",
            "name",
            unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index(
            "uq_scrape_profiles_name_global",
            "name",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_scrape_profiles_workspace_id_workspaces",
        ),
    )

    # NULL = global default. Indexed (WorkspaceScopedBase-style), but
    # nullable — cannot use the mixin itself (its column is NOT NULL).
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    mode: Mapped[ScrapeProfileMode] = enum_column(
        ScrapeProfileMode, nullable=False, default=ScrapeProfileMode.HTTP
    )
    adapter_key: Mapped[AdapterKey] = enum_column(
        AdapterKey, nullable=False, default=AdapterKey.DEFAULT_HTTP
    )
    jsonld_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    platform_patterns_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)
    embedded_json_enabled: Mapped[bool] = mapped_column(nullable=False, default=True)

    price_selector: Mapped[str | None] = mapped_column(Text(), nullable=True)
    price_xpath: Mapped[str | None] = mapped_column(Text(), nullable=True)
    price_regex: Mapped[str | None] = mapped_column(Text(), nullable=True)
    old_price_selector: Mapped[str | None] = mapped_column(Text(), nullable=True)
    old_price_xpath: Mapped[str | None] = mapped_column(Text(), nullable=True)
    old_price_regex: Mapped[str | None] = mapped_column(Text(), nullable=True)
    currency_selector: Mapped[str | None] = mapped_column(Text(), nullable=True)
    currency_xpath: Mapped[str | None] = mapped_column(Text(), nullable=True)
    currency_regex: Mapped[str | None] = mapped_column(Text(), nullable=True)
    stock_selector: Mapped[str | None] = mapped_column(Text(), nullable=True)
    stock_xpath: Mapped[str | None] = mapped_column(Text(), nullable=True)
    stock_regex: Mapped[str | None] = mapped_column(Text(), nullable=True)
    title_selector: Mapped[str | None] = mapped_column(Text(), nullable=True)
    title_xpath: Mapped[str | None] = mapped_column(Text(), nullable=True)

    variant_strategy: Mapped[VariantStrategy] = enum_column(
        VariantStrategy, nullable=False, default=VariantStrategy.PAGE_SINGLE_PRICE
    )
    variant_selector_config: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    price_transform_rules: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    validation_rules: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    confidence_rules: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)

    wait_for_selector: Mapped[str | None] = mapped_column(Text(), nullable=True)
    request_timeout_ms: Mapped[int] = mapped_column(Integer(), nullable=False, default=30000)
    browser_timeout_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    headers: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    cookies: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
