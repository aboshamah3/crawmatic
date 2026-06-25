# Price Monitoring Backend — Python Architecture Spec v3

## 0. Major Changes From v2

This version revises the plan after technical review.

Main changes:

1. **Drop Scrapy from v1.**
   - The workload is mostly known product URLs, not a broad crawl.
   - Running Scrapy inside Celery adds Twisted reactor complexity.
   - Replace Scrapy with a direct internal extraction engine:
     - `httpx` for HTTP fetching.
     - `parsel` for CSS/XPath extraction.
     - `extruct` for JSON-LD/microdata/OpenGraph extraction.
     - Python `re` for regex extraction.
     - Playwright Python for internal browser fallback.

2. **Keep Celery + Redis for jobs.**
   - Celery remains the background job engine.
   - Redis remains broker and locking/rate-limit store.
   - Celery result backend is optional and disabled by default because job state lives in PostgreSQL.

3. **Replace APScheduler with a small DB-driven scheduler enqueuer.**
   - Schedules live in PostgreSQL in `refresh_rules`.
   - A scheduler service claims due rules and enqueues Celery jobs.
   - Use Redis lock or PostgreSQL advisory lock to prevent duplicate scheduler execution.

4. **Add distributed per-domain rate limiting.**
   - Redis token bucket/sliding-window limiter keyed by workspace + domain + access method.
   - Every worker must acquire a token before fetching.
   - If no token is available, the task is requeued with delay and jitter.

5. **Add in-flight deduplication.**
   - Avoid scraping the same match concurrently from scheduled + manual runs.
   - Use Redis lock keyed by workspace + match ID.
   - Also use DB uniqueness for job targets.

6. **Add time-series partitioning and retention.**
   - `price_observations`, `request_attempts`, `webhook_events`, and alert event history must be monthly partitioned by timestamp.
   - Raw observations/attempts retained for a configurable period.
   - Daily rollups retained longer.

7. **Add current-price tables.**
   - Do not calculate latest prices by scanning historical observations.
   - Maintain `match_current_prices` and `variant_price_states`.

8. **Fix alert precedence and boundaries.**
   - Alert logic is now a single ordered decision tree.

9. **Use Decimal/NUMERIC for money.**
   - Never use float for prices.
   - Reject cross-currency comparison unless an FX module is explicitly added later.

10. **Add explicit unique constraints/upsert keys.**
    - Products, variants, competitors, and matches need natural uniqueness rules.

11. **Add current alert state + alert event history.**
    - One mutable current alert state per variant.
    - Append-only event history for changes.

12. **Choose ID strategy.**
    - Use application-generated UUIDv7 for primary IDs.
    - UUIDv7 is time-ordered and better for insert-heavy indexed tables than random UUIDv4.

13. **Define URL pattern derivation.**
    - Domain Strategy Optimizer depends on consistent domain + URL pattern grouping.

14. **Add compliance and operational guardrails.**
    - Public product pages only.
    - No login bypass, CAPTCHA solving, paywall bypass, or account-based scraping.
    - Per-domain review/allowlist before production scraping.

---

## 1. Purpose

Build an internal-first, SaaS-ready backend for competitor price monitoring.

The backend monitors a client store's products and variants against manually matched competitor product URLs.

It should be API-first so a future WooCommerce plugin, Salla integration, n8n workflow, admin script, or dashboard can fully control it.

V1 does **not** need a frontend.

V1 does **not** need automatic product matching.

V1 does **not** need auto-repricing.

V1 does **not** use external scraping APIs or unlocker services.

---

## 2. Final Stack

```text
Language: Python
API: FastAPI
Database: PostgreSQL
ORM: SQLAlchemy
Migrations: Alembic
Queue: Celery
Broker: Redis
Scheduler: Custom DB-driven scheduler enqueuer
HTTP Fetching: httpx
HTML/CSS/XPath Extraction: parsel
Structured Data Extraction: extruct
Regex Extraction: Python re
Browser Fallback: Playwright Python
Deployment: Railway first
```

### Why not Scrapy in v1

Scrapy is a strong scraping framework, but this project is not primarily a crawl frontier problem. The backend already knows the exact competitor product URLs to fetch.

The main workload is:

```text
known URL
↓
fetch page
↓
extract price
↓
save observation
```

Using Scrapy inside Celery can introduce Twisted reactor lifecycle issues. To keep Celery integration simple and predictable, use a direct internal extraction engine in v1.

Scrapy can be reconsidered later only if the product evolves into broad site crawling or discovery.

---

## 3. Core Constraints

1. The backend is internal-first but SaaS-ready.
2. No frontend in v1.
3. Configuration is mainly stored in the DB.
4. Products can have variants.
5. Price monitoring happens at variant level.
6. Competitor product URLs are manually matched in v1.
7. Each product variant can have unlimited competitor matches.
8. API must support running:
   - one match
   - one variant
   - one product
   - one competitor
   - one product group
   - one workspace
9. Refresh frequency must be dynamic:
   - workspace daily
   - competitor daily
   - product group hourly
   - variant hourly
   - match on demand
10. Store extracted results only.
11. Do not store raw HTML or screenshots in v1.
12. No external scraping APIs/services.
13. Proxies and browser fallback must be internal.
14. The engine must learn the best access and extraction strategy per domain/template.
15. Webhook/event architecture should be ready, but webhook delivery can be added later.
16. Public product pages only.
17. No CAPTCHA solving, login bypassing, account scraping, paywall bypassing, or abuse of private APIs.

---

## 4. High-Level Architecture

```text
External Systems
WooCommerce Plugin / Salla / n8n / Admin Script / Future Dashboard
        |
        v
FastAPI Backend
Auth / Workspaces / Products / Variants / Competitors / Matches / Jobs / Alerts / Events
        |
        +--------------------+
        |                    |
        v                    v
PostgreSQL              Redis
Config + Results        Celery Broker + Locks + Rate Limits
        |                    |
        v                    v
Scheduler Enqueuer      Celery Workers
DB-driven schedules     HTTP/Playwright extraction jobs
                             |
                             v
                    Competitor Websites
                    Direct HTTP / Internal Proxy / Internal Playwright
```

---

## 5. Main Services

### 5.1 API Service

FastAPI service responsible for:

- Authentication.
- API keys.
- Workspace isolation.
- Product and variant management.
- Competitor management.
- Competitor product URL matching.
- Scrape profile management.
- Access policy and proxy provider management.
- Dynamic refresh rules.
- Manual scrape job triggers.
- Job status.
- Observations/results API.
- Alerts API.
- Webhook endpoint/event API.
- Domain strategy discovery endpoints.

The API service does not scrape directly. It creates jobs and reads results.

---

### 5.2 Worker Service

Celery workers responsible for:

- Running scrape jobs.
- Fetching pages with HTTPX.
- Fetching JS-rendered pages with internal Playwright when configured.
- Saving request attempts.
- Saving price observations.
- Updating current price state.
- Running price analysis.
- Creating/updating alert state.
- Creating alert events.
- Creating webhook events.
- Updating domain strategy stats.
- Retrying failed jobs according to policy.

---

### 5.3 Scheduler Enqueuer Service

A lightweight service responsible for:

- Reading due `refresh_rules` from PostgreSQL.
- Claiming due rules safely.
- Enqueuing Celery dispatch jobs.
- Updating `next_run_at`.
- Avoiding duplicate execution using Redis lock or PostgreSQL advisory lock.

Do not use APScheduler in v1.

Do not rely on Celery task-level rate limits for domain protection.

---

### 5.4 Extraction Engine

Internal Python package responsible for:

- Resolving access method.
- Acquiring distributed domain rate-limit tokens.
- Fetching page with HTTPX or Playwright.
- Applying extraction methods.
- Normalizing price/currency/stock.
- Validating confidence.
- Returning structured result.

The extraction engine is not a crawler. It is a known-URL price extraction engine.

---

## 6. Configuration Philosophy

The backend code is the engine.

The database controls:

- Workspaces.
- Products and variants.
- Competitors.
- Matched competitor URLs.
- Refresh schedules.
- Access policies.
- Proxy providers.
- Domain rate limits.
- Extraction profiles.
- Selectors, XPath rules, regex rules.
- Price validation rules.
- Learned domain/template strategy.
- Current alert state and event history.

### Config resolution order

For extraction configuration:

```text
Match-level override
↓
Domain strategy profile preferred method
↓
Competitor-level default
↓
Workspace-level default
↓
Global default
```

For access/proxy configuration:

```text
Match-level access policy
↓
Domain strategy profile preferred method
↓
Domain access rule
↓
Competitor-level default
↓
Workspace-level default
↓
Global default
```

---

## 7. Internal Access and Proxy Strategy

No external scraping APIs or unlocker services should be used.

Allowed access methods:

```text
DIRECT_HTTP
DIRECT_HTTP_RETRY
PROXY_HTTP
PLAYWRIGHT_PROXY
```

Default fallback chain for unknown domains:

```text
Attempt 1: Direct HTTP
Attempt 2: Direct HTTP retry with backoff
Attempt 3: HTTP through internal proxy pool
Attempt 4: Internal Playwright through internal proxy pool
Attempt 5: Failed → needs profile/proxy/selector tuning
```

For learned domains, do not start from attempt 1 every time. Start from the learned best access method.

Example:

If direct access consistently fails for:

```text
competitor.com/products/*
```

future scrapes should start with:

```text
PROXY_HTTP
```

---

## 8. Distributed Domain Rate Limiting

This is mandatory before scaling.

Celery task rate limits are not enough because they are per worker instance. With multiple workers, per-worker limits multiply and can still hammer a domain.

### 8.1 Required component

Add:

```text
DistributedRateLimiter
```

Backed by Redis.

Every worker must acquire a token before fetching a URL.

Rate-limit keys should include:

```text
workspace_id
domain
access_method
```

Example Redis keys:

```text
rate:{workspace_id}:{domain}:DIRECT_HTTP
rate:{workspace_id}:{domain}:PROXY_HTTP
rate:{workspace_id}:{domain}:PLAYWRIGHT_PROXY
```

### 8.2 Token logic

Use a Redis atomic script or transaction.

Required behavior:

```text
if token available:
  allow request
else:
  return wait time
```

If no token is available:

```text
requeue task with countdown = wait_time + jitter
```

### 8.3 Jitter

Add jitter to prevent a thundering herd.

Example:

```text
delay = wait_time + random(2, 20 seconds)
```

### 8.4 Domain concurrency

Also control concurrent in-flight requests per domain.

Use Redis semaphore keys:

```text
semaphore:{workspace_id}:{domain}:{access_method}
```

Release the semaphore after the request finishes or after TTL.

---

## 9. In-Flight Deduplication

Scheduled jobs and manual jobs may target the same match at the same time.

Avoid duplicate scraping.

### 9.1 Redis match lock

Before scraping a match:

```text
lock:scrape:{workspace_id}:{match_id}
```

TTL example:

```text
10 minutes for HTTP
30 minutes for Playwright
```

If lock exists:

```text
skip, attach to existing job, or requeue after delay
```

### 9.2 Job targets table

Add a `scrape_job_targets` table.

```text
scrape_job_targets
- id
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

Unique constraint:

```text
unique(scrape_job_id, match_id)
```

This prevents duplicates inside the same high-level job.

---

## 10. Domain Strategy Optimizer

### 10.1 Purpose

The Domain Strategy Optimizer reduces server and proxy usage by learning the best access method and extraction method per competitor domain and URL pattern.

It avoids trying all access/extraction methods for every product.

### 10.2 Learn by domain + URL pattern

The same domain can have different templates:

```text
example.com/products/*
example.com/p/*
example.com/ar/products/*
example.com/offers/*
```

So the optimizer learns by:

```text
workspace_id + competitor_id + domain + url_pattern
```

### 10.3 Access methods

```text
DIRECT_HTTP
DIRECT_HTTP_RETRY
PROXY_HTTP
PLAYWRIGHT_PROXY
```

### 10.4 Extraction methods

```text
PLATFORM_PATTERN
JSON_LD
EMBEDDED_JSON
CSS_SELECTOR
XPATH
REGEX
PLAYWRIGHT_RENDERED_SELECTOR
```

### 10.5 Discovery mode

When a new competitor domain or URL pattern is added:

```text
New competitor/domain added
↓
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

Discovery mode may try multiple methods because it runs on a small sample, not on the whole catalog.

### 10.6 Production mode

After a strategy is learned:

```text
Load match
↓
Resolve domain strategy profile
↓
Start with preferred access method
↓
Start with preferred extraction method
↓
If success: save observation and update stats
↓
If failure: try allowed fallback methods
↓
If fallback works repeatedly: update learned profile
```

### 10.7 Promotion rule

A method can become preferred after:

```text
3 successful extractions
across at least 3 different URLs
same domain + URL pattern
confidence >= configured threshold, default 0.85
valid numeric price
valid currency when required
```

### 10.8 Rediscovery triggers

Re-run discovery if:

```text
3 consecutive failures for preferred method
success rate drops below 80%
selector returns empty repeatedly
price confidence drops below 0.75 repeatedly
site starts returning 403/429 repeatedly
currency disappears
price values become unrealistic
template appears changed
```

### 10.9 Periodic light re-check

Every 7–14 days, optionally test a cheaper earlier method on a tiny sample.

Example:

If current preferred access is `PROXY_HTTP`, occasionally test `DIRECT_HTTP` on one URL.

### 10.10 Race-safe stats

Strategy stats must use atomic SQL increments.

Do not use read-modify-write in application code.

Example:

```sql
UPDATE strategy_attempt_stats
SET
  attempt_count = attempt_count + 1,
  success_count = success_count + 1,
  last_success_at = now()
WHERE id = :id;
```

Promotion and rediscovery decisions must use row-level locking or advisory locks.

---

## 11. URL Pattern Derivation

The optimizer depends on stable URL pattern grouping.

Add a deterministic function:

```text
derive_url_pattern(url)
```

### 11.1 Normalization steps

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

### 11.2 ID-like segment rules

Replace segment with `:id` if:

```text
all digits
UUID-like
long mixed alphanumeric ID
contains mostly digits
```

### 11.3 Slug rules

For common product routes:

```text
/products/<slug>        → /products/*
/product/<slug>         → /product/*
/p/<id-or-slug>         → /p/*
/item/<id-or-slug>      → /item/*
/ar/products/<slug>     → /ar/products/*
```

### 11.4 Manual override

Allow manual override in `domain_access_rules` or `domain_strategy_profiles`:

```text
url_pattern_override
```

This is important because heuristics will not be perfect.

---

## 12. Price Extraction Strategy

Extraction methods:

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

### 12.1 Platform/product pattern

This means using the competitor site’s own public product data where available. It is not an external scraping API.

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

### 12.2 JSON-LD / structured data

Use `extruct` to extract embedded structured metadata such as JSON-LD and microdata.

### 12.3 Embedded JSON

Extract product data from scripts like:

```html
<script>
  window.__PRODUCT__ = {
    price: 5500,
    currency: "SAR",
    variants: [...]
  }
</script>
```

### 12.4 CSS selector

Use `parsel` for CSS selectors.

Example:

```text
.price .amount
```

### 12.5 XPath selector

Use `parsel` for XPath selectors.

Example:

```text
//span[contains(@class, 'price')]/text()
```

### 12.6 Regex

Use Python `re`.

Example:

```text
"price"\s*:\s*"?(?P<price>[0-9.,]+)"?
```

### 12.7 Playwright-rendered selector

Use Playwright only when the page requires JavaScript rendering or variant selection.

No external browser API.

---

## 13. Extraction Confidence

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

These values are tunable DB configuration, not hardcoded constants.

Default minimum accepted confidence:

```text
0.75
```

Default strategy promotion threshold:

```text
0.85
```

---

## 14. Price Validation Rules

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
  "reject_if_text_contains": [
    "save",
    "discount",
    "off",
    "خصم",
    "وفر"
  ],
  "prefer_text_contains": [
    "price",
    "sale",
    "السعر"
  ]
}
```

---

## 15. Money and Currency

Use Decimal in Python and NUMERIC in PostgreSQL.

Never use float for prices.

Recommended SQLAlchemy type:

```text
Numeric(18, 4)
```

### Currency comparison rule

Do not compare prices across different currencies in v1.

If client price currency and competitor price currency differ:

```text
mark observation valid but not comparable
exclude from price analysis
create warning/error: CURRENCY_MISMATCH
```

An FX conversion module can be added later, but it is not part of v1.

---

## 16. Variants

Price monitoring happens at variant level.

Product = parent catalog item.

ProductVariant = actual sellable/priced item.

Example:

```text
Product: iPhone 15
Variant: iPhone 15 - 128GB - Black → 2,999 SAR
Variant: iPhone 15 - 256GB - Black → 3,399 SAR
```

Competitor matches should link to `product_variant_id`.

Even simple products should have one default variant.

---

## 17. Variant Extraction Strategies

Supported variant strategies:

```text
PAGE_SINGLE_PRICE
URL_HAS_VARIANT_SELECTED
HTML_VARIANT_TABLE
EMBEDDED_JSON_VARIANTS
SELECT_VARIANT_WITH_PLAYWRIGHT
CUSTOM_VARIANT_ADAPTER
```

### 17.1 PAGE_SINGLE_PRICE

Use when each competitor URL points to one simple product or one selected variant.

### 17.2 URL_HAS_VARIANT_SELECTED

Use when the URL already selects the variant:

```text
/product/iphone-15?variant=12345
/product/iphone-15-128gb-black
```

### 17.3 HTML_VARIANT_TABLE

Use when the page contains a visible table/dropdown of variant prices.

### 17.4 EMBEDDED_JSON_VARIANTS

Use when all variant prices are stored in embedded page JSON.

### 17.5 SELECT_VARIANT_WITH_PLAYWRIGHT

Use when price updates only after selecting size/color in a browser.

Use only when needed.

### 17.6 CUSTOM_VARIANT_ADAPTER

Use only for special sites that cannot be handled generically.

---

## 18. ID Strategy

Use application-generated UUIDv7 for primary IDs.

Reason:

- Globally unique.
- Time-ordered.
- Better for insert-heavy indexed tables than random UUIDv4.
- Can be generated in the app even if the database version does not support native UUIDv7.

All IDs should be exposed as public API IDs.

---

## 19. Database Models

Use SQLAlchemy models and Alembic migrations.

All workspace-owned tables must include `workspace_id`.

Money columns must use NUMERIC/Decimal.

Append-heavy timestamp tables should be partitioned by month.

---

### 19.1 Workspace

```text
workspaces
- id uuidv7 pk
- name
- slug
- status
- default_scrape_profile_id nullable
- default_access_policy_id nullable
- created_at
- updated_at
```

Unique:

```text
unique(slug)
```

---

### 19.2 User

```text
users
- id uuidv7 pk
- workspace_id nullable
- email
- password_hash
- role
- status
- created_at
- updated_at
```

Unique:

```text
unique(email)
```

Roles:

```text
SUPER_ADMIN
WORKSPACE_ADMIN
READ_ONLY
```

---

### 19.3 RefreshToken

```text
refresh_tokens
- id uuidv7 pk
- user_id
- token_hash
- expires_at
- revoked_at nullable
- created_at
```

Use this for refresh token revocation.

---

### 19.4 ApiKey

```text
api_keys
- id uuidv7 pk
- workspace_id
- name
- key_hash
- scopes json/list
- status
- last_used_at nullable
- created_at
- revoked_at nullable
```

Example scopes:

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

---

### 19.5 Product

Parent product.

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
```

Unique constraints:

```text
unique(workspace_id, external_id) where external_id is not null
unique(workspace_id, sku) where sku is not null
```

---

### 19.6 ProductVariant

Actual priced/sellable item.

```text
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
```

Unique constraints:

```text
unique(workspace_id, external_id) where external_id is not null
unique(workspace_id, sku) where sku is not null
unique(workspace_id, product_id, title)
```

For simple products, create a default variant.

---

### 19.7 ProductGroup

```text
product_groups
- id uuidv7 pk
- workspace_id
- name
- description nullable
- status
- created_at
- updated_at
```

Unique:

```text
unique(workspace_id, name)
```

Relationship table:

```text
product_group_items
- id uuidv7 pk
- workspace_id
- product_group_id
- product_id nullable
- product_variant_id nullable
- created_at
```

Unique:

```text
unique(workspace_id, product_group_id, product_id)
unique(workspace_id, product_group_id, product_variant_id)
```

---

### 19.8 Competitor

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
```

Unique:

```text
unique(workspace_id, domain)
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

Default should be:

```text
legal_status = REVIEW_REQUIRED
robots_policy = REVIEW_REQUIRED
```

---

### 19.9 CompetitorProductMatch

Links a product variant to a competitor product URL.

```text
competitor_product_matches
- id uuidv7 pk
- workspace_id
- product_id
- product_variant_id
- competitor_id
- competitor_url
- normalized_competitor_url
- url_pattern
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
unique(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)
```

Indexes:

```text
(workspace_id, product_variant_id)
(workspace_id, competitor_id)
(workspace_id, url_pattern)
(workspace_id, status)
```

Priority:

```text
LOW
NORMAL
HIGH
CRITICAL
```

Status:

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

---

### 19.10 ScrapeProfile

Defines how to extract data from a page.

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
```

Modes:

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

Unique:

```text
unique(workspace_id, name)
```

---

### 19.11 ProxyProvider

Internal proxy providers only.

```text
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
```

Types:

```text
DATACENTER
RESIDENTIAL
MOBILE
```

No external scraping API provider type in v1.

---

### 19.12 AccessPolicy

Controls how a URL should be accessed.

```text
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
```

Strategies:

```text
DIRECT_ONLY
DIRECT_THEN_PROXY
PROXY_FIRST
RESIDENTIAL_ONLY
BROWSER_FALLBACK
```

No external scraping API strategy in v1.

---

### 19.13 DomainAccessRule

Domain-level rules.

```text
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

Unique:

```text
unique(workspace_id, competitor_id, domain, url_pattern)
```

---

### 19.14 DomainStrategyProfile

Learned best strategy per domain/template.

```text
domain_strategy_profiles
- id uuidv7 pk
- workspace_id
- competitor_id
- domain
- url_pattern
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
```

Unique:

```text
unique(workspace_id, competitor_id, domain, url_pattern)
```

Status:

```text
DISCOVERY_REQUIRED
LEARNING
ACTIVE
DEGRADED
DISABLED
```

---

### 19.15 StrategyAttemptStat

Tracks success/failure by method.

```text
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
```

Unique:

```text
unique(domain_strategy_profile_id, method_type, method_name)
```

Method type:

```text
ACCESS
EXTRACTION
```

---

### 19.16 StrategyDiscoveryRun

Tracks discovery mode.

```text
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

Status:

```text
PENDING
RUNNING
COMPLETED
FAILED
PARTIAL
```

---

### 19.17 RefreshRule

Dynamic schedule configuration.

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
```

Scopes:

```text
WORKSPACE
COMPETITOR
PRODUCT
VARIANT
PRODUCT_GROUP
MATCH
```

Indexes:

```text
(workspace_id, enabled, next_run_at)
```

---

### 19.18 ScrapeJob

High-level job.

```text
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
```

Types:

```text
MANUAL
SCHEDULED
API_TRIGGERED
RETRY_FAILED
DISCOVERY
```

Statuses:

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

---

### 19.19 ScrapeJobTarget

Prevents duplicates inside one scrape job.

```text
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

Unique:

```text
unique(scrape_job_id, match_id)
```

---

### 19.20 RequestAttempt

Append-only request attempt log.

This table must be monthly partitioned by `created_at`.

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
```

Access methods:

```text
DIRECT_HTTP
DIRECT_HTTP_RETRY
PROXY_HTTP
PLAYWRIGHT_PROXY
```

Indexes per partition:

```text
(workspace_id, match_id, created_at desc)
(workspace_id, scrape_job_id)
(workspace_id, error_code, created_at desc)
```

Retention default:

```text
90 days raw
```

---

### 19.21 PriceObservation

Append-only extracted competitor price result.

This table must be monthly partitioned by `scraped_at`.

```text
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
```

Stock statuses:

```text
IN_STOCK
OUT_OF_STOCK
UNKNOWN
```

Indexes per partition:

```text
(workspace_id, match_id, scraped_at desc)
(workspace_id, product_variant_id, scraped_at desc)
(workspace_id, scrape_job_id)
(workspace_id, success, scraped_at desc)
```

Retention default:

```text
90 days raw observations
daily rollups retained 2 years
```

---

### 19.22 MatchCurrentPrice

Current state per match.

Do not scan `price_observations` for latest price.

```text
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
```

Unique:

```text
unique(workspace_id, match_id)
```

Update this in the same transaction as inserting a successful observation.

Only update if the new observation is newer than the stored `scraped_at`.

---

### 19.23 VariantPriceState

Current comparison state per variant.

```text
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

Unique:

```text
unique(workspace_id, product_variant_id)
```

---

### 19.24 VariantAlertState

One mutable current alert state per variant.

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
```

Unique:

```text
unique(workspace_id, product_variant_id)
```

Status:

```text
OPEN
RESOLVED
IGNORED
```

---

### 19.25 PriceAlertEvent

Append-only alert history.

This table should be monthly partitioned by `created_at`.

```text
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
```

Event types:

```text
CREATED
UPDATED
RESOLVED
REOPENED
UNCHANGED
```

---

### 19.26 Daily Rollup

```text
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
```

Unique:

```text
unique(workspace_id, product_variant_id, date)
```

---

### 19.27 WebhookEndpoint

Prepared for future integrations.

```text
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
```

---

### 19.28 WebhookEvent

This table should be monthly partitioned by `created_at`.

Events can be fetched by API in v1.

Automatic delivery can come later.

```text
webhook_events
- id uuidv7 pk
- workspace_id
- event_type
- payload json
- status
- created_at
- delivered_at nullable
```

Event types:

```text
price.alert.created
price.alert.updated
scrape.job.completed
scrape.job.failed
match.scrape.failed
product.comparison.updated
domain.strategy.updated
```

Retention default:

```text
90 days
```

---

## 20. Alert Logic

Alert logic is variant-level and must be a single ordered decision tree.

### 20.1 Inputs

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

### 20.2 Currency mismatch

If competitor currency differs from client currency:

```text
exclude from comparison
mark current price comparable = false
store error/warning CURRENCY_MISMATCH
```

Do not compare cross-currency in v1.

### 20.3 Decision tree

Ordered rules:

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

### 20.4 Boundary values

```text
Exactly 0% lower = CLOSE_TO_COMPETITORS
More than 0% and less than 1% lower = CLOSE_TO_COMPETITORS
Exactly 1% lower = NORMAL
Exactly 5% lower = NORMAL
More than 5% lower = CHANCE_TO_INCREASE_PRICE
```

### 20.5 Alert types

```text
NO_COMPETITOR_DATA
RISK
HIGH_PRICE
CHANCE_TO_INCREASE_PRICE
NORMAL
CLOSE_TO_COMPETITORS
```

### 20.6 Severity

```text
NO_COMPETITOR_DATA = LOW
RISK = CRITICAL
HIGH_PRICE = HIGH
CHANCE_TO_INCREASE_PRICE = MEDIUM
NORMAL = NONE
CLOSE_TO_COMPETITORS = MEDIUM
```

---

## 21. Partitioning and Retention

Append-heavy tables must be designed for growth from day one.

Tables to partition monthly:

```text
price_observations by scraped_at
request_attempts by created_at
webhook_events by created_at
price_alert_events by created_at
```

Use PostgreSQL native range partitioning.

Retention defaults:

```text
price_observations raw: 90 days
request_attempts raw: 90 days
webhook_events: 90 days
price_alert_events: 1 year
daily rollups: 2 years
```

Retention must be configurable per workspace later.

Add a maintenance job to:

```text
create next month's partitions
drop expired partitions
create daily rollups
vacuum/analyze partitions if needed
```

---

## 22. API Design

Base path:

```text
/v1
```

All list endpoints must use cursor-based pagination.

### 22.1 Pagination standard

Request:

```text
GET /v1/observations?limit=100&cursor=...
```

Response:

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

All high-volume endpoints need filters.

---

### 22.2 Health

```text
GET /health
```

---

### 22.3 Auth

```text
POST /v1/auth/login
POST /v1/auth/refresh
POST /v1/auth/logout
```

---

### 22.4 API Keys

```text
POST /v1/api-keys
GET /v1/api-keys
DELETE /v1/api-keys/{id}
```

---

### 22.5 Products

```text
POST /v1/products
GET /v1/products
GET /v1/products/{id}
PATCH /v1/products/{id}
DELETE /v1/products/{id}
POST /v1/products/bulk-upsert
```

Bulk upsert supports nested variants.

---

### 22.6 Variants

```text
GET /v1/variants
GET /v1/variants/{id}
PATCH /v1/variants/{id}
POST /v1/variants/bulk-upsert
```

---

### 22.7 Product Groups

```text
POST /v1/product-groups
GET /v1/product-groups
PATCH /v1/product-groups/{id}
DELETE /v1/product-groups/{id}
POST /v1/product-groups/{id}/items
DELETE /v1/product-groups/{id}/items/{item_id}
```

---

### 22.8 Competitors

```text
POST /v1/competitors
GET /v1/competitors
GET /v1/competitors/{id}
PATCH /v1/competitors/{id}
DELETE /v1/competitors/{id}
```

---

### 22.9 Matches

```text
POST /v1/matches
GET /v1/matches
GET /v1/matches/{id}
PATCH /v1/matches/{id}
DELETE /v1/matches/{id}
POST /v1/matches/bulk-upsert
```

Bulk match example:

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

### 22.10 Scrape Profiles

```text
POST /v1/scrape-profiles
GET /v1/scrape-profiles
GET /v1/scrape-profiles/{id}
PATCH /v1/scrape-profiles/{id}
DELETE /v1/scrape-profiles/{id}
```

---

### 22.11 Access Policies and Proxies

```text
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
```

---

### 22.12 Domain Strategy

```text
POST /v1/domain-strategies/discover/competitor/{competitor_id}
POST /v1/domain-strategies/discover/domain
GET /v1/domain-strategies
GET /v1/domain-strategies/{id}
PATCH /v1/domain-strategies/{id}
POST /v1/domain-strategies/{id}/rediscover
GET /v1/domain-strategies/{id}/stats
```

---

### 22.13 Refresh Rules

```text
POST /v1/refresh-rules
GET /v1/refresh-rules
GET /v1/refresh-rules/{id}
PATCH /v1/refresh-rules/{id}
DELETE /v1/refresh-rules/{id}
```

---

### 22.14 Run Jobs

```text
POST /v1/jobs/run/workspace
POST /v1/jobs/run/competitor/{competitor_id}
POST /v1/jobs/run/product/{product_id}
POST /v1/jobs/run/variant/{variant_id}
POST /v1/jobs/run/product-group/{product_group_id}
POST /v1/jobs/run/product/{product_id}/competitor/{competitor_id}
POST /v1/jobs/run/variant/{variant_id}/competitor/{competitor_id}
POST /v1/jobs/run/match/{match_id}
```

Response:

```json
{
  "job_id": "job_123",
  "status": "PENDING",
  "total_targets": 1
}
```

---

### 22.15 Job Status

```text
GET /v1/jobs
GET /v1/jobs/{id}
GET /v1/jobs/{id}/targets
GET /v1/jobs/{id}/results
GET /v1/jobs/{id}/alerts
GET /v1/jobs/{id}/attempts
```

Filters:

```text
status
scope
source
created_after
created_before
```

---

### 22.16 Results

```text
GET /v1/observations
GET /v1/matches/{match_id}/current-price
GET /v1/products/{product_id}/price-comparison
GET /v1/variants/{variant_id}/price-comparison
```

Observation filters:

```text
match_id
product_id
variant_id
competitor_id
success
error_code
scraped_after
scraped_before
```

---

### 22.17 Alerts

```text
GET /v1/alerts/current
GET /v1/alerts/current/{variant_id}
GET /v1/alert-events
PATCH /v1/alerts/current/{variant_id}
```

Filters:

```text
type
severity
status
product_id
variant_id
created_after
created_before
```

---

### 22.18 Webhooks and Events

```text
POST /v1/webhook-endpoints
GET /v1/webhook-endpoints
PATCH /v1/webhook-endpoints/{id}
DELETE /v1/webhook-endpoints/{id}

GET /v1/webhook-events
GET /v1/webhook-events/{id}
```

---

## 23. Job Flow

### 23.1 Run one match

```text
API request
↓
Validate workspace access
↓
Create ScrapeJob
↓
Create ScrapeJobTarget
↓
Enqueue Celery scrape_match task
↓
Acquire in-flight match lock
↓
Load match + variant + competitor
↓
Resolve scrape profile
↓
Resolve domain strategy
↓
Resolve access policy
↓
Acquire distributed domain rate-limit token
↓
Fetch URL using preferred access method
↓
Extractor starts from preferred extraction method
↓
Observation saved
↓
MatchCurrentPrice updated
↓
RequestAttempt saved
↓
Strategy stats updated atomically
↓
Price analysis runs
↓
VariantPriceState updated
↓
VariantAlertState created/updated
↓
PriceAlertEvent created if state changed
↓
WebhookEvent created
↓
Release lock
```

---

### 23.2 Run one variant

```text
API request with variant_id
↓
Find active matches for variant
↓
Create ScrapeJob
↓
Create unique ScrapeJobTargets
↓
Queue match-level tasks
↓
Analyze variant after tasks complete or after each successful update
```

---

### 23.3 Run one product

```text
API request with product_id
↓
Find active variants
↓
Find active matches for those variants
↓
Create ScrapeJob
↓
Queue match-level tasks
↓
Analyze affected variants
```

---

### 23.4 Run one competitor

```text
API request with competitor_id
↓
Find active matches for competitor
↓
Create ScrapeJob
↓
Queue match-level tasks
```

---

### 23.5 Run workspace

```text
API request or scheduled rule
↓
Find all active matches in workspace
↓
Create ScrapeJob
↓
Queue match-level tasks in batches
↓
Rate limiter spreads actual requests by domain
```

---

## 24. Celery Queues

Use separate queues:

```text
scrape_dispatch
scrape_http
scrape_browser
price_analysis
strategy_discovery
webhook_events
maintenance
```

### scrape_dispatch

Expands large jobs into match-level tasks.

Example:

```text
workspace job → 10,000–20,000 match tasks
competitor job → 2,000 match tasks
variant job → 5–10 match tasks
match job → 1 match task
```

### scrape_http

Runs HTTPX extraction for individual match URLs.

### scrape_browser

Runs Playwright extraction. Keep separate because browser tasks are heavier.

### price_analysis

Runs variant-level price comparison.

### strategy_discovery

Runs discovery jobs for competitor domains/templates.

### webhook_events

Creates/fetches events in v1. Automatic delivery later.

### maintenance

Cleanup, partition creation, rollups, recovery jobs.

---

## 25. Scheduler

Use a custom DB-driven scheduler enqueuer.

### 25.1 Why

Schedules are business configuration and should live in PostgreSQL.

The scheduler service only does:

```text
claim due refresh rules
create scrape job
enqueue Celery task
calculate next_run_at
```

### 25.2 Duplicate prevention

Use one of:

```text
PostgreSQL advisory lock
Redis lock: lock:scheduler:refresh-rules
```

The scheduler must be safe if two instances start accidentally.

### 25.3 Rule claiming

Use row locking:

```sql
SELECT *
FROM refresh_rules
WHERE enabled = true
  AND next_run_at <= now()
ORDER BY next_run_at
FOR UPDATE SKIP LOCKED;
```

Then update `locked_at`, enqueue, and calculate `next_run_at`.

---

## 26. Legal and Compliance Guardrails

This backend is for monitoring publicly available product pricing.

Do not support:

```text
login-required scraping
CAPTCHA solving
paywall bypass
private account scraping
credentialed competitor access
abuse of private APIs
```

Competitors should start with:

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
DISABLED or MANUAL_REVIEW
```

---

## 27. Observability and Operations

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
```

Celery result backend:

```text
disabled by default
```

Reason:

```text
scrape_jobs and scrape_job_targets are the source of truth
```

Optional tools:

```text
Flower for Celery monitoring
OpenTelemetry-compatible logs/metrics
Sentry or self-hosted error tracker if allowed
```

No external monitoring dependency is required for v1.

---

## 28. Workspace Isolation

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

### 28.1 Structural enforcement

Do not rely only on developer discipline.

Add:

```text
Workspace-scoped repository/query helpers
workspace dependency in every route
tests for cross-workspace access
```

### 28.2 Optional defense-in-depth

Add PostgreSQL Row-Level Security after the MVP stabilizes.

If using RLS, set workspace context per transaction:

```sql
SET LOCAL app.workspace_id = '<workspace_id>';
```

Policies should restrict rows by `workspace_id`.

---

## 29. Security and Secrets

### 29.1 API keys

```text
store only hash
show full key only once
support scopes
support revocation
track last_used_at
```

### 29.2 Refresh tokens

```text
store hashed refresh tokens
support logout/revocation
expire tokens
```

### 29.3 Encryption

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

## 30. Error Codes

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

These codes are important for:

```text
debugging
strategy optimizer
access policy tuning
rediscovery triggers
client reporting
```

---

## 31. Deployment

Railway v1 services:

```text
api-service
worker-http-service
worker-browser-service
scheduler-service
postgres
redis
```

### API service command

```bash
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

### HTTP worker command

```bash
celery -A app.workers.celery_app worker -Q scrape_dispatch,scrape_http,price_analysis,strategy_discovery,maintenance --loglevel=info
```

### Browser worker command

```bash
celery -A app.workers.celery_app worker -Q scrape_browser --loglevel=info --concurrency=1
```

### Scheduler service command

```bash
python -m app.scheduler.scheduler_app
```

---

## 32. Repository Structure

```text
price-monitor-backend/
  README.md
  PROJECT_SPEC.md
  pyproject.toml
  alembic.ini
  docker-compose.yml

  app/
    main.py

    core/
      config.py
      security.py
      workspace_context.py
      errors.py
      encryption.py
      ids.py
      pagination.py

    db/
      session.py
      base.py
      repositories/
        workspace_scoped.py
      models/
        workspace.py
        user.py
        refresh_token.py
        api_key.py
        product.py
        product_variant.py
        product_group.py
        competitor.py
        match.py
        scrape_profile.py
        proxy_provider.py
        access_policy.py
        domain_access_rule.py
        domain_strategy_profile.py
        strategy_attempt_stat.py
        strategy_discovery_run.py
        refresh_rule.py
        scrape_job.py
        scrape_job_target.py
        request_attempt.py
        observation.py
        current_price.py
        alert_state.py
        alert_event.py
        daily_rollup.py
        webhook.py

    api/
      deps.py
      routes/
        auth.py
        workspaces.py
        api_keys.py
        products.py
        variants.py
        product_groups.py
        competitors.py
        matches.py
        scrape_profiles.py
        access_policies.py
        proxy_providers.py
        domain_access_rules.py
        domain_strategies.py
        refresh_rules.py
        jobs.py
        observations.py
        alerts.py
        webhooks.py

    services/
      workspaces_service.py
      products_service.py
      variants_service.py
      competitors_service.py
      matches_service.py
      config_resolution_service.py
      access_policy_service.py
      rate_limiter_service.py
      lock_service.py
      domain_strategy_service.py
      scrape_jobs_service.py
      price_analysis_service.py
      alerts_service.py
      webhook_events_service.py

    workers/
      celery_app.py
      tasks/
        scrape_tasks.py
        browser_tasks.py
        analysis_tasks.py
        strategy_tasks.py
        webhook_tasks.py
        maintenance_tasks.py

    scheduler/
      scheduler_app.py
      scheduler_service.py

    extraction/
      fetchers/
        http_fetcher.py
        playwright_fetcher.py
      extractors/
        platform_extractor.py
        structured_data_extractor.py
        embedded_json_extractor.py
        selector_extractor.py
        xpath_extractor.py
        regex_extractor.py
        variant_extractor.py
      normalizers/
        price_normalizer.py
        currency_normalizer.py
        stock_normalizer.py
      strategies/
        extraction_pipeline.py
        access_pipeline.py
        url_pattern.py

  alembic/
    versions/

  tests/
    unit/
    integration/
```

---

## 33. Quick MVP Test

Do not build everything first.

Build a vertical slice:

```text
DB config
↓
API trigger
↓
Celery task
↓
HTTPX fetch
↓
parsel/extruct extraction
↓
Observation saved
↓
Current price updated
↓
Alert generated
↓
API returns result
```

Skip initially:

```text
dynamic scheduler
proxies
Playwright
webhook delivery
auto matching
frontend
full strategy optimizer
RLS
partition automation
```

### Minimal endpoints for quick test

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

### Demo seed

Create:

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

Use fixture HTML pages first instead of real websites.

This proves backend logic before dealing with blocking.

---

## 34. Claude Code Build Phases

### Phase 0 — Foundation and HTTP Stack Spike

Build:

```text
FastAPI app
config management
PostgreSQL connection
SQLAlchemy base
Alembic setup
Redis connection
Celery app skeleton
health endpoint
docker-compose
HTTPX fetcher spike
parsel selector spike
extruct structured-data spike
```

Deliverable:

```text
API boots
DB connects
Redis connects
Health endpoint works
Alembic migration works
Celery task can fetch fixture URL with HTTPX and extract price
```

This phase proves the non-Scrapy extraction engine before committing to the full backend.

---

### Phase 1 — Workspace, Auth, API Keys

Build:

```text
Workspace model
User model
JWT login
refresh token model
API key model
workspace dependency
scoped repository helpers
seed admin user and workspace
```

Deliverable:

```text
Protected routes resolve workspace context.
API keys can authenticate future plugin requests.
Refresh tokens can be revoked.
```

---

### Phase 2 — Products, Variants, Competitors, Matches

Build:

```text
Product CRUD
Variant CRUD
Product group CRUD
Competitor CRUD
Match CRUD
Bulk product+variant upsert
Bulk match upsert
unique constraints
URL normalization
URL pattern derivation
```

Deliverable:

```text
External plugin can send products, variants, and competitor URLs.
```

---

### Phase 3 — Scrape Profiles and Extraction Pipeline

Build:

```text
Scrape profile CRUD
config resolution service
structured data extractor
embedded JSON extractor
CSS selector extractor
XPath extractor
regex extractor
price normalization
currency normalization
validation rules
confidence scoring
```

Deliverable:

```text
Each match resolves final extraction config and extracts price from fixture pages.
```

---

### Phase 4 — Celery Manual Jobs, Current Price, Alerts

Build:

```text
Celery worker
Scrape job model
Scrape job target model
Run match endpoint
Run variant endpoint
Request attempt model
Price observation model
Match current price model
Variant price state model
Variant alert state model
Price alert event model
Job status tracking
```

Deliverable:

```text
POST /v1/jobs/run/match/{id} fetches URL, extracts price, saves observation, updates current price, calculates alert.
```

---

### Phase 5 — Distributed Rate Limiting and In-Flight Dedup

Build:

```text
Redis token bucket/sliding-window limiter
Redis domain semaphore
Redis match lock
requeue with delay + jitter
domain limits from DB
structured RATE_LIMITED and LOCKED_ALREADY_RUNNING handling
```

Deliverable:

```text
Multiple workers cannot exceed per-domain limits or scrape the same match concurrently.
```

This must happen before large-scale jobs.

---

### Phase 6 — Access Policies and Internal Proxies

Build:

```text
Proxy provider model
Access policy model
Domain access rule model
Direct HTTP
Direct retry
Proxy HTTP
retry policy
proxy rotation policy
request attempt logging
```

Deliverable:

```text
Scraper can use DIRECT_HTTP, DIRECT_HTTP_RETRY, and PROXY_HTTP according to DB policy.
```

---

### Phase 7 — Domain Strategy Optimizer v1

Build:

```text
Domain strategy profile model
Strategy attempt stats model
Strategy discovery run model
Discovery job for 3–10 sample URLs
Preferred access method learning
Preferred extraction method learning
Promotion after 3 confirmations
Atomic stats updates
Rediscovery trigger flags
Periodic light re-check mechanism
```

Deliverable:

```text
New competitor domain can run discovery mode and future scrapes start from learned methods.
```

---

### Phase 8 — Dynamic Scheduler

Build:

```text
Refresh rule CRUD
DB-driven scheduler service
next_run_at calculation
safe claiming with lock/SKIP LOCKED
workspace schedules
competitor schedules
product group schedules
variant schedules
match schedules
```

Deliverable:

```text
Workspace can run daily and priority groups can run hourly.
```

---

### Phase 9 — Internal Playwright Fallback

Build:

```text
Playwright Python integration
BROWSER mode scrape profile
PLAYWRIGHT_PROXY access method
wait_for_selector support
browser worker queue separation
variant selection with Playwright when configured
resource limits for browser tasks
```

Deliverable:

```text
JS-rendered competitor pages can be scraped internally without external APIs.
```

---

### Phase 10 — Partitioning, Retention, Rollups

Build:

```text
monthly partitions
partition creation job
retention job
daily rollup job
indexes per partition
maintenance API/status
```

Deliverable:

```text
Append-heavy tables stay manageable as observations grow.
```

---

### Phase 11 — Webhook/Event Readiness

Build:

```text
Webhook endpoint model
Webhook event model
event creation on alerts/jobs/strategy updates
event fetch API
pagination and retention
```

Do not build automatic webhook delivery yet unless needed.

Deliverable:

```text
External systems can poll events and alerts.
```

---

### Phase 12 — RLS Defense-in-Depth

Optional but recommended after MVP stability.

Build:

```text
PostgreSQL RLS policies
SET LOCAL app.workspace_id per transaction
tests proving cross-workspace reads/writes fail
```

Deliverable:

```text
Workspace isolation is enforced both in app code and database policy.
```

---

## 35. What Not To Build in V1

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

## 36. V1 Success Criteria

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
10. Extract competitor prices using configured profiles.
11. Save request attempts.
12. Save observations.
13. Update match current price.
14. Update variant current comparison state.
15. Generate variant-level current alert state.
16. Store alert event history.
17. Expose comparison results by API.
18. Enforce distributed per-domain rate limits.
19. Prevent duplicate in-flight match scrapes.
20. Use DB-driven access policies.
21. Learn preferred access/extraction strategy for a new domain.
22. Start future scrapes from learned strategy instead of trying everything.
23. Run daily and hourly schedules.
24. Keep append-heavy tables manageable with partitioning/retention.
25. Be ready for a future WooCommerce/Salla plugin.
```

---

## 37. Recommended Pilot

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

## 38. Final Implementation Notes

Build this as a configurable price extraction backend, not as a hardcoded scraper.

Core idea:

```text
Products/variants define what the client sells.
Competitor matches define where to fetch.
Scrape profiles define how to extract.
Access policies define how to access.
Domain strategy profiles learn what works.
Rate limiter controls domain pressure.
Jobs execute scrape requests.
Observations store history.
Current price tables make analysis fast.
Alert states explain pricing position.
Events expose changes for future integrations.
```

The most important parts to get right from day one:

```text
workspace isolation
variant-level pricing
match-level competitor URLs
DB-driven configuration
HTTPX/parsel/extruct extraction pipeline
structured request attempts
structured extraction results
confidence scoring
distributed domain rate limiting
in-flight deduplication
current price denormalization
clear alert decision tree
domain strategy optimizer
API-first job control
```
