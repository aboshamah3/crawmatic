# Contract: Catalog ORM Models (`app_shared/models/catalog.py`)

Framework-agnostic SQLAlchemy models (no FastAPI). All four use `WorkspaceScopedBase` (`workspace_id NOT NULL`, indexed) + are added to `WORKSPACE_OWNED_MODELS` (`repository.py`). Shapes are the **exact** §22 shapes (see `data-model.md`). Constraint names come from SPEC-02 `NAMING_CONVENTION` (all-columns tokens → distinct names for the two same-leading-column partial uniques).

## Models
- `Product(Base, WorkspaceScopedBase, TimestampMixin)` — `external_id?`, `sku?`, `title`, `brand?`, `barcode?`, `url?`, `status(ProductStatus)`. No price column (Principle III).
- `ProductVariant(Base, WorkspaceScopedBase, TimestampMixin)` — `product_id`, `external_id?`, `sku?`, `barcode?`, `title`, `option_values?(JSONB)`, `current_price(Money)`, `currency(CHAR(3))`, `url?`, `status(VariantStatus)`.
- `ProductGroup(Base, WorkspaceScopedBase, TimestampMixin)` — `name`, `description?`, `status(GroupStatus)`.
- `ProductGroupItem(Base, WorkspaceScopedBase)` — `product_group_id`, `product_id?`, `product_variant_id?`, `created_at` (bare `TZDateTime`, **no** `updated_at`).

## `__table_args__` per model (indexes/constraints)
- **Product**: `UniqueConstraint("workspace_id","id")`; `Index("uq_products_workspace_id_external_id","workspace_id","external_id", unique=True, postgresql_where=text("external_id IS NOT NULL"))`; same for `sku`; FK `workspace_id → workspaces.id`.
- **ProductVariant**: `UniqueConstraint("workspace_id","id")`; two partial uniques (external_id, sku); `UniqueConstraint("workspace_id","product_id","title")`; **composite FK** `("workspace_id","product_id") → products("workspace_id","id")`; FK `workspace_id → workspaces.id`.
- **ProductGroup**: `UniqueConstraint("workspace_id","id")`; `UniqueConstraint("workspace_id","name")`; FK `workspace_id → workspaces.id`.
- **ProductGroupItem**: two partial uniques (`(ws,group,product) WHERE product_id IS NOT NULL`, `(ws,group,variant) WHERE product_variant_id IS NOT NULL`); three composite FKs (group, product, variant); FK `workspace_id → workspaces.id`.

## Registration
- `models/__init__.py` re-exports the four (so `Base.metadata` sees them; callers can `from app_shared.models import Product, ...`).
- `repository.py`: `WORKSPACE_OWNED_MODELS |= {Product, ProductVariant, ProductGroup, ProductGroupItem}`; `ModelT` widened to `TypeVar("ModelT", bound=Base)`.

## Unit tests (no DB)
- Column presence/types/nullability match §22; `ProductGroupItem` has no `updated_at`.
- Partial-index `postgresql_where` renders `... IS NOT NULL`; constraint names follow the convention (two same-leading uniques get distinct names).
- Composite FKs reference `(workspace_id, id)` parents; parents carry `unique(workspace_id, id)`.
- Naive-datetime guard passes (all timestamps are `TZDateTime`); Money column is `NUMERIC(18,4)`; status columns render `VARCHAR(32)`.
- `WORKSPACE_OWNED_MODELS` contains all four; scoped helpers raise without a `workspace_id`.
