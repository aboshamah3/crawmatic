# Quickstart & Validation: Scrapyd HTTP Spider MVP

How to validate this feature. Two tiers: **unit** (runs in this build env — no Postgres/Redis/Scrapyd) and **live** (authored + skip-marked, runs on a full-stack host). No test contacts a real competitor domain (FR-021/SC-007).

## Prerequisites

- uv workspace synced: `uv sync`.
- Unit tier needs nothing else. Live tier needs a reachable Postgres (via PgBouncer), Redis, and a Scrapyd HTTP node with basic auth (env: `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `REDIS_URL`, `SCRAPYD_HTTP_URLS`, `SCRAPYD_USERNAME`, `SCRAPYD_PASSWORD`).

## Unit validation (this env)

```bash
uv run pytest tests/unit -q
```

Covers (maps to Success Criteria):
- **Extraction** (`test_extraction_jsonld_css_regex.py`) — JSON-LD/CSS/regex fixtures each extract with the expected default confidence (0.95/0.85/0.75); fallback order jsonld→css→regex; `PRICE_NOT_FOUND`; single unlabeled number → 0.40 (SC-003).
- **Price validation** (`test_price_validation.py`) — Decimal exactness; float/NaN/Infinity/over-scale/non-positive rejected (never rounded); currency mismatch → `comparable=false`+`CURRENCY_MISMATCH`; min/max; `reject_if_text_contains`; confidence < 0.75 → `LOW_CONFIDENCE_PRICE` (SC-003/SC-004).
- **Fetch-time SSRF** (`test_fetch_url_safety.py`) — injected public IP accepted; private/loopback/link-local/unique-local/metadata resolved IP rejected; each redirect hop re-validated; scheme/userinfo rejected pre-fetch (SC-002).
- **Batching** (`test_persistence_batching.py`) — flush at N items / T seconds / final flush at close; N items → ≪ N flushes; DB routed through the `deferToThread` seam (SC-006).
- **Reactor-safe DB** (`test_reactor_safe_db.py`) — `run_in_thread` offloads; `workspace_txn` sets context.
- **Robots** (`test_robots_middleware.py`) — `RESPECT` skips disallowed; policy per-request, not global.
- **Dispatch** (`test_scrapyd_dispatch.py`) — basic auth + args → jobid; wrong creds → 401, no schedule; `SET NX` idempotency no-op on retry (SC-005).
- **Models / migration render** (`test_observations_models.py`, `test_rls_observations.py`, `test_migration_offline_observations.py`) — composite PK incl. partition key; `postgresql_partition_by`; `unique(workspace_id, match_id)`; RLS DDL for all three; `alembic upgrade head --sql` renders `PARTITION BY` + initial partitions; single head.
- **Boundaries / scoping** (`test_import_boundaries.py`, `test_observations_scoping_guard.py`) — `scrape_core.*` covered; `app_shared.observations`/`scrapyd.client` import no scrapy/twisted; unscoped select on the new models flagged.

## Migration render (offline, no DB)

```bash
SPECIFY_FEATURE_DIRECTORY=specs/007-scrapyd-http-spider uv run alembic upgrade head --sql
```

Expect the two `PARTITION BY RANGE` parents, current+next month `PARTITION OF` tables, `match_current_prices` with `unique(workspace_id, match_id)`, and the RLS statements for all three. See contracts/migration-observations.md.

## Live validation (full-stack host — skip-marked here)

```bash
uv run alembic upgrade head            # applies the partitioned tables + partitions + RLS
uv run pytest tests/integration -q     # runs only where Postgres/Redis/Scrapyd are reachable
```

Scenarios (map to User Stories):
1. **US1 / SC-001** (`test_spider_jsonld_fixture_live.py`) — seed one workspace/product/variant/competitor/match/profile; serve a JSON-LD fixture at the match URL (loopback fixture server, resolver allowlisted for it); schedule `generic_price_spider` with `workspace_id`/`scrape_job_id`/`match_ids`; assert exactly one `price_observations` row (correct price/currency, `extraction_method=JSON_LD`, confidence ≥ 0.75, `success=true`), `match_current_prices` upserted, one `request_attempt`.
2. **US2 / SC-002** (`test_spider_ssrf_live.py`) — match URL resolving to a private IP (and a public→internal 302): refused before body download; no `success=true` observation; failure recorded with `BLOCKED`.
3. **US3 / SC-003** (`test_spider_strategies_live.py`) — CSS-only and regex-only fixtures → expected method+confidence; a discount/"save X"-only fixture rejected.
4. **US4 / SC-005** (`test_dispatch_scrapyd_live.py`) — authenticated `schedule.json` returns a jobid; unauthenticated rejected; retried dispatch of the same `(scrape_job_id, batch_index)` does not double-run.
5. **US5 / SC-006** (`test_spider_batch_live.py`) — N fixture matches → all N observations persist with commit count ≪ N; DB off the reactor thread.
6. **Isolation** (`test_observations_isolation_live.py`) — cross-workspace read/write blocked (app scoping + RLS); no-context → 0 rows.

## Not in scope (later specs)

No alert/variant-state/webhook/`price_analysis` computation; no `variant_price_states`; no `scrape_jobs`/`scrape_job_targets` tables (FR-015 recorded as a deferred seam — plan.md Complexity Tracking); no proxies/rate-limiter/in-flight-dedup/domain-strategy-optimizer/browser spider.
