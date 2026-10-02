"""Catalog index ORM models: ``catalog_index_products``,
``catalog_index_codes``, ``catalog_index_loads`` (2026-10-02).

A fleet-wide, read-mostly copy of PUBLIC storefront listings (title,
price, URL of products that ~30k KSA stores publish), crawled on the ops
host and bulk-loaded by ``scripts/load_catalog_index.py``. Used to
propose competitor matches (``app_shared.catalog_index.lookup``).

GLOBAL, no ``workspace_id``, no RLS, the same class as
``domain_playbooks``: the rows describe public pages, are written by no
tenant path, and are the same for every workspace. Must NOT be added to
``app_shared.repository.WORKSPACE_OWNED_MODELS``.

GENERATIONS. A load writes a complete new ``generation`` beside the old
one and then flips ``catalog_index_loads.status`` to ``active`` in one
transaction; lookups read only the active generation, so a crashed or
truncated load is never visible. The loader deletes older generations
after the flip. That is why both big tables key on ``generation`` first
and carry no surrogate id and no foreign key between them.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import BigInteger, Boolean, Index, Integer, Numeric, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app_shared.models.base import Base, TimestampMixin, TZDateTime

__all__ = ["CatalogIndexCode", "CatalogIndexLoad", "CatalogIndexProduct"]


class CatalogIndexProduct(Base):
    """One storefront listing in one generation."""

    __tablename__ = "catalog_index_products"
    __table_args__ = (
        Index(
            "ix_catalog_index_products_title_tsv",
            text("to_tsvector('simple', coalesce(title, ''))"),
            postgresql_using="gin",
        ),
        Index("ix_catalog_index_products_generation_domain", "generation", "domain"),
    )

    id = None  # type: ignore[assignment]

    generation: Mapped[int] = mapped_column(Integer(), primary_key=True)
    #: The crawl index's own row id; unique within a generation.
    source_rowid: Mapped[int] = mapped_column(BigInteger(), primary_key=True)
    #: The store: a bare host, or ``salla.sa/<store>`` for a path store.
    domain: Mapped[str] = mapped_column(Text(), nullable=False)
    #: Bare host of ``domain`` (what ``competitors.domain`` stores).
    host: Mapped[str] = mapped_column(Text(), nullable=False)
    url: Mapped[str] = mapped_column(Text(), nullable=False)
    title: Mapped[str | None] = mapped_column(Text(), nullable=True)
    brand: Mapped[str | None] = mapped_column(Text(), nullable=True)
    sku: Mapped[str | None] = mapped_column(Text(), nullable=True)
    mpn: Mapped[str | None] = mapped_column(Text(), nullable=True)
    gtin: Mapped[str | None] = mapped_column(Text(), nullable=True)
    #: Price as crawled. Third-party data in the store's own currency, so
    #: a plain numeric, not the engine's ``Money`` type.
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    available: Mapped[bool | None] = mapped_column(Boolean(), nullable=True)
    #: The crawl classifier's verdict for the store: ``pool`` (multi-brand
    #: reseller), ``own_brand``, ``unbranded``, ``foreign_currency``,
    #: ``unreadable`` or ``unknown``.
    store_verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    #: When the price was observed (the index build time), never "now".
    crawled_at: Mapped[datetime] = mapped_column(TZDateTime(), nullable=False)


class CatalogIndexCode(Base):
    """One lookup key of one listing: kind ``g`` (GTIN-14) or ``m``
    (model code), from ``app_shared.catalog_index.keys``."""

    __tablename__ = "catalog_index_codes"

    id = None  # type: ignore[assignment]

    generation: Mapped[int] = mapped_column(Integer(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(1), primary_key=True)
    code_key: Mapped[str] = mapped_column(Text(), primary_key=True)
    source_rowid: Mapped[int] = mapped_column(BigInteger(), primary_key=True)


class CatalogIndexLoad(Base, TimestampMixin):
    """One load attempt. ``status``: ``loading`` -> ``active`` ->
    ``retired``, or ``failed``. Exactly one row is ``active``."""

    __tablename__ = "catalog_index_loads"

    id = None  # type: ignore[assignment]

    generation: Mapped[int] = mapped_column(Integer(), primary_key=True, autoincrement=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    products: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    codes: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    #: Build time of the crawl index this generation was loaded from.
    source_built_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(TZDateTime(), nullable=True)
