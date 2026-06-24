# Price Monitoring Backend — Python Architecture Spec

## 1. Goal

Build an internal-first, SaaS-ready backend for competitor price monitoring.

The system will monitor client product prices against competitor product URLs.

V1 is not a frontend product. It is an API-first backend that can later be controlled by a WooCommerce plugin, Salla integration, n8n workflow, Google Sheet automation, or future dashboard.

## 2. Final Stack

```text
API: FastAPI
Database: PostgreSQL
ORM: SQLAlchemy
Migrations: Alembic
Queue: Celery
Broker: Redis
Scheduler: APScheduler
Scraping: Scrapy
JS fallback: scrapy-playwright
Deployment: Railway first
Language: Python
```

## 3. Core Principles

1. API-first.
2. Workspace isolation from day one.
3. Manual product matching now.
4. Automatic product matching later.
5. Generic scraping profiles first.
6. Custom spiders/adapters only when needed.
7. Scrapy HTTP-first.
8. Playwright only as fallback.
9. Store extracted results only.
10. No raw HTML or screenshots in v1.
11. No auto-repricing in v1.
12. Webhook/event-ready, but webhook delivery can come later.

## 4. High-Level Architecture

```text
External Systems
WooCommerce Plugin / Salla / n8n / Admin Scripts / Future Dashboard
        |
        v
FastAPI Backend
Auth / Workspaces / Products / Competitors / Matches / Jobs / Alerts / Events
        |
        +--------------------+
        |                    |
        v                    v
PostgreSQL              Redis
Config + Results        Celery Broker
        |                    |
        v                    v
APScheduler             Celery Workers
Dynamic schedules       Scrapy jobs
                             |
                             v
                    Competitor Websites
                    Direct / Proxy / Playwright
```

## 5. Main Services

### 5.1 API Service

FastAPI app responsible for:

* Authentication.
* API keys.
* Workspace isolation.
* Product management.
* Competitor management.
* Competitor product URL matching.
* Scrape profile management.
* Proxy policy management.
* Refresh rule management.
* Manual scrape job triggering.
* Job status.
* Results API.
* Alerts API.
* Webhook/event API.

The API does not scrape directly. It creates jobs.

### 5.2 Worker Service

Celery workers responsible for:

* Running scrape jobs.
* Running Scrapy spiders.
* Saving observations.
* Running price analysis.
* Creating alerts.
* Creating webhook events.
* Retrying failed jobs.

### 5.3 Scheduler Service

APScheduler service responsible for:

* Reading refresh rules from the database.
* Scheduling dynamic jobs.
* Supporting workspace-level, competitor-level, product-level, group-level, and match-level refreshes.
* Enqueuing Celery tasks when schedules are due.

### 5.4 Scraper Engine

Scrapy project responsible for:

* Fetching competitor product pages.
* Applying scraping profile configuration.
* Extracting price, old price, currency, title, and stock status.
* Normalizing price data.
* Returning structured scrape results.
* Falling back to Playwright only when needed.

## 6. Repository Structure

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

    db/
      session.py
      base.py
      models/
        workspace.py
        user.py
        api_key.py
        product.py
        competitor.py
        match.py
        scrape_profile.py
        proxy_policy.py
        refresh_rule.py
        scrape_job.py
        observation.py
        alert.py
        webhook.py

    api/
      deps.py
      routes/
        auth.py
        workspaces.py
        api_keys.py
        products.py
        competitors.py
        matches.py
        scrape_profiles.py
        proxy_policies.py
        refresh_rules.py
        jobs.py
        observations.py
        alerts.py
        webhooks.py

    services/
      workspaces_service.py
      products_service.py
      competitors_service.py
      matches_service.py
      config_resolution_service.py
      scrape_jobs_service.py
      price_analysis_service.py
      alerts_service.py
      webhook_events_service.py

    workers/
      celery_app.py
      tasks/
        scrape_tasks.py
        analysis_tasks.py
        webhook_tasks.py

    scheduler/
      scheduler_app.py
      scheduler_service.py

    scraper/
      scrapy.cfg
      price_monitor/
        settings.py
        items.py
        pipelines.py
        middlewares.py

        spiders/
          generic_price_spider.py
          shopify_spider.py
          woocommerce_spider.py

        extractors/
          jsonld_extractor.py
          selector_extractor.py
          regex_extractor.py
          price_normalizer.py
          stock_normalizer.py

        adapters/
          default_adapter.py
          shopify_adapter.py
          woocommerce_adapter.py
          playwright_adapter.py

  alembic/
    versions/

  tests/
    unit/
    integration/
```

## 7. Database Models

### 7.1 Workspace

Represents a client or tenant.

```text
Workspace
- id
- name
- slug
- status
- created_at
- updated_at
```

### 7.2 User

Internal users for now.

```text
User
- id
- workspace_id nullable
- email
- password_hash
- role
- status
- created_at
- updated_at
```

Roles:

```text
SUPER_ADMIN
WORKSPACE_ADMIN
READ_ONLY
```

### 7.3 API Key

Used later by WooCommerce plugin, Salla connector, n8n, or custom integrations.

```text
ApiKey
- id
- workspace_id
- name
- key_hash
- scopes
- status
- last_used_at
- created_at
- revoked_at
```

Example scopes:

```text
products:read
products:write
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

### 7.4 Product

Client’s own product.

```text
Product
- id
- workspace_id
- external_id
- sku
- title
- brand
- barcode
- url
- current_price
- currency
- product_group_id nullable
- status
- created_at
- updated_at
```

### 7.5 Product Group

Used for hourly priority products or specific campaign groups.

```text
ProductGroup
- id
- workspace_id
- name
- description
- status
- created_at
- updated_at
```

### 7.6 Competitor

A competitor website.

```text
Competitor
- id
- workspace_id
- name
- domain
- status
- default_scrape_profile_id nullable
- default_proxy_policy_id nullable
- max_concurrent_requests
- max_requests_per_minute
- created_at
- updated_at
```

### 7.7 Competitor Product Match

Links one client product to one competitor product URL.

There is no fixed limit.

```text
CompetitorProductMatch
- id
- workspace_id
- product_id
- competitor_id
- competitor_url
- external_sku nullable
- external_title nullable
- scrape_profile_id nullable
- proxy_policy_id nullable
- priority
- status
- last_scraped_at nullable
- last_success_at nullable
- last_failed_at nullable
- created_at
- updated_at
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

### 7.8 Scrape Profile

Defines how to scrape a competitor page.

```text
ScrapeProfile
- id
- workspace_id nullable
- name
- mode
- adapter_key
- price_selector nullable
- old_price_selector nullable
- stock_selector nullable
- title_selector nullable
- currency_selector nullable
- jsonld_enabled
- meta_enabled
- regex_rules json nullable
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
API
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

### 7.9 Proxy Provider

```text
ProxyProvider
- id
- workspace_id nullable
- name
- type
- base_url nullable
- username nullable
- password_encrypted nullable
- status
- created_at
- updated_at
```

Types:

```text
NONE
DATACENTER
RESIDENTIAL
MOBILE
SCRAPING_API
```

### 7.10 Proxy Policy

Defines when and how to use proxies.

```text
ProxyPolicy
- id
- workspace_id nullable
- name
- provider_id nullable
- strategy
- use_proxy_on_first_attempt
- use_proxy_on_retry
- max_retries
- rotate_per_request
- sticky_session
- country_code nullable
- max_requests_per_minute nullable
- max_requests_per_hour nullable
- max_requests_per_day nullable
- created_at
- updated_at
```

Strategies:

```text
NONE
DIRECT_FIRST
PROXY_ON_RETRY
PROXY_FIRST
RESIDENTIAL_ONLY
SCRAPING_API
```

### 7.11 Refresh Rule

Dynamic schedule configuration.

```text
RefreshRule
- id
- workspace_id
- name
- scope
- product_id nullable
- product_group_id nullable
- competitor_id nullable
- match_id nullable
- cron_expression nullable
- interval_minutes nullable
- priority
- enabled
- created_at
- updated_at
```

Scopes:

```text
WORKSPACE
COMPETITOR
PRODUCT
PRODUCT_GROUP
MATCH
```

Examples:

```text
Daily workspace refresh:
scope = WORKSPACE
cron_expression = "0 3 * * *"

Hourly priority products:
scope = PRODUCT_GROUP
interval_minutes = 60

Daily competitor refresh:
scope = COMPETITOR
cron_expression = "0 4 * * *"
```

### 7.12 Scrape Job

Represents a high-level scrape request.

```text
ScrapeJob
- id
- workspace_id
- type
- scope
- product_id nullable
- product_group_id nullable
- competitor_id nullable
- match_id nullable
- status
- priority
- total_targets
- success_count
- failure_count
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

### 7.13 Price Observation

One extracted result from one competitor product URL.

```text
PriceObservation
- id
- workspace_id
- match_id
- scrape_job_id nullable
- price nullable
- old_price nullable
- currency nullable
- stock_status nullable
- raw_title nullable
- success
- error_code nullable
- error_message nullable
- scraped_at
```

Stock statuses:

```text
IN_STOCK
OUT_OF_STOCK
UNKNOWN
```

### 7.14 Price Alert

Generated after comparing client price with competitor prices.

```text
PriceAlert
- id
- workspace_id
- product_id
- scrape_job_id nullable
- type
- severity
- client_price
- benchmark_price nullable
- cheapest_competitor_price nullable
- average_competitor_price nullable
- message
- details json nullable
- status
- created_at
- resolved_at nullable
```

Alert types:

```text
NORMAL
CLOSE_TO_COMPETITORS
CHANCE_TO_INCREASE_PRICE
HIGH_PRICE
RISK
NO_COMPETITOR_DATA
```

Severity:

```text
NONE
LOW
MEDIUM
HIGH
CRITICAL
```

### 7.15 Webhook Endpoint

Not used heavily in v1, but must be ready.

```text
WebhookEndpoint
- id
- workspace_id
- name
- url
- secret_encrypted nullable
- enabled
- event_types
- created_at
- updated_at
```

### 7.16 Webhook Event

Events can be fetched by API first. Automatic delivery can come later.

```text
WebhookEvent
- id
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
```

## 8. Workspace Isolation Rules

Every workspace-owned model must include `workspace_id`.

All queries must be scoped by workspace.

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

Create shared helper methods for scoped queries.

API key authentication must resolve the workspace automatically.

JWT authentication must resolve allowed workspaces.

## 9. Scrape Config Resolution

Final scrape configuration should resolve in this order:

```text
Match-level override
↓
Competitor-level default
↓
Workspace-level default
↓
Global default
```

Same for proxy policies.

This allows one competitor to use default HTML extraction while one difficult URL uses Playwright or residential proxy.

## 10. Scraping Strategy

Default flow for one competitor product URL:

```text
Load match
Load product
Resolve scrape profile
Resolve proxy policy
Try HTTP request with Scrapy
Try JSON-LD extraction
Try meta extraction
Try CSS selector extraction
Try regex extraction
Normalize price
Validate price
Save observation
Run price analysis
```

Fallback flow:

```text
If HTTP fails:
  retry direct based on retry policy

If blocked or timeout:
  retry with proxy if allowed

If price still missing and profile allows browser:
  retry with scrapy-playwright

If still failed:
  save failed observation with structured error
```

Do not use Playwright by default.

## 11. Generic Spider Design

Use one generic spider first:

```text
generic_price_spider
```

Input:

```text
scrape_job_id
workspace_id
match_ids
```

Responsibilities:

* Load matches from DB.
* Resolve scrape config per match.
* Build Scrapy requests.
* Extract product price.
* Send item to pipeline.
* Save observations.
* Report success/failure.

Later add custom spiders:

```text
shopify_spider
woocommerce_spider
custom_domain_spider
```

Do not add custom spiders unless the generic spider cannot handle the site cleanly.

## 12. Extractors

Extractor order:

```text
1. JSON-LD extractor
2. Ecommerce platform extractor
3. CSS selector extractor
4. Regex extractor
5. Playwright-rendered selector extractor
```

### JSON-LD Extractor

Looks for:

```text
Product
Offer
offers.price
offers.priceCurrency
availability
```

### Shopify Extractor

Try common Shopify patterns:

```text
/products/<handle>.json
embedded product JSON
variant price
```

### WooCommerce Extractor

Try common WooCommerce patterns:

```text
wp-json/wc/store/products
structured product data
HTML selectors
```

### Selector Extractor

Uses DB-configured selectors:

```text
price_selector
old_price_selector
stock_selector
title_selector
currency_selector
```

### Regex Extractor

Useful when price is inside scripts or custom markup.

## 13. Price Analysis Rules

For each client product:

```text
client_price = product.current_price
competitor_prices = latest successful active competitor prices
cheapest_competitor_price = min(competitor_prices)
highest_competitor_price = max(competitor_prices)
average_competitor_price = average(competitor_prices)
```

Difference formula:

```text
difference_percent = ((client_price - benchmark_price) / benchmark_price) * 100
```

Positive difference means client is more expensive.

Negative difference means client is cheaper.

## 14. Alert Logic

The normal target state:

```text
Client price is 1% to 5% lower than competitors.
```

### Risk

If client price is higher than all competitor prices:

```text
Type: RISK
Severity: CRITICAL
Message: Risk: your price is higher than all competitors.
```

### High Price

If client price is higher than at least one competitor:

```text
Type: HIGH_PRICE
Severity: HIGH
Message: High price: at least one competitor is cheaper.
```

### Close to Competitors

If client price is 0% to 1% lower than benchmark, equal, or almost equal:

```text
Type: CLOSE_TO_COMPETITORS
Severity: MEDIUM
Message: Close to competitors: price is nearly the same as competitors.
```

### Normal

If client price is 1% to 5% lower than benchmark:

```text
Type: NORMAL
Severity: NONE
Message: Normal: price is within target range.
```

### Chance to Increase Price

If client price is more than 5% lower than benchmark:

```text
Type: CHANCE_TO_INCREASE_PRICE
Severity: MEDIUM
Message: Chance to increase the price: your price is much lower than competitors.
```

### No Competitor Data

If no valid competitor price exists:

```text
Type: NO_COMPETITOR_DATA
Severity: LOW
Message: No valid competitor price data available.
```

## 15. Benchmark Strategy

Use this default:

```text
For HIGH_PRICE and RISK:
Compare against all individual competitor prices and cheapest competitor price.

For CLOSE_TO_COMPETITORS, NORMAL, and CHANCE_TO_INCREASE_PRICE:
Compare against average competitor price.
```

Later make this configurable per workspace:

```text
CHEAPEST
AVERAGE
MEDIAN
SELECTED_COMPETITORS_ONLY
```

## 16. API Endpoints

Base path:

```text
/v1
```

### Auth

```text
POST /v1/auth/login
POST /v1/auth/refresh
```

### API Keys

```text
POST /v1/api-keys
GET /v1/api-keys
DELETE /v1/api-keys/{id}
```

### Products

```text
POST /v1/products
GET /v1/products
GET /v1/products/{id}
PATCH /v1/products/{id}
DELETE /v1/products/{id}

POST /v1/products/bulk-upsert
```

### Product Groups

```text
POST /v1/product-groups
GET /v1/product-groups
PATCH /v1/product-groups/{id}
DELETE /v1/product-groups/{id}
POST /v1/product-groups/{id}/products
DELETE /v1/product-groups/{id}/products/{product_id}
```

### Competitors

```text
POST /v1/competitors
GET /v1/competitors
GET /v1/competitors/{id}
PATCH /v1/competitors/{id}
DELETE /v1/competitors/{id}
```

### Matches

```text
POST /v1/matches
GET /v1/matches
GET /v1/matches/{id}
PATCH /v1/matches/{id}
DELETE /v1/matches/{id}

POST /v1/matches/bulk-upsert
```

### Scrape Profiles

```text
POST /v1/scrape-profiles
GET /v1/scrape-profiles
GET /v1/scrape-profiles/{id}
PATCH /v1/scrape-profiles/{id}
DELETE /v1/scrape-profiles/{id}
```

### Proxy Policies

```text
POST /v1/proxy-policies
GET /v1/proxy-policies
PATCH /v1/proxy-policies/{id}
DELETE /v1/proxy-policies/{id}
```

### Refresh Rules

```text
POST /v1/refresh-rules
GET /v1/refresh-rules
GET /v1/refresh-rules/{id}
PATCH /v1/refresh-rules/{id}
DELETE /v1/refresh-rules/{id}
```

### Run Jobs

```text
POST /v1/jobs/run/workspace
POST /v1/jobs/run/competitor/{competitor_id}
POST /v1/jobs/run/product/{product_id}
POST /v1/jobs/run/product/{product_id}/competitor/{competitor_id}
POST /v1/jobs/run/match/{match_id}
```

### Job Status

```text
GET /v1/jobs
GET /v1/jobs/{id}
GET /v1/jobs/{id}/results
GET /v1/jobs/{id}/alerts
```

### Observations

```text
GET /v1/observations
GET /v1/products/{product_id}/price-comparison
```

### Alerts

```text
GET /v1/alerts
GET /v1/alerts/{id}
PATCH /v1/alerts/{id}
```

### Webhooks and Events

```text
POST /v1/webhook-endpoints
GET /v1/webhook-endpoints
PATCH /v1/webhook-endpoints/{id}
DELETE /v1/webhook-endpoints/{id}

GET /v1/webhook-events
GET /v1/webhook-events/{id}
```

## 17. Job Flow

### Run One Match

```text
API request
↓
Validate workspace access
↓
Create ScrapeJob
↓
Create Celery task with match_id
↓
Scrapy fetches URL
↓
Observation saved
↓
Price analysis runs
↓
Alert created/updated
↓
Webhook event created
```

### Run One Product

```text
API request with product_id
↓
Find all active matches for product
↓
Create ScrapeJob
↓
Create Celery tasks for each match
↓
Run observations
↓
Analyze product once all matches finish
```

### Run Competitor

```text
API request with competitor_id
↓
Find all active matches for competitor
↓
Create ScrapeJob
↓
Queue match-level tasks
```

### Run Workspace

```text
API request or scheduled rule
↓
Find all active matches in workspace
↓
Create ScrapeJob
↓
Queue match-level tasks in batches
```

## 18. Celery Queues

Use separate queues:

```text
scrape_dispatch
scrape_match
price_analysis
webhook_events
maintenance
```

### scrape_dispatch

Expands large jobs into match-level jobs.

Example:

```text
workspace job → 20,000 match jobs
competitor job → 2,000 match jobs
product job → 5-10 match jobs
match job → 1 match job
```

### scrape_match

Runs Scrapy for individual match URLs.

### price_analysis

Runs product-level price comparison.

### webhook_events

Stores and later sends webhook events.

### maintenance

Cleanup and recovery jobs.

## 19. Scheduling

Use APScheduler.

Do not hardcode schedules in code.

Refresh rules live in the database.

Scheduler service:

```text
Starts
↓
Loads enabled refresh rules
↓
Registers jobs
↓
Watches for changes or reloads periodically
↓
Enqueues Celery tasks when due
```

Dynamic refresh examples:

```text
Workspace daily:
scope = WORKSPACE
cron = "0 3 * * *"

Priority products hourly:
scope = PRODUCT_GROUP
interval_minutes = 60

Specific competitor twice daily:
scope = COMPETITOR
cron = "0 6,18 * * *"

Single match hourly:
scope = MATCH
interval_minutes = 60
```

## 20. Rate Limits

Rate limits should be configurable at:

```text
Global level
Workspace level
Competitor level
Domain level
Proxy policy level
```

Recommended v1 defaults:

```text
Per competitor:
max concurrent requests = 2
max requests per minute = 30
retry count = 2
retry delay = exponential
```

Do not scrape one domain too aggressively.

## 21. Proxy Strategy

Default strategy:

```text
Direct first
Proxy on retry
Playwright only if needed
```

Examples:

### No Proxy

```text
strategy = NONE
```

### Direct First, Proxy on Retry

```text
strategy = PROXY_ON_RETRY
use_proxy_on_first_attempt = false
use_proxy_on_retry = true
```

### Residential Always

```text
strategy = RESIDENTIAL_ONLY
use_proxy_on_first_attempt = true
rotate_per_request = true
```

### Scraping API

For very difficult sites:

```text
strategy = SCRAPING_API
```

## 22. Error Codes

Use structured error codes:

```text
HTTP_403
HTTP_404
HTTP_429
TIMEOUT
DNS_ERROR
PRICE_NOT_FOUND
INVALID_PRICE_FORMAT
CURRENCY_NOT_FOUND
STOCK_NOT_FOUND
BLOCKED
PROXY_FAILED
PLAYWRIGHT_FAILED
UNKNOWN_ERROR
```

These will help decide which competitors need proxies, selectors, or Playwright.

## 23. Deployment

Railway v1 services:

```text
api-service
worker-service
scheduler-service
postgres
redis
```

### API Service

Runs:

```text
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

### Worker Service

Runs:

```text
celery -A app.workers.celery_app worker --loglevel=info
```

### Scheduler Service

Runs:

```text
python -m app.scheduler.scheduler_app
```

Later split workers:

```text
worker-http
worker-playwright
worker-analysis
worker-webhook
```

Keep Playwright workers separate later because browser rendering consumes more CPU and memory.

## 24. Claude Code Build Phases

### Phase 0 — Foundation

Build:

* FastAPI app.
* Config management.
* PostgreSQL connection.
* SQLAlchemy base.
* Alembic setup.
* Redis connection.
* Health endpoint.
* Basic project structure.

Deliverable:

```text
API boots
DB connects
Redis connects
Health endpoint works
Alembic migration works
```

### Phase 1 — Auth and Workspace Isolation

Build:

* Workspace model.
* User model.
* JWT login.
* API key model.
* Workspace dependency.
* Scoped query helpers.
* Seed admin user and workspace.

Deliverable:

```text
Protected API routes resolve workspace context.
API keys can authenticate requests.
```

### Phase 2 — Products, Competitors, Matches

Build:

* Product CRUD.
* Product group CRUD.
* Competitor CRUD.
* Match CRUD.
* Bulk product upsert.
* Bulk match upsert.

Deliverable:

```text
External plugin can send products and competitor URLs.
```

### Phase 3 — Scrape Profiles and Proxy Policies

Build:

* Scrape profile CRUD.
* Proxy provider CRUD.
* Proxy policy CRUD.
* Config resolution service.

Deliverable:

```text
Each match resolves final scrape config.
```

### Phase 4 — Celery and Manual Jobs

Build:

* Celery app.
* Redis broker.
* Scrape job model.
* Job run APIs.
* Dispatch logic.
* Match-level tasks.

Deliverable:

```text
POST /v1/jobs/run/match/{id} creates and runs a job.
```

### Phase 5 — Scrapy Engine v1

Build:

* Scrapy project.
* Generic price spider.
* JSON-LD extractor.
* CSS selector extractor.
* Regex extractor.
* Price normalizer.
* Observation pipeline.

Deliverable:

```text
Known competitor product URLs can be scraped and saved.
```

### Phase 6 — Price Analysis and Alerts

Build:

* Latest observation resolver.
* Price comparison service.
* Alert generation.
* Alert API.

Deliverable:

```text
After scraping a product, alert status is calculated.
```

### Phase 7 — Dynamic Scheduler

Build:

* Refresh rules.
* APScheduler service.
* DB-driven schedules.
* Daily/hourly/manual support.

Deliverable:

```text
Workspace daily refresh and hourly product group refresh work.
```

### Phase 8 — Event/Webhook Readiness

Build:

* Webhook endpoint model.
* Webhook event model.
* Event creation.
* Event fetch API.

Deliverable:

```text
External systems can poll alert/job events.
```

### Phase 9 — Proxy and Playwright Fallback

Build:

* Proxy policy integration.
* Retry with proxy.
* scrapy-playwright integration.
* Browser-only scrape profile.
* Domain-specific config.

Deliverable:

```text
Difficult competitor URLs can be handled selectively.
```

### Phase 10 — Future Integrations

Later:

* WooCommerce plugin.
* Salla integration.
* Auto product matching.
* Suggested price rules.
* Auto price update approval flow.
* Admin dashboard.
* Webhook delivery with retries.
* SaaS billing and plans.

## 25. What Not To Build in v1

Do not build:

* Full dashboard.
* Billing.
* Auto product matching.
* Auto repricing.
* Raw HTML storage.
* Screenshot storage.
* Complex user roles.
* AI-based matching.
* Playwright-first scraping.
* Heavy marketplace crawling.

## 26. V1 Success Criteria

The backend is successful when it can:

1. Store multiple workspaces.
2. Store 2,000 products per workspace.
3. Store 10,000–20,000 competitor product URLs.
4. Run one match manually.
5. Run one product manually.
6. Run one competitor manually.
7. Run one full workspace manually.
8. Run daily workspace schedules.
9. Run hourly priority product group schedules.
10. Extract competitor prices.
11. Save observations.
12. Generate alerts.
13. Expose results by API.
14. Prepare for a future WooCommerce plugin.

## 27. Recommended First Pilot

Start with:

```text
1 workspace
100 products
5 competitors
500 competitor URLs
Daily refresh
Manual product refresh
Manual match refresh
Basic alert logic
```

Then scale to:

```text
2,000 products
5-10 competitors
10,000-20,000 competitor URLs
```

## 28. Final Recommendation

Build this as:

```text
FastAPI API
PostgreSQL database
SQLAlchemy ORM
Alembic migrations
Celery workers
Redis broker
APScheduler dynamic scheduler
Scrapy generic spider
scrapy-playwright fallback
Railway deployment
```

The most important parts to get right from day one:

```text
Workspace isolation
Scrape config inheritance
Match-level competitor URLs
Job status tracking
Structured errors
Price alert logic
API-first design
```
