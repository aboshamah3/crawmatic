# Price Monitoring Backend — Project Spec

## 1. Purpose

Build an internal-first, SaaS-ready backend for competitor price monitoring.

The backend monitors a client store’s products and variants against manually matched competitor product URLs. It is API-first so a future WooCommerce plugin, Salla integration, n8n workflow, admin script, or dashboard can control products, variants, competitors, matches, scrape jobs, schedules, and results.

There is no frontend required in the first implementation.

The system must support:

- Multiple workspaces/clients.
- Strong workspace isolation.
- Variant-level price monitoring.
- Manual competitor URL matching now.
- Automatic product matching later.
- DB-driven scraping and access configuration.
- Internal proxy/browser handling.
- Mature open-source scraping infrastructure.
- Dynamic schedules.
- API-triggered refreshes.
- Webhook/event readiness.
- Future SaaS expansion.

---

## 2. Non-Negotiable Principles

1. **Variant-level pricing is required.** Products are parent catalog items; variants are the sellable/priced units.
2. **Every simple product gets one default variant.** This keeps pricing, matching, and alerts consistent.
3. **Competitor matches link to variants.** A match is one competitor URL linked to one product variant.
4. **Configuration lives mostly in the database.** Code provides the engine; DB controls what to scrape, how to scrape, how often, and with what access policy.
5. **No external scraping APIs/unlocker services in v1.** Do not use ScrapingBee, Zyte API, Bright Data Unlocker, or similar services.
6. **Internal-only access methods.** Direct HTTP, direct retry, internal proxy HTTP, and internal browser/Playwright through proxy.
7. **Use Scrapy as the mature scraping engine.** Do not run Scrapy directly inside Celery task processes. Use Scrapyd-managed Scrapy services.
8. **FastAPI/Celery orchestrate; Scrapyd/Scrapy scrape.**
9. **Do not use browser scraping by default.** Browser scraping is a selective fallback.
10. **Do not store raw HTML or screenshots in v1.** Store extracted observations, request attempts, errors, and strategy stats.
11. **Use Decimal/NUMERIC for money.** Never use floats for prices.
12. **Do not compare different currencies in v1.** Save mismatched observations but mark them not comparable.
13. **Distributed domain rate limiting is required before scale.** Per-worker limits are not enough.
14. **Prevent duplicate in-flight scrapes.**
15. **Use current-state tables for hot reads.** Do not scan historical observations for every analysis.
16. **Append-heavy tables need partitioning/retention before production volume.**
17. **Use public product pages only.** No login bypass, CAPTCHA solving, paywall bypass, private account scraping, or credentialed competitor scraping.
18. **Spiders scrape and persist observations only.** Price analysis, alert-state transitions, and webhook emission run as separate Celery `price_analysis` tasks, never inline in the spider.
19. **All DB access inside spiders must be non-blocking.** Use an async driver bound to the Twisted reactor, or offload every DB call with `deferToThread`. Never block the reactor with synchronous commits.
20. **Route every Postgres connection through PgBouncer (transaction pooling).** Cap per-process SQLAlchemy pools. Direct Postgres connections from spider processes are not allowed at scale.
21. **Avoid hot-row contention.** Per-variant analysis must be coalesced (one task per variant per job), and parent-job progress counters must be aggregated, not incremented once per target.
22. **Dispatch to Scrapyd must be idempotent.** Celery is at-least-once; guard every `schedule.json` call so a retried dispatch cannot run the same batch twice.
23. **Every user-supplied URL is treated as hostile.** Competitor match URLs and webhook endpoint URLs must pass SSRF-safe validation: `http`/`https` schemes only, and private, loopback, link-local, and internal-service addresses are rejected against the resolved IP at connection time (not only at save time).
24. **All timestamps are `TIMESTAMPTZ` (UTC).** Naive timestamp columns are forbidden at the column-type level.
25. **Append-heavy tables are born partitioned.** `price_observations`, `request_attempts`, `webhook_events`, and `price_alert_events` are created as monthly-partitioned tables in the migration that first creates them — never converted later. Other tables reference them only by soft reference (plain UUID column, no FK).
26. **No per-request hot-row writes.** Anything touched on every request or attempt (API key `last_used_at`, strategy attempt stats) is buffered or throttled, never written row-per-event.
27. **Row-Level Security ships with every workspace-owned table.** RLS policies are part of the same migration that creates the table, using transaction-scoped `SET LOCAL app.workspace_id`, with fail-closed behavior when the setting is absent.

---

## 3. Final Stack

```text
Language: Python

API:
- FastAPI

Database:
- PostgreSQL

ORM and migrations:
- SQLAlchemy
- Alembic

Queue/orchestration:
- Celery
- Redis

Scheduler:
- Custom DB-driven scheduler service

Scraping:
- Scrapy
- Scrapyd

Browser fallback:
- scrapy-playwright
- Playwright browsers in separate browser scraping service

Deployment:
- Railway or similar multi-service platform
- Pinned image versions for all infrastructure containers (no `latest` tags)

Packaging:
- uv workspace (single root pyproject, one lockfile, per-service installs)

Security primitives:
- Password hashing: argon2id (or bcrypt)
- API keys: high-entropy random secret, stored as SHA-256 hash with a short prefix for lookup
- Field encryption: Fernet, with key_version for rotation

Runtime shape:
- Monorepo
- Multiple deployable services
```

---

## 4. Deployment Services

Plan for the full multi-service architecture from the beginning.

Services:

```text
api-service
scheduler-service
worker-service
scrapyd-http-service
scrapyd-browser-service
pgbouncer
postgres
redis
```

### api-service

Purpose:

- Public API.
- Auth.
- API keys.
- Workspace management.
- Product/variant management.
- Competitor/match management.
- Scrape profile configuration.
- Access policy configuration.
- Job trigger endpoints.
- Job/result/alert endpoints.
- Webhook/event endpoints.

Start command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

### scheduler-service

Purpose:

- Reads `refresh_rules` from DB.
- Claims due schedules safely.
- Creates scrape jobs.
- Enqueues Celery dispatch tasks.
- Updates next run time.

Start command:

```bash
python -m app.scheduler.scheduler_app
```

### worker-service

Purpose:

- Celery orchestration worker.
- Expands jobs into targets.
- Calls Scrapyd schedule API.
- Runs price analysis tasks.
- Creates/updates alert state.
- Handles webhook/event creation.
- Handles maintenance tasks.

Start command:

```bash
celery -A app.workers.celery_app worker --loglevel=info
```

### scrapyd-http-service

Purpose:

- Runs Scrapy spiders for normal HTTP scraping.
- Should handle most pages.

Start command:

```bash
scrapyd
```

This service contains the Scrapy project at build time. Do not rely on runtime spider uploads in production.

Scrapyd's HTTP API includes `addversion.json`, which accepts code uploads — leaving it open is remote code execution for anything that can reach the node. Enable Scrapyd basic auth on both scraping services and have the worker authenticate every `schedule.json` call. Internal-only networking is required but is not, by itself, sufficient.

### scrapyd-browser-service

Purpose:

- Runs Scrapy spiders with `scrapy-playwright`.
- Handles JS-heavy pages and variant-selection pages.
- Used only for matches/profiles that require browser mode.

Start command:

```bash
scrapyd
```

This service image must install Playwright browsers during build. Keep browser concurrency low.

### pgbouncer

Connection pooler in front of Postgres, in **transaction pooling** mode.

Reason:

- api, scheduler, worker, and both Scrapyd services connect to Postgres. Each Scrapyd spider runs as its own process with its own pool, so direct connections multiply quickly and exhaust Postgres `max_connections`.
- All services connect to `pgbouncer`, not directly to `postgres`.

Constraints introduced by transaction pooling (design around these now):

- No session-scoped state across statements: session-level advisory locks and `SET` (without `LOCAL`) do not survive between transactions. Use `pg_advisory_xact_lock` (transaction-scoped) and `SET LOCAL` only.
- Prepared-statement caching must be disabled or made pooler-safe in the driver.
- Row-Level Security (Section 32) must use `SET LOCAL app.workspace_id` inside the same transaction as the query.

Additional PgBouncer rules:

- Authentication is `scram-sha-256` with a real userlist in every deployed environment; `trust` is acceptable only on a developer's local machine.
- Pin the PgBouncer image version.
- Do the connection budget before enabling large scrape jobs: PgBouncer `default_pool_size` vs. the sum of every process's `DB_POOL_SIZE + DB_MAX_OVERFLOW` — including one pool per Scrapyd spider process and per Celery worker process. Size `max_client_conn` and per-service caps from that math, and monitor pooler saturation (Section 31).

Engine/process hygiene (every service):

- One SQLAlchemy engine per process, created lazily on first use — never at import time and never per request. Health/readiness checks reuse the process engine; a per-request engine leaks pooled connections.
- Celery prefork workers dispose any engine inherited from the parent process on `worker_process_init` before first use (fork-safety).
- The one-shot migration job is the single allowed exception to PgBouncer routing: it connects **directly to Postgres**, because session-level advisory locks and non-transactional DDL (e.g. `CREATE INDEX CONCURRENTLY`) are unsafe through transaction pooling.

### postgres

Main source of truth for configuration, products/variants, matches, jobs, observations, current prices, alerts, strategy profiles, and events. Reached only through `pgbouncer`.

### redis

Used for Celery broker, distributed locks, distributed rate limits, in-flight scrape locks, and domain semaphores. Celery result backend is not required by default because job state lives in PostgreSQL.

Correctness-critical keys (in-flight locks, idempotent-dispatch guards, rate-limit state) must never be evicted, so they cannot share an eviction policy with the broker:

- Run two Redis instances (or at minimum two configs): broker traffic on one; locks/limits/dedup state on another with `maxmemory-policy noeviction`.
- Logical database numbers on a single instance do NOT isolate memory and are not an acceptable substitute.
- Define restart behavior explicitly: locks and dedup keys are TTL-bounded, so a Redis restart may open a bounded window of duplicate work — the idempotent dispatch guard, fencing tokens, and `unique(scrape_job_id, match_id)` must make that safe.
- Monitor memory usage and eviction counters (Section 31); evictions on the locks instance are an incident, not a metric.

### Container & network hardening (all services)

- Containers run as a non-root user.
- All base and infrastructure images are version-pinned; no `latest` tags.
- Services bind dual-stack (IPv6 as well as IPv4). Platform-internal networks (e.g. Railway private networking) are IPv6-only; an IPv4-only `0.0.0.0` bind is unreachable there. This applies to the API server, both Scrapyd nodes, and PgBouncer.

---

## 5. Monorepo Structure

Use one monorepo first. Deploy separate services from different root directories or start commands.

```text
price-monitor/
  README.md
  PROJECT_SPEC.md
  pyproject.toml
  docker-compose.yml

  apps/
    api/
      app/
        main.py
        api/
        core/
        db/
        services/

    scheduler/
      app/
        scheduler/
          scheduler_app.py
          scheduler_service.py

    workers/
      app/
        workers/
          celery_app.py
          tasks/

    scrapers/
      scrapy.cfg
      price_monitor/
        settings.py
        items.py
        pipelines.py
        middlewares.py
        spiders/
          generic_price_spider.py
        extractors/
        adapters/

    scrapers-browser/
      scrapy.cfg
      price_monitor_browser/
        settings.py
        items.py
        pipelines.py
        middlewares.py
        spiders/
          generic_browser_price_spider.py
        extractors/
        adapters/

  libs/
    shared/
      app_shared/
        config.py
        enums.py
        ids.py
        database.py
        models/
        schemas/
        security/
        pagination.py
        url_patterns.py
        money.py
        task_names.py
    scrape-core/
      scrape_core/
        extraction/
        items.py
        validation.py
        confidence.py
        pipelines.py
        rate_limiter.py

  alembic/
    versions/

  specs/
    00-constitution/
    01-monorepo-services-skeleton/
    02-database-foundation/
    03-auth-api-keys-workspace-isolation/
    04-catalog-products-variants-groups/
    05-competitors-matches/
    06-scrape-profiles-extraction-rules/
    07-scrapyd-http-spider-mvp/
    08-jobs-orchestration/
    09-current-prices-alerts/
    10-access-policies-proxies-attempts/
    11-distributed-rate-limits-locks/
    12-domain-strategy-optimizer/
    13-scheduler/
    14-browser-scraping-service/
    15-retention-rollups-partition-maintenance/
    16-webhook-events/

  tests/
    unit/
    integration/
```

`apps/scrapers` (HTTP) and `apps/scrapers-browser` (Playwright) are near-identical projects. To prevent drift, the extraction logic, item models, validation, confidence scoring, DB pipelines, and the rate limiter live in a dedicated `libs/scrape-core` workspace member imported by both. The two projects should differ only in their download handler / Playwright settings and spider entrypoint, not in extraction or persistence code.

Dependency boundaries are deliberate:

- `libs/shared` (`app_shared`) never imports Scrapy/Twisted/Playwright, so the API, scheduler, and worker images never pull scraping dependencies.
- `libs/scrape-core` may depend on `app_shared`, never the reverse.
- Celery task names are string constants in `app_shared/task_names.py`; spiders emit tasks via `send_task(name, ...)` and never import the worker application.
- The monorepo is a uv workspace: one root `pyproject.toml`, one lockfile, and each service image installs only its own member's dependency closure — including the migration job image, which installs from the same lockfile so migrations execute under the exact versions they were written against.

---

## 6. Service Communication

Only the API should be publicly exposed.

Internal service communication:

```text
worker-service → scrapyd-http-service (pool)
worker-service → scrapyd-browser-service (pool)
api-service → pgbouncer → postgres
worker-service → pgbouncer → postgres
scheduler-service → pgbouncer → postgres
scrapy services → pgbouncer → postgres
all services → redis where needed
```

All Postgres traffic goes through `pgbouncer`. No service connects to `postgres` directly. The single exception is the one-shot migration job (Section 4, engine/process hygiene): it connects directly to Postgres and is never run automatically by an application service at startup — migration ordering is an explicit deploy step (compose dependency locally, pre-deploy hook on the platform), never left to manual sequencing.

`SCRAPYD_HTTP_URLS` / `SCRAPYD_BROWSER_URLS` are **comma-separated lists** so the worker can load-balance and shard across multiple Scrapyd nodes per mode. v1 may contain a single URL, but the worker must treat them as a pool from day one (Scrapyd is single-node and does not scale horizontally on its own).

Example environment variables:

```text
DATABASE_URL=postgresql+psycopg://user:pass@pgbouncer.railway.internal:6432/price_monitor
DB_POOL_SIZE=5
DB_MAX_OVERFLOW=2
REDIS_URL=redis://...

SCRAPYD_HTTP_URLS=http://scrapyd-http-1.railway.internal:6800
SCRAPYD_BROWSER_URLS=http://scrapyd-browser-1.railway.internal:6800

API_PUBLIC_BASE_URL=https://...
INTERNAL_API_BASE_URL=http://api-service.railway.internal:8000
```

Scrapyd services should not be exposed publicly.

---

## 7. High-Level Architecture

```text
External Client / Future WooCommerce Plugin / Salla / n8n
        |
        v
api-service
FastAPI API
        |
        +-----------------------------+
        |                             |
        v                             v
PostgreSQL                       Redis
source of truth                  broker + locks + rate limits
        |                             |
        v                             v
scheduler-service              worker-service
DB-driven schedules             Celery orchestration
                                      |
                                      v
                         Scrapyd Schedule API
                         /schedule.json
                            |             |
                            v             v
                  scrapyd-http       scrapyd-browser
                  Scrapy HTTP        Scrapy + Playwright
                            |             |
                            v             v
                        Competitor public product pages
```

---

## 8. Scraping Runtime Design

Scrapy should not be started repeatedly inside Celery task processes.

Instead:

1. Celery creates/dispatches a scrape job.
2. Celery calls Scrapyd’s schedule API **idempotently** (see "Idempotent dispatch" below).
3. Scrapyd runs the Scrapy spider in its own process.
4. The spider reads configuration from PostgreSQL (through PgBouncer).
5. The spider writes observations/current prices/request attempts to PostgreSQL.
6. The spider emits a `price_analysis` Celery task per affected variant. The spider itself does **not** compute alerts or webhooks.

### Reactor safety (mandatory)

The spider runs on Twisted's reactor (and scrapy-playwright depends on it). Synchronous SQLAlchemy commits inside pipelines/middlewares block the reactor and destroy concurrency. Therefore:

- Use an async DB driver integrated with the reactor, **or** wrap every DB call in `deferToThread`.
- Keep DB pools per spider process small (`DB_POOL_SIZE` above) and go through PgBouncer.
- The distributed rate limiter (Section 12) must also be non-blocking — no `time.sleep`, no blocking Redis round-trips on the reactor thread.

### Idempotent dispatch

Celery delivers tasks at-least-once and a dispatch task may be retried, but Scrapyd has no native deduplication and will happily run the same batch twice. Each dispatch must:

- Compute a stable dispatch key, e.g. `scrape_job_id + batch_index`.
- Guard with a Redis key `dispatched:{scrape_job_id}:{batch_index}` (SET NX) **and/or** persist the returned Scrapyd `jobid` on the batch row before considering the dispatch done.
- Skip scheduling if the guard already exists.

### Persistence batching

Pipelines write observations, request attempts, and current-price updates in small batches (flush every N items or T seconds), never one commit per item. At 10,000–20,000 targets per job, per-item commits through PgBouncer serialize the whole job on the pooler.

### URL fetch safety (SSRF)

The spider fetches user-supplied URLs from inside the internal network, so every request must pass the URL safety rules (Section 11): scheme allowlist, and the private/loopback/link-local/internal deny rules enforced against the **resolved IP at connection time** (defeats DNS rebinding). Every redirect hop is re-validated.

### Robots handling

`robots_policy` is per-competitor/domain (Section 22), but Scrapy's built-in `ROBOTSTXT_OBEY` is process-global. Implement robots handling as a custom downloader middleware that resolves the policy per request from the cached domain configuration.

### Generic HTTP spider

Start with one generic DB-configurable spider:

```text
generic_price_spider
```

Input arguments:

```text
workspace_id
scrape_job_id
match_ids
mode
```

The spider should:

1. Load matches from DB.
2. Resolve scrape profile (use cached resolved config — see Section 9 — do not run the full resolution chain per match).
3. Resolve access policy (cached).
4. Resolve domain strategy profile (cached).
5. Use distributed domain rate limiter (non-blocking).
6. Request competitor product URL.
7. Extract price using configured strategy.
8. Save request attempts.
9. Save observations.
10. Update match current price.
11. Update job target state.
12. Update strategy stats (atomic SQL increments).
13. Emit a `price_analysis` Celery task per affected variant (deduplicated per variant per job).

The spider stops at persistence. Alert computation, variant state, alert events, and webhook events are the responsibility of the `price_analysis` task, not the spider.

### Browser spider

Use browser spider only when needed:

```text
generic_browser_price_spider
```

Used when:

- `scrape_profiles.mode = BROWSER`
- `access_policy.allow_browser_fallback = true`
- Domain strategy learned that browser is needed.
- Variant selection requires browser interaction.

Browser spider should use `scrapy-playwright`. Keep browser concurrency low.

---

## 9. Configuration Philosophy

The code is the engine. The database controls:

- Workspaces.
- Products and variants.
- Product groups.
- Competitors.
- Competitor URL matches.
- Scrape profiles.
- Access policies.
- Proxy providers.
- Domain access rules.
- Domain strategy profiles.
- Refresh rules.
- Alert thresholds.
- Confidence thresholds.
- Retention settings later.

### Scrape profile resolution

```text
Match-level override
↓
Domain strategy profile preferred extraction method
↓
Competitor-level default
↓
Workspace-level default
↓
Global default
```

### Access policy resolution

```text
Match-level access policy
↓
Domain strategy profile preferred access method
↓
Domain access rule
↓
Competitor-level default
↓
Workspace-level default
↓
Global default
```

### Resolution caching

These chains must not be walked per match with separate queries — at 10,000–20,000 matches per refresh that is N+1 amplification.

- Batch-resolve config per `(competitor_id, url_pattern)` for a whole batch, not per match.
- Cache the resolved scrape profile and access policy in Redis with a short TTL, keyed by `(workspace_id, competitor_id, url_pattern)`.
- Invalidate the cache on writes to the relevant profile/policy/domain-rule rows (or rely on the short TTL).

---

## 10. Products, Variants, and Matches

### Product

Parent catalog item.

Example:

```text
Product: iPhone 15
```

### ProductVariant

Actual sellable/priced item.

Example:

```text
Variant: iPhone 15 - 128GB - Black
Variant: iPhone 15 - 256GB - Black
```

### Simple products

Even simple products must have one default variant.

Example:

```text
Product: Cetaphil Cleanser
Variant: Default
```

### CompetitorProductMatch

One competitor URL linked to one product variant.

Example:

```text
Variant: The Ordinary Niacinamide 30ml
Competitor: iBrand
URL: https://competitor.com/product/the-ordinary-niacinamide
```

One variant can have unlimited competitor matches.

---

## 11. Access Methods and Proxies

Allowed access methods:

```text
DIRECT_HTTP
DIRECT_HTTP_RETRY
PROXY_HTTP
PLAYWRIGHT_PROXY
```

No external scraping APIs.

Default unknown-domain fallback chain:

```text
Attempt 1: Direct HTTP
Attempt 2: Direct HTTP retry with backoff
Attempt 3: HTTP through internal proxy pool
Attempt 4: Internal Playwright through internal proxy pool
Attempt 5: Failed → needs tuning/manual review
```

For learned domains, start from the learned preferred access method instead of attempt 1.

### URL safety validation (mandatory)

Competitor match URLs and webhook endpoint URLs are user input that internal services will connect to. Validate twice:

At save time (API):

```text
scheme must be http or https
host must be a public DNS name or public IP
reject: localhost, private ranges (10/8, 172.16/12, 192.168/16), loopback,
        link-local (169.254/16, fe80::/10), unique-local (fc00::/7),
        cloud metadata endpoints, and internal service hostnames
reject userinfo in the URL (user:pass@host)
```

At fetch time (spider / future webhook dispatcher):

```text
re-resolve DNS and validate the connected IP against the same deny rules
(defeats DNS rebinding and records that change after save)
re-validate every redirect hop
```

---

## 12. Distributed Domain Rate Limiting

This is mandatory before large jobs.

Celery per-worker limits are not enough because multiple workers multiply the effective rate.

Implement:

```text
DistributedRateLimiter
```

Backed by Redis.

Every Scrapy request must acquire permission before fetching.

Rate-limit keys:

```text
rate:{workspace_id}:{domain}:DIRECT_HTTP
rate:{workspace_id}:{domain}:PROXY_HTTP
rate:{workspace_id}:{domain}:PLAYWRIGHT_PROXY
```

Also enforce domain concurrency:

```text
semaphore:{workspace_id}:{domain}:{access_method}
```

Use TTL to prevent deadlocks.

The limiter must be **non-blocking on the reactor**. Implement token acquisition as an async/`deferToThread` Redis call (e.g. a Lua token-bucket script for atomicity); never `time.sleep` or block the reactor while waiting.

If no token is available:

- Delay/requeue request (Scrapy non-blocking reschedule).
- Add jitter.
- Do not hammer the domain.

Example:

```text
delay = wait_time + random(2, 20 seconds)
```

### Requeue cap and overflow

Unbounded in-spider requeueing keeps a Scrapyd process slot alive for a long time under heavy rate limiting, starving other work. Therefore:

- Cap the number of in-spider requeues and the total in-spider wait per request.
- When the cap is exceeded, do not park the request inside the spider. Mark the target as deferred and overflow it back to Celery `scrape_dispatch` for re-dispatch in a later batch.

---

## 13. In-Flight Deduplication

A scheduled job and manual job can target the same match.

Prevent duplicate work.

Use Redis lock:

```text
lock:scrape:{workspace_id}:{match_id}
```

TTL examples:

```text
10 minutes for HTTP
30 minutes for browser
```

If lock exists:

- Skip.
- Attach to existing job if implemented.
- Or requeue later.

Use `scrape_job_targets` with:

```text
unique(scrape_job_id, match_id)
```

This prevents duplicates inside the same high-level job.

### Lock lifecycle (single owner)

The lock crosses worker → Scrapyd → spider → `price_analysis` boundaries, so ownership must be explicit:

- The **spider acquires** `lock:scrape:{workspace_id}:{match_id}` immediately before fetching a match, and **the spider releases it** after persistence (step 12). The worker does **not** hold per-match locks across the dispatch boundary, because a batch may sit behind the rate limiter far longer than any safe worker-held TTL.
- TTL must exceed the worst-case in-spider wait (rate-limit backoff + requeue cap from Section 12). If the per-request wait cap is, say, 5 minutes, the HTTP lock TTL must be comfortably larger.
- Use a **fencing token** (lock value = unique token); release only if the stored token matches (Lua compare-and-delete), so an expired-then-reacquired lock is never deleted by the previous owner.
- The `price_analysis` task runs after release and is idempotent (keyed per variant), so it does not need the scrape lock.
- If the lock is already held: skip the target and mark it `SKIPPED` with `LOCKED_ALREADY_RUNNING`, or requeue later.

---

## 14. Domain Strategy Optimizer

### Purpose

The Domain Strategy Optimizer learns the best access method and extraction method per domain/template. It saves resources by avoiding repeated failed attempts.

Example:

If a competitor always fails direct HTTP and always works with proxy + CSS selector, future scrapes should start directly with:

```text
PROXY_HTTP + CSS_SELECTOR
```

not:

```text
DIRECT_HTTP → DIRECT_RETRY → PROXY_HTTP
JSON_LD → EMBEDDED_JSON → CSS_SELECTOR
```

### Learn by domain + URL pattern

Do not learn by domain only.

The same domain can have different templates:

```text
example.com/products/*
example.com/p/*
example.com/ar/products/*
example.com/offers/*
```

Learn by:

```text
workspace_id + competitor_id + domain + url_pattern
```

### Access methods

```text
DIRECT_HTTP
DIRECT_HTTP_RETRY
PROXY_HTTP
PLAYWRIGHT_PROXY
```

### Extraction methods

```text
PLATFORM_PATTERN
JSON_LD
EMBEDDED_JSON
CSS_SELECTOR
XPATH
REGEX
PLAYWRIGHT_RENDERED_SELECTOR
```

### Discovery mode

When a new competitor/domain pattern is added:

```text
Take 3–10 sample matched URLs
↓
Run discovery mode
↓
Test access methods
↓
Test extraction methods
↓
Find winning combination
↓
Save learned domain strategy profile
↓
Use learned strategy for future URLs
```

Discovery mode can try multiple methods because it runs on a small sample.

### Promotion rule

Promote a method after:

```text
3 successful extractions
across at least 3 different URLs
same domain + URL pattern
confidence >= configured threshold, default 0.85
valid numeric price
valid currency when required
```

### Rediscovery triggers

Re-run discovery if:

```text
3 consecutive failures for preferred method
success rate drops below 80%
selector returns empty repeatedly
price confidence drops below 0.75 repeatedly
site returns 403/429 repeatedly
currency disappears
price values become unrealistic
template appears changed
```

### Atomic stats

Strategy stats must never be updated with read-modify-write in Python. But per-attempt SQL increments would make one `strategy_attempt_stats` row a hot row during a large single-domain batch (Section 2, principle 26). Therefore:

- Buffer attempt counters in Redis (atomic `INCR`/Lua) keyed by `(domain_strategy_profile_id, method_type, method_name)`.
- Flush to Postgres periodically and at job finalization with a single atomic `UPDATE ... SET count = count + delta` per key.
- Promotion/rediscovery decisions read the flushed DB values plus pending Redis deltas.

---

## 15. URL Pattern Derivation

The optimizer depends on stable URL pattern grouping.

Implement:

```text
derive_url_pattern(url)
```

Normalization steps:

1. Parse URL.
2. Lowercase hostname.
3. Remove scheme.
4. Remove `www.`.
5. Remove trailing slash.
6. Remove fragment.
7. Remove query string for pattern derivation.
8. Preserve locale prefixes like `/ar/`, `/en/`.
9. Split path into segments.
10. Replace ID-like segments with `:id`.
11. Replace product slug segments after known product path keys with `*`.

ID-like segments:

```text
all digits
UUID-like
long mixed alphanumeric ID
contains mostly digits
```

Product path rules:

```text
/products/<slug>        → /products/*
/product/<slug>         → /product/*
/p/<id-or-slug>         → /p/*
/item/<id-or-slug>      → /item/*
/ar/products/<slug>     → /ar/products/*
```

Allow manual override in:

```text
domain_access_rules.url_pattern_override
domain_strategy_profiles.url_pattern
```

### Pattern algorithm versioning

Stored `url_pattern` values are join keys between matches and learned strategies, produced by heuristics that will be refined over time. To keep them consistent:

- Maintain a `URL_PATTERN_ALGORITHM_VERSION` constant, and store `url_pattern_version` on every row that stores a derived pattern (`competitor_product_matches`, `domain_strategy_profiles`).
- When the algorithm changes, bump the version and run a backfill maintenance task that re-derives patterns and re-links (or re-queues discovery for) affected strategy profiles.
- Never mix patterns from different algorithm versions in strategy lookups.

---

## 16. Price Extraction Strategy

Extraction order for unknown domains:

```text
1. Platform/product pattern
2. JSON-LD / structured metadata
3. Embedded JavaScript product data
4. CSS selector from DB
5. XPath selector from DB
6. Regex rule from DB
7. Internal Playwright-rendered selector
8. Failed: PRICE_NOT_FOUND
```

For learned domains, start from preferred extraction method and fallback only if needed.

### Platform/product pattern

Use public product data available on the competitor site itself.

Examples:

```text
Shopify:
/products/<handle>.js
/products/<handle>.json

WooCommerce:
public product JSON in HTML
public store product data if available

Magento:
embedded JSON config in product page
```

This is not an external scraping API.

### JSON-LD / structured data

Extract product structured data when present.

Look for:

```text
Product
Offer
offers.price
offers.priceCurrency
availability
```

### Embedded JSON

Look inside scripts for product/variant data.

Example:

```html
<script>
  window.__PRODUCT__ = {
    price: 5500,
    currency: "SAR",
    variants: [...]
  }
</script>
```

### CSS selector

DB-configured selectors:

```text
price_selector = ".product-price .amount"
old_price_selector = ".old-price .amount"
stock_selector = ".availability"
title_selector = "h1"
```

### XPath selector

DB-configured XPath rules:

```text
price_xpath = "//span[contains(@class, 'price')]/text()"
```

### Regex

DB-configured regex rules:

```text
"price"\s*:\s*"?(?P<price>[0-9.,]+)"?
```

### Playwright-rendered selector

Used only when:

- price is rendered by JavaScript.
- variant must be selected interactively.
- HTTP extraction repeatedly fails.
- domain strategy says browser is required.

---

## 17. Extraction Confidence

Wrong prices are worse than missing prices.

Every extraction returns confidence.

Default confidence examples:

```text
Platform variant JSON matched by SKU/options: 0.95
JSON-LD price + currency + title match: 0.95
Embedded JSON variant match: 0.90
CSS selector exact price: 0.85
XPath selector exact price: 0.85
Regex from script: 0.75
Playwright visible price after selected variant: 0.80
Only one number found on page: 0.40, reject by default
```

These should be tunable DB configuration, not hardcoded constants.

Default minimum accepted confidence:

```text
0.75
```

Default strategy promotion threshold:

```text
0.85
```

---

## 18. Price Validation

Before saving a price as valid:

```text
price must be Decimal/NUMERIC
price must be > 0
currency should match expected currency if configured
price should not be old price unless explicitly selected
price should not be installment price
price should not be discount percentage
price should not be "save X"
price should not be shipping price
price should pass min/max validation
confidence must be above threshold
```

Validation rules live in `scrape_profiles.validation_rules`.

Example:

```json
{
  "required_currency": "SAR",
  "min_price": 1,
  "max_price": 100000,
  "reject_if_text_contains": ["save", "discount", "off", "خصم", "وفر"],
  "prefer_text_contains": ["price", "sale", "السعر"]
}
```

---

## 19. Money and Currency

Use Decimal in Python and NUMERIC in PostgreSQL.

Never use float for prices.

Recommended DB type:

```text
NUMERIC(18, 4)
```

Monetary values must be finite: reject `NaN` and `Infinity` at the type boundary — PostgreSQL `NUMERIC` will happily store `NaN`, so the application must refuse it. Values with more decimal places than the column scale are rejected, not silently rounded: a wrong price is worse than a missing one.

Do not compare across currencies in v1.

If competitor currency differs from client variant currency:

```text
save observation
mark comparable = false
exclude from price analysis
store CURRENCY_MISMATCH warning/error
```

FX conversion can be added later, but is out of scope for v1.

---

## 20. Variant Extraction Strategies

Supported variant strategies:

```text
PAGE_SINGLE_PRICE
URL_HAS_VARIANT_SELECTED
HTML_VARIANT_TABLE
EMBEDDED_JSON_VARIANTS
SELECT_VARIANT_WITH_PLAYWRIGHT
CUSTOM_VARIANT_ADAPTER
```

Use `SELECT_VARIANT_WITH_PLAYWRIGHT` only when price changes after selecting size/color in browser.

---

## 21. ID Strategy

Use application-generated UUIDv7 for primary IDs.

Reason:

- Globally unique.
- Time-ordered.
- Better for insert-heavy indexed tables than random UUIDv4.
- Can be generated in the app.

All IDs are public API IDs.

Accepted trade-off: UUIDv7 embeds its creation timestamp, so public IDs disclose when a row was created. For this product that is acceptable, and it is a deliberate, documented decision.

---

## 22. Database Models

Use SQLAlchemy models and Alembic migrations.

Schema conventions (every table):

- All workspace-owned tables include `workspace_id` and get an RLS policy (Section 32) in the same migration that creates them.
- Money columns use `NUMERIC(18,4)`; values must be finite (Section 19).
- Every timestamp column is `TIMESTAMPTZ` (UTC). Naive timestamp columns are forbidden — enforce at the base-model level so it cannot be forgotten per table.
- Constraint/index names come from a deterministic naming convention that includes **all** constrained columns, not just the first — this schema routinely puts two multi-column uniques on one table starting with the same column (e.g. `unique(workspace_id, external_id)` and `unique(workspace_id, sku)`), which a first-column-only convention would name identically.
- Enum-like values are string-backed columns validated in the application.
- Migrations run only as the dedicated one-shot job connected directly to Postgres (Sections 4, 6); application services never migrate at startup. A single linear migration history is enforced in CI (fail on multiple heads).

Partitioned-table rules:

- `price_observations`, `request_attempts`, `webhook_events`, and `price_alert_events` are **created as monthly-partitioned tables in the migration that first creates them** — never created plain and converted later (that is a table rewrite under data).
- Their primary keys include the partition key (e.g. `(id, scraped_at)`): every unique constraint on a partitioned table must contain the partition key.
- Other tables reference partitioned rows by **soft reference** (plain UUID column, no FK). Retention drops partitions, so soft references may dangle; readers must tolerate a missing observation row — the current-state tables carry the data they need denormalized.

Deletion semantics:

- Any entity that history rows point at (products, variants, competitors, matches, product groups) is **archived by status, never hard-deleted**, once dependent observations/attempts/events exist. Hard delete is allowed only while no dependent history exists.

### Core identity and auth

```text
workspaces
- id uuidv7 pk
- name
- slug unique
- status
- default_scrape_profile_id nullable
- default_access_policy_id nullable
- created_at
- updated_at

users
- id uuidv7 pk
- workspace_id nullable
- email unique
- password_hash
- role
- status
- created_at
- updated_at

refresh_tokens
- id uuidv7 pk
- user_id
- token_hash
- expires_at
- revoked_at nullable
- created_at

api_keys
- id uuidv7 pk
- workspace_id
- name
- key_prefix (first characters of the key, for lookup and display)
- key_hash (SHA-256 of the full high-entropy secret)
- scopes json/list
- status
- last_used_at nullable
- created_at
- revoked_at nullable
```

API keys are random high-entropy secrets, so a fast hash (SHA-256) is correct — a password KDF (bcrypt/argon2) would add tens of milliseconds to every machine-client request for no security gain. `last_used_at` is throttled: at most one write per key per minute (buffer in Redis), never one write per request (Section 2, principle 26).

Roles:

```text
SUPER_ADMIN
WORKSPACE_ADMIN
READ_ONLY
```

API key scopes:

```text
products:read
products:write
variants:read
variants:write
competitors:read
competitors:write
matches:read
matches:write
jobs:run
jobs:read
results:read
alerts:read
webhooks:read
webhooks:write
```

### Catalog

```text
products
- id uuidv7 pk
- workspace_id
- external_id nullable
- sku nullable
- title
- brand nullable
- barcode nullable
- url nullable
- status
- created_at
- updated_at

product_variants
- id uuidv7 pk
- workspace_id
- product_id
- external_id nullable
- sku nullable
- barcode nullable
- title
- option_values json nullable
- current_price numeric(18,4)
- currency char(3)
- url nullable
- status
- created_at
- updated_at

product_groups
- id uuidv7 pk
- workspace_id
- name
- description nullable
- status
- created_at
- updated_at

product_group_items
- id uuidv7 pk
- workspace_id
- product_group_id
- product_id nullable
- product_variant_id nullable
- created_at
```

Unique constraints:

```text
products: unique(workspace_id, external_id) where external_id is not null
products: unique(workspace_id, sku) where sku is not null

product_variants: unique(workspace_id, external_id) where external_id is not null
product_variants: unique(workspace_id, sku) where sku is not null
product_variants: unique(workspace_id, product_id, title)

product_groups: unique(workspace_id, name)

product_group_items: unique(workspace_id, product_group_id, product_id)
product_group_items: unique(workspace_id, product_group_id, product_variant_id)
```

### Competitors and matches

```text
competitors
- id uuidv7 pk
- workspace_id
- name
- domain
- status
- legal_status
- robots_policy
- default_scrape_profile_id nullable
- default_access_policy_id nullable
- max_concurrent_requests nullable
- max_requests_per_minute nullable
- created_at
- updated_at

competitor_product_matches
- id uuidv7 pk
- workspace_id
- product_id
- product_variant_id
- competitor_id
- competitor_url
- normalized_competitor_url
- url_pattern
- url_pattern_version
- competitor_variant_identifier nullable
- competitor_variant_sku nullable
- competitor_variant_options json nullable
- external_title nullable
- scrape_profile_id nullable
- access_policy_id nullable
- priority
- status
- health_status
- last_error_code nullable
- consecutive_failures
- success_rate_7d nullable
- current_price_id nullable
- last_scraped_at nullable
- last_success_at nullable
- last_failed_at nullable
- created_at
- updated_at
```

Unique constraints:

```text
competitors: unique(workspace_id, domain)
competitor_product_matches: unique(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)
```

Legal statuses:

```text
REVIEW_REQUIRED
APPROVED
DISABLED
```

Robots policy:

```text
RESPECT
REVIEW_REQUIRED
IGNORE_AFTER_APPROVAL
```

Priority:

```text
LOW
NORMAL
HIGH
CRITICAL
```

Match status:

```text
ACTIVE
PAUSED
FAILED
ARCHIVED
```

Health status:

```text
HEALTHY
DEGRADED
FAILING
UNKNOWN
```

### Scrape and access config

```text
scrape_profiles
- id uuidv7 pk
- workspace_id nullable
- name
- mode
- adapter_key
- jsonld_enabled
- platform_patterns_enabled
- embedded_json_enabled
- price_selector nullable
- price_xpath nullable
- price_regex nullable
- old_price_selector nullable
- old_price_xpath nullable
- old_price_regex nullable
- currency_selector nullable
- currency_xpath nullable
- currency_regex nullable
- stock_selector nullable
- stock_xpath nullable
- stock_regex nullable
- title_selector nullable
- title_xpath nullable
- variant_strategy
- variant_selector_config json nullable
- price_transform_rules json nullable
- validation_rules json nullable
- confidence_rules json nullable
- wait_for_selector nullable
- request_timeout_ms
- browser_timeout_ms nullable
- headers json nullable
- cookies json nullable
- created_at
- updated_at

proxy_providers
- id uuidv7 pk
- workspace_id nullable
- name
- type
- base_url
- username nullable
- password_encrypted nullable
- country_code nullable
- status
- monthly_budget_limit nullable
- created_at
- updated_at

access_policies
- id uuidv7 pk
- workspace_id nullable
- name
- strategy
- provider_id nullable
- country_code nullable
- use_proxy_on_first_attempt
- use_proxy_on_retry
- allow_browser_fallback
- max_retries
- rotate_per_request
- sticky_session
- session_ttl_minutes nullable
- max_requests_per_minute nullable
- max_requests_per_hour nullable
- max_requests_per_day nullable
- timeout_ms
- created_at
- updated_at

domain_access_rules
- id uuidv7 pk
- workspace_id
- competitor_id
- domain
- url_pattern nullable
- url_pattern_override nullable
- access_policy_id
- max_concurrent_requests
- max_requests_per_minute
- cooldown_seconds
- block_detection_rules json nullable
- enabled
- created_at
- updated_at
```

Config guardrails:

- `scrape_profiles.cookies` may carry only non-identifying technical cookies (e.g. currency/locale selection). Session or authentication cookies are forbidden (Section 30) and rejected by validation.
- `proxy_providers.monthly_budget_limit` is enforced at dispatch/fetch time from cheap Redis usage counters (incremented per proxied request, reset monthly) — never by counting `request_attempts` rows on the hot path. On budget exhaustion, fall back per the access-policy strategy or fail with `LIMIT_REACHED`.

Scrape profile modes:

```text
HTTP
BROWSER
CUSTOM
```

Adapter keys:

```text
default_http
jsonld_first
selector_only
regex_only
shopify_product_json
woocommerce_store_api
playwright_rendered
custom_adapter
```

Proxy provider types:

```text
DATACENTER
RESIDENTIAL
MOBILE
```

Access policy strategies:

```text
DIRECT_ONLY
DIRECT_THEN_PROXY
PROXY_FIRST
RESIDENTIAL_ONLY
BROWSER_FALLBACK
```

### Domain strategy optimizer

```text
domain_strategy_profiles
- id uuidv7 pk
- workspace_id
- competitor_id
- domain
- url_pattern
- url_pattern_version
- status
- preferred_access_method nullable
- preferred_extraction_method nullable
- access_confidence nullable
- extraction_confidence nullable
- confirmed_success_count
- recent_failure_count
- last_discovery_at nullable
- last_success_at nullable
- last_failed_at nullable
- created_at
- updated_at

strategy_attempt_stats
- id uuidv7 pk
- domain_strategy_profile_id
- method_type
- method_name
- attempt_count
- success_count
- failure_count
- success_rate
- avg_response_time_ms nullable
- avg_confidence nullable
- last_success_at nullable
- last_failed_at nullable

strategy_discovery_runs
- id uuidv7 pk
- workspace_id
- competitor_id
- domain
- url_pattern
- sample_size
- status
- winning_access_method nullable
- winning_extraction_method nullable
- created_at
- completed_at nullable
```

Unique constraints:

```text
domain_strategy_profiles: unique(workspace_id, competitor_id, domain, url_pattern)
strategy_attempt_stats: unique(domain_strategy_profile_id, method_type, method_name)
```

Strategy status:

```text
DISCOVERY_REQUIRED
LEARNING
ACTIVE
DEGRADED
DISABLED
```

Method type:

```text
ACCESS
EXTRACTION
```

### Jobs

```text
refresh_rules
- id uuidv7 pk
- workspace_id
- name
- scope
- product_id nullable
- product_variant_id nullable
- product_group_id nullable
- competitor_id nullable
- match_id nullable
- cron_expression nullable
- interval_minutes nullable
- priority
- enabled
- next_run_at nullable
- last_run_at nullable
- locked_at nullable
- created_at
- updated_at

scrape_jobs
- id uuidv7 pk
- workspace_id
- type
- scope
- product_id nullable
- product_variant_id nullable
- product_group_id nullable
- competitor_id nullable
- match_id nullable
- status
- priority
- total_targets
- success_count
- failure_count
- skipped_count
- requested_by nullable
- source
- started_at nullable
- completed_at nullable
- created_at

scrape_job_targets
- id uuidv7 pk
- workspace_id
- scrape_job_id
- match_id
- status
- locked_at nullable
- started_at nullable
- completed_at nullable
- error_code nullable
- created_at
```

Refresh scopes:

```text
WORKSPACE
COMPETITOR
PRODUCT
VARIANT
PRODUCT_GROUP
MATCH
```

Job types:

```text
MANUAL
SCHEDULED
API_TRIGGERED
RETRY_FAILED
DISCOVERY
```

Job statuses:

```text
PENDING
RUNNING
COMPLETED
PARTIAL_FAILED
FAILED
CANCELLED
```

Sources:

```text
API
SCHEDULER
INTERNAL
PLUGIN
```

Unique constraint:

```text
scrape_job_targets: unique(scrape_job_id, match_id)
```

### Observations and current prices

```text
request_attempts
- id uuidv7 pk
- workspace_id
- scrape_job_id
- match_id
- attempt_number
- url
- access_method
- proxy_provider_id nullable
- proxy_country nullable
- status_code nullable
- response_time_ms nullable
- success
- error_code nullable
- error_message nullable
- created_at

price_observations
- id uuidv7 pk
- workspace_id
- match_id
- product_id
- product_variant_id
- scrape_job_id nullable
- price numeric(18,4) nullable
- old_price numeric(18,4) nullable
- currency char(3) nullable
- stock_status nullable
- raw_title nullable
- success
- comparable
- error_code nullable
- error_message nullable
- extraction_method nullable
- extraction_confidence numeric(5,4) nullable
- selector_used nullable
- scraped_at

match_current_prices
- id uuidv7 pk
- workspace_id
- match_id
- product_id
- product_variant_id
- competitor_id
- price numeric(18,4) nullable
- old_price numeric(18,4) nullable
- currency char(3) nullable
- stock_status nullable
- comparable
- observation_id
- success
- error_code nullable
- extraction_method nullable
- extraction_confidence numeric(5,4) nullable
- scraped_at
- updated_at

variant_price_states
- id uuidv7 pk
- workspace_id
- product_id
- product_variant_id
- client_price numeric(18,4)
- currency char(3)
- cheapest_competitor_price numeric(18,4) nullable
- average_competitor_price numeric(18,4) nullable
- highest_competitor_price numeric(18,4) nullable
- comparable_competitor_count
- latest_alert_type
- latest_alert_severity
- latest_alert_state_id nullable
- calculated_at
- updated_at
```

Partition monthly (created partitioned from birth; PK includes the partition key — Section 22 partitioned-table rules):

```text
request_attempts by created_at
price_observations by scraped_at
```

`match_current_prices.observation_id` and `competitor_product_matches.current_price_id` are soft references (no FK); after retention drops old partitions they may dangle, and readers must tolerate that — the current-state row itself carries every field analysis needs.

Unique constraints:

```text
match_current_prices: unique(workspace_id, match_id)
variant_price_states: unique(workspace_id, product_variant_id)
```

Access methods:

```text
DIRECT_HTTP
DIRECT_HTTP_RETRY
PROXY_HTTP
PLAYWRIGHT_PROXY
```

Stock statuses:

```text
IN_STOCK
OUT_OF_STOCK
UNKNOWN
```

### Alerts and events

```text
variant_alert_states
- id uuidv7 pk
- workspace_id
- product_id
- product_variant_id
- type
- severity
- status
- client_price numeric(18,4)
- benchmark_price numeric(18,4) nullable
- cheapest_competitor_price numeric(18,4) nullable
- average_competitor_price numeric(18,4) nullable
- message
- details json nullable
- first_seen_at
- last_seen_at
- resolved_at nullable
- updated_at

price_alert_events
- id uuidv7 pk
- workspace_id
- product_id
- product_variant_id
- alert_state_id
- event_type
- previous_type nullable
- new_type
- previous_severity nullable
- new_severity
- message
- details json nullable
- created_at

variant_price_daily_rollups
- id uuidv7 pk
- workspace_id
- product_id
- product_variant_id
- date
- currency
- client_price numeric(18,4)
- min_competitor_price numeric(18,4) nullable
- avg_competitor_price numeric(18,4) nullable
- max_competitor_price numeric(18,4) nullable
- alert_type
- comparable_competitor_count
- created_at

webhook_endpoints
- id uuidv7 pk
- workspace_id
- name
- url
- secret_encrypted nullable
- enabled
- event_types json/list
- created_at
- updated_at

webhook_events
- id uuidv7 pk
- workspace_id
- event_type
- payload json
- status
- created_at
- delivered_at nullable
```

Partition monthly (created partitioned from birth; PK includes the partition key — Section 22 partitioned-table rules):

```text
price_alert_events by created_at
webhook_events by created_at
```

`webhook_endpoints.url` must pass URL safety validation (Section 11) at save time and again at delivery time.

Unique constraints:

```text
variant_alert_states: unique(workspace_id, product_variant_id)
variant_price_daily_rollups: unique(workspace_id, product_variant_id, date)
```

Alert event types:

```text
CREATED
UPDATED
RESOLVED
REOPENED
UNCHANGED
```

Webhook event types:

```text
price.alert.created
price.alert.updated
scrape.job.completed
scrape.job.failed
match.scrape.failed
product.comparison.updated
domain.strategy.updated
```

---

## 23. Alert Logic

Alert logic is variant-level and must be one ordered decision tree.

Inputs:

```text
client_price = product_variants.current_price
client_currency = product_variants.currency
competitor_prices = latest comparable prices from match_current_prices
cheapest_competitor_price = min(competitor_prices)
highest_competitor_price = max(competitor_prices)
average_competitor_price = average(competitor_prices)
```

Only include competitors where:

```text
success = true
comparable = true
currency = client_currency
price is not null
```

If competitor currency differs from client currency:

```text
exclude from comparison
mark current price comparable = false
store CURRENCY_MISMATCH warning/error
```

Decision tree:

```text
1. If no comparable competitor prices:
   NO_COMPETITOR_DATA

2. Else if client_price > highest_competitor_price:
   RISK

3. Else if client_price > cheapest_competitor_price:
   HIGH_PRICE

4. Else calculate discount_vs_average:
   discount_vs_average = ((average_competitor_price - client_price) / average_competitor_price) * 100

5. If discount_vs_average > 5:
   CHANCE_TO_INCREASE_PRICE

6. If discount_vs_average >= 1 and discount_vs_average <= 5:
   NORMAL

7. If discount_vs_average >= 0 and discount_vs_average < 1:
   CLOSE_TO_COMPETITORS

8. Else:
   HIGH_PRICE
```

Boundary values:

```text
Exactly 0% lower = CLOSE_TO_COMPETITORS
More than 0% and less than 1% lower = CLOSE_TO_COMPETITORS
Exactly 1% lower = NORMAL
Exactly 5% lower = NORMAL
More than 5% lower = CHANCE_TO_INCREASE_PRICE
```

Determinism rules:

- `discount_vs_average` is computed in `Decimal` with an explicit quantization (4 decimal places, `ROUND_HALF_UP`) before any boundary comparison — never in binary float, or the "exactly 1%" and "exactly 5%" boundaries stop being testable.
- Step 8 is a defensive branch: once steps 2–3 have passed, `discount_vs_average` is mathematically ≥ 0, so step 8 should be unreachable. It exists so unexpected data degrades to `HIGH_PRICE` instead of crashing; boundary tests cover steps 1–7.

Alert types:

```text
NO_COMPETITOR_DATA
RISK
HIGH_PRICE
CHANCE_TO_INCREASE_PRICE
NORMAL
CLOSE_TO_COMPETITORS
```

Severity:

```text
NO_COMPETITOR_DATA = LOW
RISK = CRITICAL
HIGH_PRICE = HIGH
CHANCE_TO_INCREASE_PRICE = MEDIUM
NORMAL = NONE
CLOSE_TO_COMPETITORS = MEDIUM
```

Recompute triggers — all of these run the same idempotent per-variant `price_analysis` task:

```text
a scrape completed for a match of the variant (deduplicated per variant per job)
the variant's client price or currency changed (PATCH or bulk upsert)
a match of the variant was archived/paused (comparable-price set changed)
```

A client price change must never wait for the next scrape to be reflected in alert state.

---

## 24. API Design

Base path:

```text
/v1
```

All high-volume list endpoints use cursor-based pagination.

Pagination response:

```json
{
  "items": [],
  "next_cursor": null
}
```

Default limit:

```text
50
```

Max limit:

```text
500
```

Mutating-endpoint rules:

- `DELETE` on entities with dependent history (products, variants, competitors, matches, product groups) archives by status (Section 22 deletion semantics); it hard-deletes only when no history exists. The response makes clear which happened.
- Bulk upserts are set-based (batched `INSERT ... ON CONFLICT`), never row-by-row loops. Identity resolution order: `external_id`, then `sku`, then (for variants) `(product_id, title)`.
- `POST /v1/auth/login` is rate-limited per account and per source address (Redis counters, progressive backoff); failures return a uniform error that does not reveal which credential factor was wrong.

### Endpoints

```text
GET /health

POST /v1/auth/login
POST /v1/auth/refresh
POST /v1/auth/logout

POST /v1/api-keys
GET /v1/api-keys
DELETE /v1/api-keys/{id}

POST /v1/products
GET /v1/products
GET /v1/products/{id}
PATCH /v1/products/{id}
DELETE /v1/products/{id}
POST /v1/products/bulk-upsert

GET /v1/variants
GET /v1/variants/{id}
PATCH /v1/variants/{id}
POST /v1/variants/bulk-upsert

POST /v1/product-groups
GET /v1/product-groups
PATCH /v1/product-groups/{id}
DELETE /v1/product-groups/{id}
POST /v1/product-groups/{id}/items
DELETE /v1/product-groups/{id}/items/{item_id}

POST /v1/competitors
GET /v1/competitors
GET /v1/competitors/{id}
PATCH /v1/competitors/{id}
DELETE /v1/competitors/{id}

POST /v1/matches
GET /v1/matches
GET /v1/matches/{id}
PATCH /v1/matches/{id}
DELETE /v1/matches/{id}
POST /v1/matches/bulk-upsert

POST /v1/scrape-profiles
GET /v1/scrape-profiles
GET /v1/scrape-profiles/{id}
PATCH /v1/scrape-profiles/{id}
DELETE /v1/scrape-profiles/{id}

POST /v1/proxy-providers
GET /v1/proxy-providers
PATCH /v1/proxy-providers/{id}
DELETE /v1/proxy-providers/{id}

POST /v1/access-policies
GET /v1/access-policies
PATCH /v1/access-policies/{id}
DELETE /v1/access-policies/{id}

POST /v1/domain-access-rules
GET /v1/domain-access-rules
PATCH /v1/domain-access-rules/{id}
DELETE /v1/domain-access-rules/{id}

POST /v1/domain-strategies/discover/competitor/{competitor_id}
POST /v1/domain-strategies/discover/domain
GET /v1/domain-strategies
GET /v1/domain-strategies/{id}
PATCH /v1/domain-strategies/{id}
POST /v1/domain-strategies/{id}/rediscover
GET /v1/domain-strategies/{id}/stats

POST /v1/refresh-rules
GET /v1/refresh-rules
GET /v1/refresh-rules/{id}
PATCH /v1/refresh-rules/{id}
DELETE /v1/refresh-rules/{id}

POST /v1/jobs/run/workspace
POST /v1/jobs/run/competitor/{competitor_id}
POST /v1/jobs/run/product/{product_id}
POST /v1/jobs/run/variant/{variant_id}
POST /v1/jobs/run/product-group/{product_group_id}
POST /v1/jobs/run/product/{product_id}/competitor/{competitor_id}
POST /v1/jobs/run/variant/{variant_id}/competitor/{competitor_id}
POST /v1/jobs/run/match/{match_id}

GET /v1/jobs
GET /v1/jobs/{id}
GET /v1/jobs/{id}/targets
GET /v1/jobs/{id}/results
GET /v1/jobs/{id}/alerts
GET /v1/jobs/{id}/attempts

GET /v1/observations
GET /v1/matches/{match_id}/current-price
GET /v1/products/{product_id}/price-comparison
GET /v1/variants/{variant_id}/price-comparison

GET /v1/alerts/current
GET /v1/alerts/current/{variant_id}
GET /v1/alert-events
PATCH /v1/alerts/current/{variant_id}

POST /v1/webhook-endpoints
GET /v1/webhook-endpoints
PATCH /v1/webhook-endpoints/{id}
DELETE /v1/webhook-endpoints/{id}

GET /v1/webhook-events
GET /v1/webhook-events/{id}
```

### Example product bulk upsert

```json
{
  "products": [
    {
      "external_id": "100",
      "sku": "IPH15",
      "title": "iPhone 15",
      "brand": "Apple",
      "url": "https://clientstore.com/product/iphone-15",
      "variants": [
        {
          "external_id": "101",
          "sku": "IPH15-128-BLK",
          "barcode": "123456789",
          "title": "iPhone 15 - 128GB - Black",
          "option_values": {
            "Storage": "128GB",
            "Color": "Black"
          },
          "current_price": 2999,
          "currency": "SAR",
          "url": "https://clientstore.com/product/iphone-15?variant=101"
        }
      ]
    }
  ]
}
```

### Example match bulk upsert

```json
{
  "matches": [
    {
      "variant_external_id": "101",
      "competitor_id": "comp_001",
      "competitor_url": "https://competitor.com/iphone-15-128gb-black",
      "competitor_variant_sku": "IPH15-128-BLK",
      "competitor_variant_options": {
        "Storage": "128GB",
        "Color": "Black"
      }
    }
  ]
}
```

---

## 25. Job Flow

### Run one match

```text
API request
↓
Validate workspace access
↓
Create ScrapeJob
↓
Create ScrapeJobTarget
↓
Enqueue Celery dispatch task
↓
Worker schedules Scrapyd spider idempotently with workspace_id, scrape_job_id, match_ids
↓
--- spider (Scrapyd process, reactor-safe) ---
Spider loads match + variant + competitor
↓
Resolve scrape profile / domain strategy / access policy (cached)
↓
Acquire in-flight match lock (fencing token)
↓
Acquire distributed domain rate-limit token (non-blocking)
↓
Fetch URL using preferred access method
↓
Extract price using preferred extraction method
↓
Save RequestAttempt
↓
Save PriceObservation
↓
Update MatchCurrentPrice
↓
Update strategy stats atomically
↓
Mark target completed/failed
↓
Release in-flight match lock
↓
Emit price_analysis Celery task for the affected variant (dedup per variant per job)
↓
--- price_analysis task (Celery worker) ---
Update VariantPriceState
↓
Update VariantAlertState
↓
Create PriceAlertEvent if state changed
↓
Create WebhookEvent
```

### Run one variant

```text
API request with variant_id
↓
Find active matches for variant
↓
Create ScrapeJob
↓
Create unique ScrapeJobTargets
↓
Queue dispatch task
↓
Scrapy processes matches
↓
Analyze variant after updates
```

### Run one product

```text
API request with product_id
↓
Find active variants
↓
Find active matches for those variants
↓
Create ScrapeJob
↓
Queue dispatch tasks
↓
Analyze affected variants
```

### Run one competitor

```text
API request with competitor_id
↓
Find active matches for competitor
↓
Create ScrapeJob
↓
Queue dispatch tasks
```

### Run workspace

```text
API request or scheduled rule
↓
Find all active matches in workspace
↓
Create ScrapeJob
↓
Queue match batches
↓
Rate limiter spreads actual requests by domain
```

### Client price update (no scraping involved)

```text
API request updates a variant's current_price/currency (PATCH or bulk upsert)
↓
Enqueue price_analysis task for the variant (idempotent, deduplicated)
↓
Variant price/alert state reflects the new client price without waiting for a scrape
```

---

## 26. Celery Queues

Use queues:

```text
scrape_dispatch
price_analysis
strategy_discovery
webhook_events
maintenance
```

Scrapy HTTP and browser work happens inside Scrapyd services, not Celery.

### scrape_dispatch

Expands large jobs into Scrapyd runs.

Example:

```text
workspace job → batches by domain/mode
competitor job → batches by domain
variant job → small batch
match job → single match batch
```

Node handling within each Scrapyd pool:

- Node selection is deterministic (e.g. hash by domain, or round-robin with the chosen node persisted on the batch) so two dispatch retries can never send one batch to two nodes.
- Scrapyd's pending-job queue is per-node and not durable across node loss. The dispatch/finalization logic must detect a batch that was dispatched but whose targets never progressed (node died with the batch queued) and re-dispatch it after a timeout — protected by the same idempotency guards and in-flight match locks.

### price_analysis

Runs variant-level comparison. Triggered by the spider after persistence (not run inside the spider), and also whenever a variant's client price or currency changes via the API (Section 23 recompute triggers).

- One task per affected variant, deduplicated per variant per job, so many competitor matches completing for the same variant collapse into a single recompute instead of contending on the same `variant_price_states` / `variant_alert_states` row.
- The task is idempotent: it reads the variant's current comparable `match_current_prices`, recomputes state, and upserts. Re-running it produces the same result.

Parent-job progress (`scrape_jobs.success_count` / `failure_count` / `skipped_count`) must **not** be incremented once per target — that serializes thousands of writes on one row. Aggregate from `scrape_job_targets` periodically and on job finalization instead.

### strategy_discovery

Runs domain discovery orchestration.

### webhook_events

Creates/fetches events in v1. Automatic delivery later.

### maintenance

Partition creation, retention, rollups, cleanup, recovery.

---

## 27. Batching Strategy

Do not create one Scrapyd job per URL at scale.

Group matches by:

```text
workspace
competitor/domain
scrape mode HTTP/BROWSER
access policy if needed
url_pattern if useful
```

Batch size examples:

```text
HTTP batch: 50–200 matches
Browser batch: 5–25 matches
```

This keeps Scrapyd jobs manageable and avoids thousands of tiny jobs.

---

## 28. Scheduler

Use a custom DB-driven scheduler enqueuer.

Duties:

```text
claim due refresh rules
create scrape job
enqueue Celery dispatch task
calculate next_run_at
```

Duplicate prevention:

The scheduler relies on **row-level claiming with `FOR UPDATE SKIP LOCKED`** (below). This is the single chosen model and lets multiple scheduler instances run safely.

Do **not** also wrap the whole pass in a global `lock:scheduler:refresh-rules` / advisory lock — that would force a singleton and negate SKIP LOCKED. (A transaction-scoped `pg_advisory_xact_lock` per individual rule is acceptable as belt-and-suspenders, but the global pass lock is not.)

Rule claiming:

```sql
SELECT *
FROM refresh_rules
WHERE enabled = true
  AND next_run_at <= now()
ORDER BY next_run_at
FOR UPDATE SKIP LOCKED;
```

Then update `locked_at`, enqueue, and calculate `next_run_at`.

Enqueue the Celery dispatch task **inside the claiming transaction**, before commit. If the commit then fails, the already-sent task is neutralized by the idempotent dispatch guard and the in-flight match locks; the reverse order (commit, then enqueue) can silently lose a scheduled run if the process dies between the two. Duplicates are cheap here; missed runs are not.

---

## 29. Partitioning and Retention

Append-heavy tables must be designed for growth.

Tables to partition monthly:

```text
price_observations by scraped_at
request_attempts by created_at
webhook_events by created_at
price_alert_events by created_at
```

Retention defaults:

```text
price_observations raw: 90 days
request_attempts raw: 90 days
webhook_events: 90 days
price_alert_events: 1 year
daily rollups: 2 years
```

Maintenance job should:

```text
create next month's partitions
drop expired partitions
create daily rollups
vacuum/analyze partitions if needed
```

These tables are created **partitioned from birth**, in the migration of the phase that first introduces each of them (Sections 22, 35) — creating them plain and converting later is a table rewrite under data, and their primary keys must include the partition key from day one. At 10,000–20,000 matches × multiple attempts × daily, `price_observations` and `request_attempts` reach millions of rows per month per workspace quickly.

Retention must be implemented as **partition drop**, never bulk `DELETE`. DELETE-based retention on these tables causes bloat and vacuum storms. Choosing partition-drop is what makes the 90-day retention cheap.

Maintenance-job ordering: daily rollups for a period must be verified complete **before** the retention job may drop the raw partitions that feed them. Soft references into dropped partitions (Section 22) are expected; readers tolerate them.

---

## 30. Legal and Compliance Guardrails

This backend is for monitoring publicly available product pricing.

Do not support:

```text
login-required scraping
CAPTCHA solving
paywall bypass
private account scraping
credentialed competitor access
abuse of private APIs
session or authentication cookies in scrape profiles
```

Competitors start with:

```text
legal_status = REVIEW_REQUIRED
```

Before production scraping:

```text
review target domain
document whether pages are public
decide robots policy
set request limits
approve competitor
```

If a site blocks heavily or requires login/CAPTCHA, mark it:

```text
DISABLED
```

or:

```text
REVIEW_REQUIRED
```

---

## 31. Observability and Operations

Add from early phases:

```text
structured JSON logs
job status in PostgreSQL
per-domain success rate
per-domain error rate
per-domain avg response time
queue depth visibility
rate-limit hit counts
proxy usage counts
browser fallback counts
strategy promotion/rediscovery events
pgbouncer/postgres connection saturation
spider reactor responsiveness (event-loop lag)
in-spider requeue + overflow-to-Celery counts
idempotent-dispatch dedup skips
redis memory usage + eviction counts (must stay zero on the locks/limits instance)
scrapyd node liveness + per-node queue depth (lost nodes must be detected)
proxy spend vs monthly budget
```

Celery result backend:

```text
disabled by default
```

Reason:

```text
scrape_jobs and scrape_job_targets are the source of truth
```

Optional:

```text
Flower for Celery monitoring
OpenTelemetry-compatible logs/metrics
Sentry or self-hosted error tracker if allowed
```

No external monitoring dependency is required for MVP.

---

## 32. Workspace Isolation

Every workspace-owned entity must include `workspace_id`.

Never fetch by ID alone.

Bad:

```python
session.get(Product, product_id)
```

Good:

```python
select(Product).where(
    Product.id == product_id,
    Product.workspace_id == workspace_id
)
```

Structural enforcement:

```text
Workspace-scoped repository/query helpers
workspace dependency in every route
tests for cross-workspace access
CI lint that forbids session.get() / unscoped selects on workspace-owned models
```

Defense-in-depth (mandatory, from the first workspace-owned table):

```text
PostgreSQL Row-Level Security on every workspace-owned table,
  enabled in the same migration that creates the table
SET LOCAL app.workspace_id = '<workspace_id>' inside the query transaction
  (transaction-scoped — the only form that survives PgBouncer transaction pooling)
policies fail closed: an absent or empty app.workspace_id matches zero rows
```

Query helpers alone are convention — a developer can bypass them without noticing. RLS plus the CI lint is what makes isolation structural rather than advisory.

---

## 33. Security and Secrets

Passwords:

```text
argon2id (or bcrypt) with per-user salt
uniform login failure response (no factor disclosure)
login rate limiting per account and per source address
```

API keys:

```text
high-entropy random secret; store SHA-256 hash + short key_prefix for lookup
show full key only once
support scopes
support revocation
track last_used_at (throttled: at most one write per key per minute)
```

Refresh tokens:

```text
store hashed refresh tokens
rotate on every use; reuse of an already-rotated token is rejected
concurrent refresh: at most one of two simultaneous uses succeeds (atomic rotation)
support logout/revocation
expire tokens
```

Access tokens and revocation:

```text
JWTs carry user id, role, and workspace — verifiable without a DB read
access-token lifetime is short (minutes, not hours)
user/workspace status (disabled, suspended) is checked against a short-TTL
  Redis cache on workspace-scoped requests, so a suspension takes effect
  within the cache TTL rather than the token lifetime
```

Internal-surface authentication:

```text
Scrapyd basic auth on both scraping nodes (its API accepts code uploads)
PgBouncer scram-sha-256 in every deployed environment
containers run as non-root
```

Encrypt:

```text
proxy passwords
webhook secrets
future integration tokens
```

Use:

```text
Fernet key stored in environment variable
key_version column for encrypted fields where needed
```

Rotation story:

```text
support decrypting old key versions
re-encrypt records with new key
then retire old key
```

---

## 34. Error Codes

Use structured error codes:

```text
HTTP_403
HTTP_404
HTTP_429
TIMEOUT
DNS_ERROR
PRICE_NOT_FOUND
VARIANT_NOT_FOUND
INVALID_PRICE_FORMAT
LOW_CONFIDENCE_PRICE
CURRENCY_NOT_FOUND
CURRENCY_MISMATCH
STOCK_NOT_FOUND
BLOCKED
PROXY_FAILED
PLAYWRIGHT_FAILED
SELECTOR_BROKEN
STRATEGY_DEGRADED
RATE_LIMITED
LOCKED_ALREADY_RUNNING
LIMIT_REACHED
LEGAL_REVIEW_REQUIRED
UNKNOWN_ERROR
```

These support:

```text
debugging
strategy optimizer
access policy tuning
rediscovery triggers
client reporting
```

---

## 35. Spec Kit Implementation Plan

Use Spec Kit to implement this project in small independent specs.

Do not implement the full system from one giant prompt.

Each spec should have:

```text
/specify
/clarify
/plan
/tasks
/implement
```

Start with structure, then move feature by feature.

### 00 — Project Constitution

Purpose: define non-negotiables that Claude Code must not violate.

Covers:

```text
Python stack
monorepo
multi-service deployment
FastAPI API
Scrapy + Scrapyd scraping services
no Scrapy inside Celery tasks
workspace isolation (scoped queries + RLS from the first workspace-owned table)
variant-level pricing
DB-driven configuration
no external scraping APIs
public product pages only
SSRF-safe handling of every user-supplied URL
Decimal/NUMERIC money (finite values only)
TIMESTAMPTZ timestamps everywhere
append-heavy tables born partitioned
no per-request hot-row writes
no frontend in v1
```

Acceptance:

```text
Constitution file exists.
Rules are clear.
Future specs reference it.
```

### 01 — Monorepo & Services Skeleton

Purpose: create the repo and service structure only.

Covers:

```text
apps/api
apps/scheduler
apps/workers
apps/scrapers
apps/scrapers-browser
libs/shared + libs/scrape-core (uv workspace members, dependency boundaries per §5)
docker-compose (pinned images, non-root containers, dual-stack binds)
Scrapyd basic auth on both nodes
basic env loading
service start commands
```

Acceptance:

```text
API service boots.
Scheduler service boots.
Worker service boots.
Scrapyd HTTP service boots.
Scrapyd browser service boots.
Postgres, PgBouncer, and Redis run locally.
All services connect through PgBouncer, not directly to Postgres.
GET /health works.
Scrapyd HTTP service is reachable.
Scrapyd browser service is reachable.
```

No business logic beyond health checks.

### 02 — Database Foundation

Purpose: create DB foundation and migration system.

Covers:

```text
SQLAlchemy setup
Alembic setup (one-shot migration job, direct-to-Postgres, single linear history in CI)
DB session handling (pooler-safe; lazy per-process engine; fork-safe)
UUIDv7 ID helper
base model patterns (TIMESTAMPTZ timestamps; naming convention covering ALL constrained columns)
core enums
Decimal/NUMERIC money rule (finite values only — NaN/Infinity rejected)
workspace-scoped model base (RLS-ready)
```

Acceptance:

```text
Alembic migration runs as the dedicated one-shot job.
DB tables can be created.
UUIDv7 IDs work.
Timestamp columns are TIMESTAMPTZ.
Two multi-column uniques sharing a first column get distinct generated names.
Basic DB connection test passes.
```

### 03 — Auth, API Keys, Workspace Isolation

Purpose: secure API foundation.

Covers:

```text
workspaces
users
JWT login (short-lived tokens) + login rate limiting
refresh tokens (rotation; atomic against concurrent use)
API keys (SHA-256 hash + prefix; throttled last_used_at)
API key scopes
workspace context dependency
user/workspace status cache (suspension takes effect within cache TTL)
workspace-scoped repository helpers
Row-Level Security policies on workspace-owned tables
cross-workspace access tests
CI guard against unscoped queries on workspace-owned models
```

Acceptance:

```text
Login works and is rate-limited.
Refresh token works; a rotated token is rejected on reuse.
Logout/revocation works.
API key auth works; a revoked key authenticates nothing.
Workspace context is resolved.
Cross-workspace reads/writes are blocked by tests.
RLS denies cross-workspace rows even when an application filter is missing.
Suspending a workspace cuts off its tokens within the status-cache TTL.
```

### 04 — Catalog: Products, Variants, Groups

Purpose: store client catalog properly.

Covers:

```text
products
product_variants
default variant for simple products
product_groups
product_group_items
bulk product+variant upsert
unique constraints for external_id and SKU
```

Acceptance:

```text
Can create products.
Can create variants.
Simple product automatically has default variant.
Can bulk upsert Woo/Salla-style product payload.
Can group products/variants.
```

### 05 — Competitors & Matches

Purpose: link client variants to competitor product URLs.

Covers:

```text
competitors
competitor_product_matches
URL safety validation (SSRF rules, save-time — Section 11)
URL normalization
URL pattern derivation (versioned algorithm — Section 15)
match health fields
bulk match upsert (set-based)
legal_status and robots_policy fields
```

Acceptance:

```text
Can create competitor.
Can create match linked to product_variant_id.
A variant can have unlimited matches.
Private/internal/metadata URLs are rejected at save time.
URL normalization works.
URL pattern derivation works and records its algorithm version.
Bulk match upsert works.
```

### 06 — Scrape Profiles & Extraction Rules

Purpose: define DB-driven extraction configuration.

Covers:

```text
scrape_profiles
selectors
XPath
regex
JSON-LD settings
embedded JSON settings
variant strategies
validation rules
confidence thresholds
config resolution service
```

Acceptance:

```text
Can create scrape profile.
Can assign profile to competitor or match.
Config resolution returns final profile for a match.
Validation/confidence rules are stored and readable.
```

No real scraping execution yet.

### 07 — Scrapyd HTTP Spider MVP

Purpose: prove mature Scrapy setup through Scrapyd.

Covers:

```text
generic_price_spider
fixture HTML pages
JSON-LD extraction
CSS selector extraction
regex extraction
item pipeline (batched writes — Section 8)
reactor-safe DB writes (async driver or deferToThread — decided once, in scrape-core)
fetch-time URL safety enforcement (SSRF — resolved-IP checks, redirect re-validation)
per-domain robots middleware
price_observations table (created partitioned — Section 22)
DB write of observations
Scrapyd schedule API call (authenticated)
```

Acceptance:

```text
Scrapyd runs generic_price_spider.
Spider accepts workspace_id, scrape_job_id, match_ids.
Spider loads match/config from DB.
Spider scrapes fixture pages.
Spider saves PriceObservation.
```

Use fixture pages first, not real competitor pages.

### 08 — Jobs & Orchestration

Purpose: API triggers scraping through worker and Scrapyd.

Covers:

```text
scrape_jobs
scrape_job_targets
worker calls Scrapyd schedule API (idempotent dispatch guard)
deterministic node selection within each Scrapyd pool (Section 26)
stalled-batch detection + timeout re-dispatch (node loss — Section 26)
Celery engine fork-safety (dispose inherited engine on worker_process_init)
run one match
run one variant
job status endpoints
target status updates
job counters aggregated from targets (not per-target increments)
```

Acceptance:

```text
POST /v1/jobs/run/match/{id} creates job.
Worker schedules Scrapyd.
Spider runs.
Job status can be fetched.
Job results can be fetched.
POST /v1/jobs/run/variant/{id} runs all active matches for variant.
```

### 09 — Current Prices & Alert Logic

Purpose: make scraped data useful.

Covers:

```text
match_current_prices (soft reference to observations — no FK)
variant_price_states
variant_alert_states
price_alert_events (created partitioned — Section 22)
price_analysis Celery task (separate from spider, dedup per variant per job)
price_analysis trigger on client price/currency change (Section 23)
ordered alert decision tree (Decimal arithmetic, quantized boundaries)
currency mismatch handling
current comparison endpoint
current alerts endpoint
```

Acceptance:

```text
After scraping, match current price is updated.
Variant price state is updated.
Current alert state is deterministic.
Alert event history is created when alert changes.
Variant comparison API returns expected result.
```

### 10 — Access Policies, Proxies, Request Attempts

Purpose: controlled access behavior.

Covers:

```text
proxy_providers (encrypted credentials; Redis budget counters — Section 22)
access_policies
domain_access_rules
request_attempts (created partitioned — Section 22)
direct HTTP
direct retry
proxy HTTP
retry rules
proxy assignment to Scrapy requests
attempt logging
```

Acceptance:

```text
Each scrape logs request attempts.
Access policy controls direct/proxy behavior.
Proxy provider config can be stored encrypted.
Domain access rule overrides competitor/workspace defaults.
```

### 11 — Distributed Rate Limiting & In-Flight Locks

Purpose: prevent domain blocking and duplicate work.

Covers:

```text
Redis token bucket or sliding-window limiter (non-blocking on the reactor)
Redis domain semaphore
Redis match lock with fencing token (spider-owned acquire/release)
requeue/delay with jitter
in-spider requeue cap + overflow back to Celery
rate-limit hit logging
LOCKED_ALREADY_RUNNING handling
RATE_LIMITED handling
```

Acceptance:

```text
Multiple workers cannot exceed per-domain limits.
Same match cannot be scraped concurrently.
Rate-limited work is delayed instead of hammering.
```

This must be done before large-scale real scraping.

### 12 — Domain Strategy Optimizer

Purpose: learn what works per domain/template.

Covers:

```text
domain_strategy_profiles
strategy_attempt_stats
strategy_discovery_runs
sample URL discovery
preferred access method learning
preferred extraction method learning
3-confirmation promotion rule
buffered stats (Redis counters flushed atomically — Section 14)
rediscovery triggers
periodic light re-check
```

Acceptance:

```text
New competitor domain can run discovery on sample URLs.
Winning access/extraction method is stored.
Future scrapes start from learned methods.
Stats are buffered and flushed atomically (no per-attempt hot-row writes).
Rediscovery can be triggered after repeated failures.
```

### 13 — Scheduler

Purpose: dynamic recurring jobs.

Covers:

```text
refresh_rules
DB-driven scheduler service
next_run_at
safe claiming with FOR UPDATE SKIP LOCKED
enqueue inside the claiming transaction (Section 28)
workspace schedules
competitor schedules
product schedules
variant schedules
product group schedules
match schedules
```

Acceptance:

```text
Daily workspace refresh can be configured.
Hourly product group refresh can be configured.
Scheduler enqueues jobs without duplicate runs.
```

### 14 — Browser Scraping Service

Purpose: internal JS fallback.

Covers:

```text
scrapyd-browser-service
scrapy-playwright
browser spider
BROWSER scrape profile
PLAYWRIGHT_PROXY access method
wait_for_selector
browser worker limits
variant selection if configured
```

Acceptance:

```text
Browser service runs separately.
Only browser-mode matches are sent to browser service.
JS-rendered fixture page can be scraped.
Browser concurrency is controlled.
```

### 15 — Retention, Rollups & Partition Maintenance

Purpose: protect Postgres growth over time.

The append-heavy tables are already partitioned — each was created partitioned in the phase that introduced it (07, 09, 10, 16). This phase delivers the jobs that keep partitioning working.

Covers:

```text
partition creation job (next month's partitions, created in advance)
retention job (partition drop, never bulk DELETE)
daily rollups
rollups-verified-before-drop ordering guarantee
dangling soft-reference tolerance checks
```

Acceptance:

```text
Next month's partitions are created ahead of time.
Old partitions are dropped per retention, only after their rollups are verified.
Daily rollups are generated.
Readers tolerate soft references into dropped partitions.
Large append-heavy tables remain manageable.
```

### 16 — Webhook Events

Purpose: integration readiness.

Covers:

```text
webhook_endpoints (URL safety validation — Section 11)
webhook_events (created partitioned — Section 22)
event creation on alert/job/strategy changes
event polling API
pagination
retention
```

Acceptance:

```text
Events are created.
External systems can poll events.
No automatic delivery is required yet.
```

---

## 36. Recommended Implementation Order

Start with:

```text
00 Project Constitution
01 Monorepo & Services Skeleton
02 Database Foundation
03 Auth, API Keys, Workspace Isolation
04 Catalog: Products, Variants, Groups
05 Competitors & Matches
06 Scrape Profiles & Extraction Rules
07 Scrapyd HTTP Spider MVP
08 Jobs & Orchestration
09 Current Prices & Alert Logic
```

Then add:

```text
10 Access Policies, Proxies, Request Attempts
11 Distributed Rate Limiting & In-Flight Locks
12 Domain Strategy Optimizer
13 Scheduler
14 Browser Scraping Service
15 Retention, Rollups & Partition Maintenance
16 Webhook Events
```

Partitioning is NOT deferred to phase 15: each append-heavy table is created partitioned in the phase that introduces it (07 observations, 09 alert events, 10 request attempts, 16 webhook events). Phase 15 only adds the maintenance/retention/rollup jobs.

Do not start with the scheduler, browser, or optimizer.

First prove:

```text
product variant
↓
manual match
↓
Scrapyd HTTP spider
↓
observation saved
↓
current price updated
↓
alert generated
↓
API returns comparison
```

---

## 37. Quick MVP Test

Build this vertical slice first:

```text
DB config
↓
API trigger
↓
Celery orchestration
↓
Scrapyd schedule
↓
Scrapy HTTP spider
↓
fixture page scrape
↓
Observation saved
↓
Current price updated
↓
Alert generated
↓
API returns result
```

Minimal endpoints:

```text
GET /health
POST /v1/bootstrap/demo
POST /v1/jobs/run/match/{match_id}
POST /v1/jobs/run/variant/{variant_id}
GET /v1/jobs/{job_id}
GET /v1/jobs/{job_id}/results
GET /v1/variants/{variant_id}/price-comparison
GET /v1/alerts/current
GET /v1/observations
```

Demo seed:

```text
1 workspace
3 products
5 variants
3 competitors
15 competitor product matches
2 scrape profiles:
  - jsonld_first
  - selector_only
```

Use fixture HTML pages first.

---

## 38. What Not To Build in V1

Do not build:

```text
frontend/dashboard
billing
auto product matching
auto repricing
external scraping API integrations
CAPTCHA solving
login bypassing
raw HTML archive
screenshot archive
AI matching
full SaaS roles/permissions
marketplace-wide crawling
browser-first scraping for all URLs
```

---

## 39. V1 Success Criteria

The backend is successful when it can:

```text
1. Store multiple workspaces.
2. Store products and variants.
3. Store 2,000 products per workspace.
4. Store 10,000–20,000 competitor product matches per workspace.
5. Run one match manually.
6. Run one variant manually.
7. Run one product manually.
8. Run one competitor manually.
9. Run one full workspace manually.
10. Use Scrapyd-managed Scrapy HTTP spiders.
11. Extract competitor prices using configured profiles.
12. Save request attempts.
13. Save observations.
14. Update match current price.
15. Update variant current comparison state.
16. Generate variant-level current alert state.
17. Store alert event history.
18. Expose comparison results by API.
19. Enforce distributed per-domain rate limits.
20. Prevent duplicate in-flight match scrapes.
21. Use DB-driven access policies.
22. Learn preferred access/extraction strategy for a new domain.
23. Start future scrapes from learned strategy instead of trying everything.
24. Run daily and hourly schedules.
25. Keep append-heavy tables manageable with partitioning/retention.
26. Be ready for a future WooCommerce/Salla plugin.
27. Survive Celery retries without duplicate scrapes (idempotent Scrapyd dispatch).
28. Sustain concurrent spider processes without exhausting Postgres connections (PgBouncer + capped pools).
29. Keep the spider reactor responsive (no blocking DB or rate-limiter calls).
30. Reject private/internal URLs at save time and at fetch time (SSRF-safe).
31. Enforce workspace isolation with RLS in addition to query scoping.
32. Survive a restart of the locks/limits Redis without unbounded duplicate scraping.
33. Detect and re-dispatch batches lost to a dead Scrapyd node.
34. Reflect a client price change in alert state without waiting for a scrape.
```

---

## 40. Recommended Pilot

Start with:

```text
1 workspace
100 products
150 variants
5 competitors
500 competitor URLs
fixture HTML pages first
then 50 real URLs
then 500 real URLs
```

Pilot goals:

```text
manual match scrape works
manual variant scrape works
structured-data extraction works
CSS selector extraction works
observations are saved
current prices are updated
alerts are generated
domain rate limiter works
in-flight dedup works
domain strategy discovery works on sample URLs
learned strategy is used for future URLs
```

Then scale to:

```text
2,000 products
5–10 competitors
10,000–20,000 competitor URLs
daily full refresh
hourly priority product group refresh
```

---

## 41. Final Implementation Notes

Build this as a configurable price monitoring backend, not as a hardcoded scraper.

Core idea:

```text
Products/variants define what the client sells.
Competitor matches define where to scrape.
Scrape profiles define how to extract.
Access policies define how to access.
Scrapyd/Scrapy performs scraping.
Redis controls rate limits and locks.
Domain strategy profiles learn what works.
Jobs execute scrape requests.
Observations store history.
Current price tables make reads fast.
Alert states explain pricing position.
Events expose changes for integrations.
```

The most important parts to get right from day one:

```text
workspace isolation (scoped queries + RLS, shipped with each table)
variant-level pricing
match-level competitor URLs
SSRF-safe URL validation at save time and fetch time
DB-driven configuration
Scrapyd-managed Scrapy services (authenticated, never public)
no Scrapy inside Celery tasks
reactor-safe (non-blocking) spider DB and rate-limit calls
PgBouncer + capped connection pools (with the connection budget done up front)
lazy per-process engines; fork-safe Celery workers; migrations as a direct-to-Postgres one-shot job
idempotent Scrapyd dispatch
price analysis as a separate deduplicated Celery task (also triggered by client price changes)
no hot-row contention anywhere (variant state, job counters, key last_used_at, strategy stats)
append-heavy tables created partitioned, referenced only by soft references
split Redis: broker vs. noeviction locks/limits state
structured request attempts
structured extraction results
confidence scoring
distributed domain rate limiting
in-flight deduplication (spider-owned, fenced lock)
current price denormalization
clear alert decision tree (Decimal, quantized, deterministic)
domain strategy optimizer (versioned URL patterns, buffered stats)
API-first job control
Spec Kit incremental implementation
```
