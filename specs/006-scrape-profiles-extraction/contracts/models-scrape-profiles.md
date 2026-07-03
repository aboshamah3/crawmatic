# Contract: `ScrapeProfile` ORM model (`app_shared/models/scrape_profiles.py`)

Declares ORM shape only — RLS + FK promotions live in the creating migration (`migration-scrape-profiles.md`). See `data-model.md` for the full column table.

## Shape

- `class ScrapeProfile(Base, TimestampMixin)` — **not** `WorkspaceScopedBase` (dual-scope, research D2).
- `__tablename__ = "scrape_profiles"`.
- `workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)` — NULL = global default.
- `name`, `mode` (`enum_column(ScrapeProfileMode, default=HTTP)`), `adapter_key` (`enum_column(AdapterKey, default="default_http")`), three `*_enabled` `Boolean(default=True)`, the nullable extraction `*_selector`/`*_xpath`/`*_regex` + `title_*`, `variant_strategy` (`enum_column(VariantStrategy, default=PAGE_SINGLE_PRICE)`), JSONB `variant_selector_config`/`price_transform_rules`/`validation_rules`/`confidence_rules`/`headers`/`cookies`, `wait_for_selector`, `request_timeout_ms Integer(default=30000)`, `browser_timeout_ms Integer nullable`.

## Constraints (`__table_args__`)

```python
Index("uq_scrape_profiles_workspace_id_name", "workspace_id", "name",
      unique=True, postgresql_where=text("workspace_id IS NOT NULL")),
Index("uq_scrape_profiles_name_global", "name",
      unique=True, postgresql_where=text("workspace_id IS NULL")),
ForeignKeyConstraint(["workspace_id"], ["workspaces.id"],
      name="fk_scrape_profiles_workspace_id_workspaces"),
```

(The `workspace_id` index comes from `index=True` on the column → `ix_scrape_profiles_workspace_id`.)

## Rules

- Enum columns render as plain `VARCHAR(32)` (never PG-native), validated at bind/result by `enum_column`.
- Not added to `WORKSPACE_OWNED_MODELS`; queried only through `app_shared.profiles.repository` (dual-scope helpers).
- Re-exported from `app_shared/models/__init__.py` so `Base.metadata` (Alembic `target_metadata`) sees the table.
- All constraint/index names ≤63 bytes (all ≤38 here — no explicit shortening).

## Tests (unit, no DB)

- Column set/types/nullability match `data-model.md`; `workspace_id` is nullable + indexed.
- Both partial unique indexes exist with the exact `postgresql_where` predicates.
- Documented defaults present (mode/adapter/three enabled/variant_strategy/request_timeout_ms).
- All names ≤63 bytes; enums reject out-of-set values.
- Import-boundary: module imports no fastapi/scrapy/twisted/playwright.
