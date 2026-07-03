"""Catalog ORM models: products, variants, groups (SPEC-04).

Per ``contracts/models-catalog.md`` / ``data-model.md`` — four
workspace-owned tables, all on :class:`~app_shared.models.base.WorkspaceScopedBase`
(``workspace_id NOT NULL``, indexed), each registered in
:data:`app_shared.repository.WORKSPACE_OWNED_MODELS` and given
:func:`app_shared.models.rls.emit_rls_policy` in the creating Alembic
migration (``alembic/versions/<rev>_catalog_tables.py``), not here — this
module only declares ORM shape.

* :class:`Product` — no price column (pricing is variant-level only,
  Principle III). Partial-unique ``external_id``/``sku`` (per workspace,
  where present); ``unique(workspace_id, id)`` so children can target it
  via a workspace-local composite FK (D3).
* :class:`ProductVariant` — the only table carrying money
  (``current_price`` / ``currency``). Composite FK
  ``(workspace_id, product_id) -> products(workspace_id, id)``; full
  ``unique(workspace_id, product_id, title)`` so a product has at most
  one row per title (one default per product, FR-005/FR-006).
* :class:`ProductGroup` — named, workspace-unique grouping container.
* :class:`ProductGroupItem` — membership row referencing **either** a
  product **or** a variant (app-layer "exactly one" rule; the DB allows
  either via nullable ``MATCH SIMPLE`` composite FKs). Bare ``created_at``
  (:class:`~app_shared.models.base.TZDateTime`) with **no** ``updated_at``
  — same shape as ``RefreshToken`` (SPEC-03).

All workspace-local composite FKs point at a ``unique(workspace_id, id)``
parent, so a child's ``(workspace_id, <ref>)`` pair can only ever
reference a row that lives in the *same* workspace (D3) — cross-workspace
references are structurally impossible, not just app-filtered.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    ForeignKeyConstraint,
    Index,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.enums import GroupStatus, ProductStatus, VariantStatus, enum_column
from app_shared.models.base import Base, TimestampMixin, TZDateTime, WorkspaceScopedBase
from app_shared.money import Money


class Product(Base, WorkspaceScopedBase, TimestampMixin):
    """``products`` — no price column; pricing lives on variants only."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        Index(
            "uq_products_workspace_id_external_id",
            "workspace_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index(
            "uq_products_workspace_id_sku",
            "workspace_id",
            "sku",
            unique=True,
            postgresql_where=text("sku IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_products_workspace_id_workspaces",
        ),
    )

    external_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    sku: Mapped[str | None] = mapped_column(Text(), nullable=True)
    title: Mapped[str] = mapped_column(Text(), nullable=False)
    brand: Mapped[str | None] = mapped_column(Text(), nullable=True)
    barcode: Mapped[str | None] = mapped_column(Text(), nullable=True)
    url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[ProductStatus] = enum_column(ProductStatus, nullable=False)


class ProductVariant(Base, WorkspaceScopedBase, TimestampMixin):
    """``product_variants`` — the only catalog table carrying money."""

    __tablename__ = "product_variants"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "product_id", "title"),
        Index(
            "uq_product_variants_workspace_id_external_id",
            "workspace_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL"),
        ),
        Index(
            "uq_product_variants_workspace_id_sku",
            "workspace_id",
            "sku",
            unique=True,
            postgresql_where=text("sku IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_product_variants_workspace_id_product_id_products",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_product_variants_workspace_id_workspaces",
        ),
    )

    product_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    external_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    sku: Mapped[str | None] = mapped_column(Text(), nullable=True)
    barcode: Mapped[str | None] = mapped_column(Text(), nullable=True)
    title: Mapped[str] = mapped_column(Text(), nullable=False)
    option_values: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)
    current_price: Mapped[Decimal] = mapped_column(Money(), nullable=False)
    currency: Mapped[str] = mapped_column(CHAR(3), nullable=False)
    url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[VariantStatus] = enum_column(VariantStatus, nullable=False)


class ProductGroup(Base, WorkspaceScopedBase, TimestampMixin):
    """``product_groups`` — named, workspace-unique grouping container."""

    __tablename__ = "product_groups"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "name"),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_product_groups_workspace_id_workspaces",
        ),
    )

    name: Mapped[str] = mapped_column(Text(), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[GroupStatus] = enum_column(GroupStatus, nullable=False)


class ProductGroupItem(Base, WorkspaceScopedBase):
    """``product_group_items`` — membership row (product XOR variant, app-layer).

    Declares ``created_at`` directly as :class:`TZDateTime` rather than
    using :class:`TimestampMixin` — this table has no ``updated_at``
    (§22 shape, like ``RefreshToken``).
    """

    __tablename__ = "product_group_items"
    # NOTE on constraint-name length: Postgres identifiers are capped at
    # 63 bytes. The fully-spelled-out NAMING_CONVENTION-style names for
    # this table's composite FKs/partial-unique indexes (e.g.
    # ``fk_product_group_items_workspace_id_product_group_id_product_groups``,
    # 67 chars) exceed that limit, so these names are explicit and use
    # ``group_id``/``variant_id`` shorthands (dropping the redundant
    # ``product_`` prefix already implied by the table name) to stay
    # under 63 chars while remaining deterministic and unambiguous.
    __table_args__ = (
        Index(
            "uq_product_group_items_workspace_id_group_id_product_id",
            "workspace_id",
            "product_group_id",
            "product_id",
            unique=True,
            postgresql_where=text("product_id IS NOT NULL"),
        ),
        Index(
            "uq_product_group_items_workspace_id_group_id_variant_id",
            "workspace_id",
            "product_group_id",
            "product_variant_id",
            unique=True,
            postgresql_where=text("product_variant_id IS NOT NULL"),
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_group_id"],
            ["product_groups.workspace_id", "product_groups.id"],
            name="fk_product_group_items_workspace_id_group_id_product_groups",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_id"],
            ["products.workspace_id", "products.id"],
            name="fk_product_group_items_workspace_id_product_id_products",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "product_variant_id"],
            ["product_variants.workspace_id", "product_variants.id"],
            name="fk_product_group_items_workspace_id_variant_id_product_variants",
            match="SIMPLE",
        ),
        ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_product_group_items_workspace_id_workspaces",
        ),
    )

    product_group_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    product_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    product_variant_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)
