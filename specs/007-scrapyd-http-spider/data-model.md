# Phase 1 Data Model: Scrapyd HTTP Spider MVP

Three new tables in `libs/shared/app_shared/models/observations.py`, exact §22 shapes. Two are **monthly-partitioned from birth** (composite PK includes the partition key); one is a current-state table. All three are workspace-owned (`WorkspaceScopedBase`), added to `WORKSPACE_OWNED_MODELS`, and given `emit_rls_policy` in the creating migration. Money columns use `app_shared.money.Money` (`NUMERIC(18,4)`, float/NaN/Infinity/over-scale rejected). Enum-like columns use `enum_column` (app-validated `VARCHAR`, never a DB enum). Timestamps are `TZDateTime` (`TIMESTAMPTZ`, naive rejected).

New enums added to `app_shared.enums` (all `StrEnum` → `VARCHAR`):
- `AccessMethod`: `DIRECT_HTTP`, `DIRECT_HTTP_RETRY`, `PROXY_HTTP`, `PLAYWRIGHT_PROXY` (§11). This slice only ever writes `DIRECT_HTTP`.
- `StockStatus`: `IN_STOCK`, `OUT_OF_STOCK`, `UNKNOWN` (§22).
- `ExtractionMethod`: `JSON_LD`, `CSS`, `REGEX`, `SINGLE_NUMBER` (+ forward-compat `PLATFORM_JSON`, `EMBEDDED_JSON`, `XPATH`, `PLAYWRIGHT`). This slice writes `JSON_LD`/`CSS`/`REGEX`/`SINGLE_NUMBER`.
- `ScrapeErrorCode`: the §34 vocabulary (see contracts/errors.md).

---

## Entity: PriceObservation (`price_observations`) — PARTITIONED

Immutable record of one extraction attempt result. **Monthly-partitioned by `scraped_at`**; composite `PRIMARY KEY (id, scraped_at)`; `__table_args__` carries `postgresql_partition_by="RANGE (scraped_at)"`.

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK part 1 (default `new_uuid7`) |
| `scraped_at` | TIMESTAMPTZ | no | **PK part 2 = partition key**; extraction timestamp |
| `workspace_id` | UUID | no | indexed; FK → `workspaces.id`; RLS column |
| `match_id` | UUID | no | soft ref to `competitor_product_matches` (no FK — partition/retention tolerance) |
| `product_id` | UUID | no | soft ref |
| `product_variant_id` | UUID | no | soft ref (variant-level pricing) |
| `scrape_job_id` | UUID | yes | correlation ref (nullable, §22) |
| `price` | `Money` NUMERIC(18,4) | yes | null on failure observations |
| `old_price` | `Money` NUMERIC(18,4) | yes | |
| `currency` | CHAR(3) | yes | ISO code |
| `stock_status` | `StockStatus` VARCHAR | yes | |
| `raw_title` | TEXT | yes | |
| `success` | BOOL | no | `false` for failure/rejection observations |
| `comparable` | BOOL | no | `false` on `CURRENCY_MISMATCH` (excluded from comparison) |
| `error_code` | `ScrapeErrorCode` VARCHAR | yes | set when `success=false` |
| `error_message` | TEXT | yes | |
| `extraction_method` | `ExtractionMethod` VARCHAR | yes | `JSON_LD`/`CSS`/`REGEX`/`SINGLE_NUMBER` |
| `extraction_confidence` | NUMERIC(5,4) | yes | plain `Numeric(5,4)` (a score in [0,1], **not** money) |
| `selector_used` | TEXT | yes | the selector/regex/JSON path that produced the value |

**Validation rules** (enforced before insert, in `scrape_core.validation` / the extractor):
- `price` is an exact `Decimal` from `parse_money` — float/NaN/Infinity/over-scale/non-positive never stored (a rejected candidate produces a `success=false` observation with no price).
- `extraction_confidence` ∈ [0,1]; below `min_accepted_confidence` (default 0.75) → `success=false`, `error_code=LOW_CONFIDENCE_PRICE`.
- `comparable=false` iff currency mismatch (`error_code=CURRENCY_MISMATCH` recorded as a warning; row still saved).

Indexes: `(workspace_id)`, `(match_id)`, `(scraped_at)` on the parent (propagate to partitions).

---

## Entity: RequestAttempt (`request_attempts`) — PARTITIONED

Audit record of a single HTTP fetch attempt. **Monthly-partitioned by `created_at`**; composite `PRIMARY KEY (id, created_at)`; `postgresql_partition_by="RANGE (created_at)"`.

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK part 1 |
| `created_at` | TIMESTAMPTZ | no | **PK part 2 = partition key** |
| `workspace_id` | UUID | no | indexed; FK → `workspaces.id`; RLS column |
| `scrape_job_id` | UUID | yes | correlation ref |
| `match_id` | UUID | no | soft ref |
| `attempt_number` | INT | no | 1 for this slice (retry chain later) |
| `url` | TEXT | no | the fetched URL |
| `access_method` | `AccessMethod` VARCHAR | no | `DIRECT_HTTP` only in this slice |
| `proxy_provider_id` | UUID | yes | unused here (proxies later) |
| `proxy_country` | TEXT | yes | unused here |
| `status_code` | INT | yes | HTTP status (null on DNS/timeout/SSRF pre-fetch) |
| `response_time_ms` | INT | yes | |
| `success` | BOOL | no | |
| `error_code` | `ScrapeErrorCode` VARCHAR | yes | e.g. `HTTP_404`, `TIMEOUT`, `DNS_ERROR`, `BLOCKED` |
| `error_message` | TEXT | yes | |

Exactly **one** `request_attempt` is written per attempted target (FR-013). Indexes: `(workspace_id)`, `(match_id)`, `(created_at)`.

---

## Entity: MatchCurrentPrice (`match_current_prices`) — CURRENT-STATE (not partitioned)

Latest-known price snapshot per match. `unique(workspace_id, match_id)`; upserted on every successful observation. Created here if not already present (it is not — no prior model exists).

| Column | Type | Null | Notes |
|--------|------|------|-------|
| `id` | UUID (uuidv7) | no | PK |
| `workspace_id` | UUID | no | indexed; FK → `workspaces.id`; RLS column |
| `match_id` | UUID | no | part of `unique(workspace_id, match_id)` |
| `product_id` | UUID | no | soft ref |
| `product_variant_id` | UUID | no | soft ref |
| `competitor_id` | UUID | no | soft ref |
| `price` | `Money` NUMERIC(18,4) | yes | |
| `old_price` | `Money` NUMERIC(18,4) | yes | |
| `currency` | CHAR(3) | yes | |
| `stock_status` | `StockStatus` VARCHAR | yes | |
| `comparable` | BOOL | no | |
| `observation_id` | UUID | yes | **soft** ref to the winning `price_observations` row (no FK — may dangle after retention drop, §22) |
| `success` | BOOL | no | |
| `error_code` | `ScrapeErrorCode` VARCHAR | yes | |
| `extraction_method` | `ExtractionMethod` VARCHAR | yes | |
| `extraction_confidence` | NUMERIC(5,4) | yes | |
| `scraped_at` | TIMESTAMPTZ | no | observation time of the current value |
| `updated_at` | TIMESTAMPTZ | no | `TimestampMixin` `onupdate` |

**Constraints**: `UniqueConstraint("workspace_id", "match_id", name="uq_match_current_prices_workspace_id_match_id")` — the upsert conflict arbiter. `created_at` from `TimestampMixin` is retained (append/update timestamp) alongside the explicit `scraped_at`.

**Upsert behavior**: on a **successful, validated** observation the row is `insert(...).on_conflict_do_update` on `(workspace_id, match_id)` with the new price/currency/stock/comparable/`observation_id`/method/confidence/`scraped_at`. On a **failure** observation the current price is **not** overwritten with a bad value (FR-014) — the failure is recorded on the observation/attempt only. (Whether a failure updates `success`/`error_code` here without touching `price` is left to the pipeline; the price fields are never clobbered by a rejected candidate.)

---

## Relationships & isolation

- All three carry `workspace_id` (RLS + app scoping). References to `match`/`product`/`variant`/`competitor`/`observation` are **soft** (plain indexed UUID columns, no FK) — matching §22's soft-reference philosophy and avoiding FK-into/among-partitioned-table complications with retention.
- `workspace_id` has a real FK to `workspaces.id` (the RLS anchor); this is safe and non-partition-crossing.
- Cross-workspace isolation is enforced by (1) `scoped_select`/`scoped_get` (app layer, CI-guarded) and (2) DB RLS on all three (`emit_rls_policy`, fail-closed) — the two-layer model.

## Partitioning summary

| Table | Partition by | Composite PK | Initial partitions (migration) | RLS |
|-------|-------------|--------------|--------------------------------|-----|
| `price_observations` | `RANGE (scraped_at)` monthly | `(id, scraped_at)` | current + next month | on parent (propagates) |
| `request_attempts` | `RANGE (created_at)` monthly | `(id, created_at)` | current + next month | on parent (propagates) |
| `match_current_prices` | — (not partitioned) | `(id)` | — | standard `emit_rls_policy` |

## Transport shapes (not tables)

- `scrape_core.extraction.result.ExtractionCandidate` — `raw_price_text`, `currency`, `method: ExtractionMethod`, `confidence: float`, `selector_used`, `raw_title`, `stock: StockStatus | None`, `matched_text` (surrounding text for `reject_if_text_contains`).
- `scrape_core.items.ScrapeResult` — the Scrapy item flowing to the pipeline: the full observation field set + the request-attempt field set + `workspace_id`/`match_id`/`product_id`/`product_variant_id`/`competitor_id`/`scrape_job_id`, so a single item yields one observation + one attempt (+ possibly one current-price upsert).
