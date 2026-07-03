# Contract: observation/current-price models (`app_shared.models.observations`)

Three ORM models (exact §22 shapes; full column tables in data-model.md). All workspace-owned, RLS-protected, registered in `WORKSPACE_OWNED_MODELS`, re-exported from `app_shared.models.__init__` for Alembic `target_metadata`.

## `PriceObservation` — `price_observations` (PARTITIONED)

- `WorkspaceScopedBase` + explicit columns; money via `Money` (`NUMERIC(18,4)`); `extraction_confidence` `Numeric(5,4)`; `currency` `CHAR(3)`; enum columns via `enum_column` (`StockStatus`, `ExtractionMethod`, `ScrapeErrorCode`).
- `__table_args__ = ({"postgresql_partition_by": "RANGE (scraped_at)"},)` (+ any Index/FK entries).
- `scraped_at` declared `primary_key=True` → composite `PRIMARY KEY (id, scraped_at)` (partition key in PK — Postgres rule for partitioned tables).
- `workspace_id` FK → `workspaces.id`; `match_id`/`product_id`/`product_variant_id`/`scrape_job_id` are soft refs (no FK).

## `RequestAttempt` — `request_attempts` (PARTITIONED)

- Same pattern; `__table_args__ = ({"postgresql_partition_by": "RANGE (created_at)"},)`; composite `PRIMARY KEY (id, created_at)`.
- `access_method` (`AccessMethod`), `status_code`/`response_time_ms`/`attempt_number` ints, `error_code` (`ScrapeErrorCode`).

## `MatchCurrentPrice` — `match_current_prices` (current-state)

- `WorkspaceScopedBase` + `TimestampMixin`; single-column PK (`id`).
- `UniqueConstraint("workspace_id", "match_id", name="uq_match_current_prices_workspace_id_match_id")` — the upsert conflict arbiter.
- `observation_id` is a **soft** ref (no FK) — tolerates a dropped old partition (§22).

## Correctness invariants (Principle VII/VIII)

- Money only through `Money`/`parse_money` — float/NaN/Infinity/over-scale/non-positive never stored.
- All timestamps `TZDateTime` (naive rejected by the base guard).
- Enum-likes are app-validated `VARCHAR`, never DB enums.
- Composite PK includes the partition key on both partitioned tables.

## Tests (unit)

Column/type shapes; composite PK incl. partition key; `postgresql_partition_by` present; `unique(workspace_id, match_id)`; `Money`→`NUMERIC(18,4)` / `Numeric(5,4)` / `CHAR(3)`; enum columns render `VARCHAR`; all three appear in `WORKSPACE_OWNED_MODELS`; the scoping CI guard flags a planted unscoped select on each.
