"""Set-based bulk upsert core (`contracts/catalog-bulk-upsert.md`, FR-010/011/012).

Pure — compiles SQLAlchemy Core (``postgresql`` dialect) statements and
plain-data resolution maps; **never executes anything and never opens a
session**. ``apps/api/app/routers/products.py`` /
``routers/variants.py`` execute the statements this module builds,
inside the request's already-workspace-scoped transaction (FR-016).

## Identity resolution (FR-011)
Order per record: ``external_id`` -> ``sku`` -> (variants only)
``(product_id, title)``. A **product** with neither ``external_id`` nor
``sku`` has no stable identity key and is always inserted fresh (never
matched/updated) -- see :func:`build_products_upsert`.

## Bounded statements (FR-010, SC-003)
:func:`plan_upsert` partitions a batch into at most one statement per
identity kind -- three for products (``external_id`` / ``sku`` /
identity-less-plain-insert), three for variants (``external_id`` /
``sku`` / ``(product_id, title)``) -- regardless of ``len(rows)``. There
is no per-row loop anywhere in this module.

## ON CONFLICT inference (research D2)
Targeting a **partial** unique index requires the ``index_where``
inference clause to match the index predicate *exactly* (see
``app_shared/models/catalog.py``'s ``uq_products_workspace_id_external_id``
etc.): ``external_id IS NOT NULL`` / ``sku IS NOT NULL``. The
``(workspace_id, product_id, title)`` variant identity targets the
**full** (non-partial) unique, so no ``index_where`` is passed. A single
statement can only ever infer one arbiter -- hence partitioning by
identity kind before building statements.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import Insert, insert as pg_insert
from sqlalchemy.sql import func

from app_shared.catalog.default_variant import derive_default_variant
from app_shared.models.catalog import Product, ProductVariant

IdentityKind = Literal["external_id", "sku", "product_title"]
Identity = tuple[IdentityKind, Any] | None

# Columns updated by `ON CONFLICT ... DO UPDATE SET ...` -- deliberately
# never `id`, `workspace_id`, `created_at` (immutable identity/audit
# columns). `updated_at` is refreshed separately via `func.now()` since a
# Core (non-ORM) upsert never fires the mapped column's Python-side
# `onupdate` callable.
_PRODUCT_UPDATABLE_COLUMNS: tuple[str, ...] = (
    "external_id",
    "sku",
    "title",
    "brand",
    "barcode",
    "url",
    "status",
)
_VARIANT_UPDATABLE_COLUMNS: tuple[str, ...] = (
    "product_id",
    "external_id",
    "sku",
    "barcode",
    "title",
    "option_values",
    "current_price",
    "currency",
    "url",
    "status",
)

# (index_elements, index_where) per identity kind -- MUST match the
# partial-unique-index predicates declared in `app_shared/models/catalog.py`
# exactly, or Postgres can't infer the arbiter.
_PRODUCT_CONFLICT_TARGETS: dict[IdentityKind, tuple[list[str], Any]] = {
    "external_id": (["workspace_id", "external_id"], text("external_id IS NOT NULL")),
    "sku": (["workspace_id", "sku"], text("sku IS NOT NULL")),
}
_VARIANT_CONFLICT_TARGETS: dict[IdentityKind, tuple[list[str], Any]] = {
    "external_id": (["workspace_id", "external_id"], text("external_id IS NOT NULL")),
    "sku": (["workspace_id", "sku"], text("sku IS NOT NULL")),
    "product_title": (["workspace_id", "product_id", "title"], None),
}


def resolve_identity(row: Mapping[str, Any], *, is_variant: bool = False) -> Identity:
    """First present of ``external_id`` -> ``sku`` -> (variants only) ``(product_id, title)``.

    Returns ``None`` for a row with no resolvable identity (an
    identity-less **product** -- always-insert, FR-011). A variant row
    is expected to always resolve (its ``title`` is a required field and
    its ``product_id`` is filled in by parent resolution before this is
    called); ``None`` is still possible if `product_id`/`title` are both
    absent, e.g. an as-yet-unresolved parent.
    """
    external_id = row.get("external_id")
    if external_id:
        return ("external_id", external_id)
    sku = row.get("sku")
    if sku:
        return ("sku", sku)
    if is_variant:
        product_id = row.get("product_id")
        title = row.get("title")
        if product_id is not None and title:
            return ("product_title", (product_id, title))
    return None


def dedup_last_wins(
    rows: Sequence[Mapping[str, Any]],
    key_fn: Callable[[Mapping[str, Any]], Any],
) -> list[Mapping[str, Any]]:
    """Collapse rows sharing a resolved (non-``None``) ``key_fn`` result, keeping the LAST.

    Stable order otherwise (FR-012): a later duplicate overwrites its
    earlier same-key row *in place* rather than moving to the end. Rows
    where ``key_fn`` returns ``None`` (identity-less) are never
    collapsed against each other -- each is always-insert and always
    distinct.
    """
    result: list[Mapping[str, Any]] = []
    position_by_key: dict[Any, int] = {}
    for row in rows:
        key = key_fn(row)
        if key is None:
            result.append(row)
            continue
        if key in position_by_key:
            result[position_by_key[key]] = row
        else:
            position_by_key[key] = len(result)
            result.append(row)
    return result


def _partition_by_identity_kind(
    rows: Sequence[Mapping[str, Any]], *, is_variant: bool
) -> dict[IdentityKind | None, list[dict[str, Any]]]:
    partitions: dict[IdentityKind | None, list[dict[str, Any]]] = {}
    for row in rows:
        identity = resolve_identity(row, is_variant=is_variant)
        kind = identity[0] if identity is not None else None
        partitions.setdefault(kind, []).append(dict(row))
    return partitions


def build_products_upsert(
    rows: Sequence[Mapping[str, Any]], identity_kind: IdentityKind | None
) -> Insert:
    """One ``pg_insert(Product).values([...])[.on_conflict_do_update(...)]``.

    ``identity_kind is None`` -> a plain insert with **no** ``ON
    CONFLICT`` clause at all (identity-less products always insert
    fresh, FR-011). Otherwise infers the matching partial/full unique
    index and updates every column in ``_PRODUCT_UPDATABLE_COLUMNS``
    from ``excluded`` (never ``id``/``workspace_id``/``created_at``).
    """
    stmt = pg_insert(Product).values(list(rows))
    if identity_kind is None:
        return stmt
    index_elements, index_where = _PRODUCT_CONFLICT_TARGETS[identity_kind]
    set_ = {col: stmt.excluded[col] for col in _PRODUCT_UPDATABLE_COLUMNS}
    set_["updated_at"] = func.now()
    return stmt.on_conflict_do_update(
        index_elements=index_elements, index_where=index_where, set_=set_
    )


def build_variants_upsert(
    rows: Sequence[Mapping[str, Any]], identity_kind: IdentityKind | None
) -> Insert:
    """Same as :func:`build_products_upsert`, targeting ``ProductVariant``.

    ``identity_kind="product_title"`` infers the **full** unique
    ``(workspace_id, product_id, title)`` (no ``index_where``).
    """
    stmt = pg_insert(ProductVariant).values(list(rows))
    if identity_kind is None:
        return stmt
    index_elements, index_where = _VARIANT_CONFLICT_TARGETS[identity_kind]
    set_ = {col: stmt.excluded[col] for col in _VARIANT_UPDATABLE_COLUMNS}
    set_["updated_at"] = func.now()
    return stmt.on_conflict_do_update(
        index_elements=index_elements, index_where=index_where, set_=set_
    )


def plan_upsert(
    rows: Sequence[Mapping[str, Any]], *, is_variant: bool = False
) -> list[Insert]:
    """Dedup (last-wins) + partition by identity kind + one statement per non-empty partition.

    **Bounded**: at most 3 statements per table (``external_id`` /
    ``sku`` / the third identity kind or identity-less-plain-insert),
    regardless of ``len(rows)`` -- no per-row loop (SC-003).
    """
    deduped = dedup_last_wins(rows, lambda r: resolve_identity(r, is_variant=is_variant))
    partitions = _partition_by_identity_kind(deduped, is_variant=is_variant)
    builder = build_variants_upsert if is_variant else build_products_upsert
    return [builder(bucket, kind) for kind, bucket in partitions.items() if bucket]


# --- Variant -> product parent resolution (one IN(...) lookup, router-driven) ----


def variant_parent_lookup_keys(
    variant_rows: Sequence[Mapping[str, Any]],
) -> tuple[set[str], set[str]]:
    """Which parent ``external_id``/``sku`` values need a DB lookup.

    A variant row that already carries an explicit ``product_id`` needs
    no lookup. Returns ``(external_id_values, sku_values)`` so the
    caller can build exactly **one** scoped
    ``select(Product.id, Product.external_id, Product.sku).where(... IN (...))``
    (`contracts/catalog-bulk-upsert.md` "Variant->product resolution").
    """
    external_ids: set[str] = set()
    skus: set[str] = set()
    for row in variant_rows:
        if row.get("product_id") is not None:
            continue
        parent_external_id = row.get("product_external_id")
        parent_sku = row.get("product_sku")
        if parent_external_id:
            external_ids.add(parent_external_id)
        elif parent_sku:
            skus.add(parent_sku)
    return external_ids, skus


def resolve_variant_parents(
    variant_rows: Sequence[Mapping[str, Any]],
    *,
    by_external_id: Mapping[str, uuid.UUID],
    by_sku: Mapping[str, uuid.UUID],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Fill ``product_id`` on each variant row from its parent identity.

    ``by_external_id``/``by_sku`` are the maps built from the single
    scoped lookup keyed by :func:`variant_parent_lookup_keys`. Returns
    ``(resolved, unresolved)`` -- ``unresolved`` rows named a parent
    identity absent from those maps; the caller rejects those via
    `app_shared.catalog.consistency` (workspace-consistency pre-check)
    instead of letting a composite-FK violation reach the DB as a raw
    ``IntegrityError``.
    """
    resolved: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for row in variant_rows:
        row = dict(row)
        if row.get("product_id") is not None:
            resolved.append(row)
            continue
        product_id: uuid.UUID | None = None
        parent_external_id = row.get("product_external_id")
        parent_sku = row.get("product_sku")
        if parent_external_id:
            product_id = by_external_id.get(parent_external_id)
        elif parent_sku:
            product_id = by_sku.get(parent_sku)
        if product_id is None:
            unresolved.append(row)
            continue
        row["product_id"] = product_id
        resolved.append(row)
    return resolved, unresolved


# --- Default-variant injection in bulk (FR-012 tail) -------------------------


def inject_default_variants(
    products: Sequence[Mapping[str, Any]],
    product_ids: Sequence[uuid.UUID],
) -> list[dict[str, Any]]:
    """One :func:`derive_default_variant` row per product arriving with zero explicit variants.

    ``products``/``product_ids`` are positionally paired (same length,
    same order -- the caller zips its upserted-products result against
    the deduped product batch it built). Products that already carry a
    non-empty ``variants`` list are skipped -- they get no extra default
    (FR-005/FR-012 tail: every *upserted* product still ends with >=1
    variant, but only zero-variant arrivals get one synthesized).
    """
    extra: list[dict[str, Any]] = []
    for product, product_id in zip(products, product_ids, strict=True):
        if product.get("variants"):
            continue
        variant = derive_default_variant(product)
        variant["product_id"] = product_id
        extra.append(variant)
    return extra
