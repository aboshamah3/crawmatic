# Contract: Competitor/Match ORM Models (`app_shared/models/competitors_matches.py`)

Framework-agnostic SQLAlchemy models (no FastAPI). Both use `WorkspaceScopedBase` (`workspace_id NOT NULL`, indexed) + are added to `WORKSPACE_OWNED_MODELS` (`repository.py`). Shapes are the **exact** §22 shapes (see `data-model.md`). `competitors` constraint names come from the SPEC-02 `NAMING_CONVENTION`; `competitor_product_matches` uses **explicit** short names because the convention would exceed Postgres's 63-byte identifier cap (research D5).

## Models
- `Competitor(Base, WorkspaceScopedBase, TimestampMixin)` — `name`, `domain`, `status(CompetitorStatus)`, `legal_status(LegalStatus)`, `robots_policy(RobotsPolicy)`, `default_scrape_profile_id?(Uuid, no FK)`, `default_access_policy_id?(Uuid, no FK)`, `max_concurrent_requests?(Integer)`, `max_requests_per_minute?(Integer)`.
- `CompetitorProductMatch(Base, WorkspaceScopedBase, TimestampMixin)` — `product_id`, `product_variant_id`, `competitor_id`, `competitor_url`, `normalized_competitor_url`, `url_pattern`, `url_pattern_version(Integer)`, `competitor_variant_identifier?`, `competitor_variant_sku?`, `competitor_variant_options?(JSONB)`, `external_title?`, `scrape_profile_id?(Uuid, no FK)`, `access_policy_id?(Uuid, no FK)`, `priority(MatchPriority)`, `status(MatchStatus)`, `health_status(HealthStatus)`, `last_error_code?`, `consecutive_failures(Integer)`, `success_rate_7d?(NUMERIC(5,4))`, `current_price_id?(Uuid, soft ref, no FK)`, `last_scraped_at?`, `last_success_at?`, `last_failed_at?`.

## `__table_args__` per model
- **Competitor**: `UniqueConstraint("workspace_id","id")`; `UniqueConstraint("workspace_id","domain")`; `ForeignKeyConstraint(["workspace_id"],["workspaces.id"])`.
- **CompetitorProductMatch** (explicit names):
  - `UniqueConstraint("workspace_id","product_variant_id","competitor_id","normalized_competitor_url", name="uq_cpm_ws_variant_competitor_norm_url")`.
  - `ForeignKeyConstraint(["workspace_id","product_id"], ["products.workspace_id","products.id"], name="fk_cpm_workspace_product_products")`.
  - `ForeignKeyConstraint(["workspace_id","product_variant_id"], ["product_variants.workspace_id","product_variants.id"], name="fk_cpm_workspace_variant_variants")`.
  - `ForeignKeyConstraint(["workspace_id","competitor_id"], ["competitors.workspace_id","competitors.id"], name="fk_cpm_workspace_competitor_competitors")`.
  - `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_cpm_workspace_workspaces")`.

The three entity FKs are workspace-local composite FKs (`(workspace_id, ref_id) → parent(workspace_id, id)`), so cross-workspace references are structurally impossible. `current_price_id`/`scrape_profile_id`/`access_policy_id` carry **no** FK.

## Defaults (set on the ORM column via `default=`, so a client need not supply them)
- Competitor: `status=ACTIVE`, `legal_status=REVIEW_REQUIRED`, `robots_policy=RESPECT`.
- Match: `priority=NORMAL`, `status=ACTIVE`, `health_status=UNKNOWN`, `consecutive_failures=0`; `success_rate_7d`/`current_price_id`/`last_error_code`/`last_*_at` default null (FR-017).

## Registration
- `models/__init__.py` re-exports `Competitor`, `CompetitorProductMatch` (so `Base.metadata` sees them for Alembic offline-render; callers can `from app_shared.models import Competitor, ...`).
- `repository.py`: `WORKSPACE_OWNED_MODELS |= {Competitor, CompetitorProductMatch}` (the `ModelT` bound is already `Base` from SPEC-04 — no change).

## Unit tests (no DB)
- Column presence/types/nullability match §22; both timestamps are `TZDateTime` (naive-datetime guard passes); `url_pattern_version`/`consecutive_failures` are `Integer`; `success_rate_7d` is `NUMERIC(5,4)`; status/enum columns render `VARCHAR(32)`.
- Unique keys present: `competitors(workspace_id,domain)`, `competitors(workspace_id,id)`, the 4-col match unique.
- Composite FKs reference `(workspace_id, id)` parents; `competitors` carries `unique(workspace_id, id)`.
- **Every** emitted constraint/index name is ≤63 bytes (explicit `cpm` names verified).
- Health/ status/priority defaults are as specified.
- `WORKSPACE_OWNED_MODELS` contains both; scoped helpers raise without a `workspace_id`.
