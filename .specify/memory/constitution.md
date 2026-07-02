<!--
SYNC IMPACT REPORT
==================
Version change: (template, unversioned) → 1.0.0
Bump rationale: Initial ratification. First concrete constitution derived from
  PROJECT_SPEC.md (Price Monitoring Backend, 41 sections). MAJOR baseline.

Principles defined (8):
  I.    API-First, Service-Oriented Architecture
  II.   Workspace Isolation (NON-NEGOTIABLE)
  III.  Variant-Level Pricing & Explicit Matching
  IV.   Database-Driven Configuration
  V.    Disciplined Scraping Runtime (NON-NEGOTIABLE)
  VI.   Internal-Only & Legally Compliant Access (NON-NEGOTIABLE)
  VII.  Monetary & Extraction Correctness
  VIII. Scale-Safe Data & Concurrency

Added sections:
  - Technology & Security Constraints (stack lock-in, secrets, error codes)
  - Development Workflow & Quality Gates (Spec Kit phases, testing, observability)
  - Governance

Removed sections: none (template placeholders replaced).

Templates requiring updates:
  ✅ .specify/templates/plan-template.md — Constitution Check reads file
       dynamically ([Gates determined based on constitution file]); no edit needed.
  ✅ .specify/templates/spec-template.md — no constitution coupling; no edit needed.
  ✅ .specify/templates/tasks-template.md — no constitution coupling; no edit needed.

Follow-up TODOs: none. RATIFICATION_DATE set to project start (2026-06-27).

Source of truth: /srv/crawmatic/PROJECT_SPEC.md (sections referenced inline as §N).
-->

# Crawmatic Constitution

Crawmatic is an internal-first, SaaS-ready backend for competitor price monitoring.
It monitors a client store's products and variants against manually matched competitor
product URLs, and is API-first so future integrations (WooCommerce plugin, Salla, n8n,
admin scripts, dashboards) can drive it. This constitution encodes the non-negotiable
rules every feature, spec, plan, and implementation MUST honor. It is derived from and
subordinate to `PROJECT_SPEC.md`; where this document summarizes, the spec governs detail.

## Core Principles

### I. API-First, Service-Oriented Architecture

The system is API-first with no frontend in v1. It MUST be built as one monorepo that
deploys as distinct services: `api-service`, `scheduler-service`, `worker-service`,
`scrapyd-http-service`, `scrapyd-browser-service`, plus `pgbouncer`, `postgres`, and
`redis` (§4, §5). Responsibilities MUST stay separated: FastAPI and Celery orchestrate;
Scrapyd/Scrapy scrape; the scheduler claims due work; the database is the source of truth.
Only `api-service` is publicly exposed — Scrapyd and internal services MUST NOT be reachable
from the public internet (§6). Code shared by both Scrapy projects (extraction, item models,
validation, confidence scoring, DB pipelines, rate limiter) MUST live in `libs/shared` and be
imported by both, so the HTTP and browser projects differ only in download handler and
spider entrypoint (§5).

Rationale: a clean service boundary is what lets the project scale horizontally, expose a
stable contract to future integrations, and keep scraping concerns out of the API.

### II. Workspace Isolation (NON-NEGOTIABLE)

Every workspace-owned entity MUST carry `workspace_id`, and no query may fetch a
workspace-owned row by primary key alone — the `workspace_id` filter MUST be present on every
read and write (§32). Workspace scoping MUST be enforced structurally: workspace-scoped
repository/query helpers, a workspace dependency on every route, and tests that prove
cross-workspace reads and writes are blocked. RLS (`SET LOCAL app.workspace_id` inside the
query transaction) is the planned defense-in-depth and MUST remain compatible with PgBouncer
transaction pooling.

Rationale: this is a multi-tenant system; a single missing `workspace_id` predicate is a data
breach. Isolation is structural, not advisory.

### III. Variant-Level Pricing & Explicit Matching

Pricing is variant-level. Products are parent catalog items; variants are the sellable, priced
units (§10). Every product — including simple products — MUST have at least one default
variant. A competitor match (`CompetitorProductMatch`) is exactly one competitor URL linked to
exactly one product variant; a variant MAY have unlimited matches. Price observations, current
prices, comparison state, and alerts are all computed and stored at the variant level (§22, §23).

Rationale: consistency of pricing, matching, and alerting depends on a single uniform unit —
the variant — even when a product appears "simple."

### IV. Database-Driven Configuration

The code is the engine; the database controls behavior. What to scrape, how to scrape, how
often, with what access policy, and at what thresholds MUST be DB-configurable, not hardcoded
(§9, §41): scrape profiles, access policies, proxy providers, domain access rules, domain
strategy profiles, refresh rules, validation rules, and confidence/alert thresholds. Profile
and access-policy resolution chains MUST be batch-resolved and cached (Redis, short TTL keyed
by workspace/competitor/url_pattern) — never walked per match with separate queries, which is
N+1 amplification at 10k–20k matches per refresh (§9).

Rationale: a configurable backend adapts to new competitors and templates without code changes;
per-match resolution queries do not survive production scale.

### V. Disciplined Scraping Runtime (NON-NEGOTIABLE)

Scrapy is the scraping engine and MUST run under Scrapyd-managed services, never started inside
Celery task processes (§7, §8). The runtime rules are non-negotiable:

- **Spiders persist only.** A spider fetches, extracts, validates, and writes observations,
  request attempts, current prices, and strategy stats — then stops. Price analysis,
  alert-state transitions, and webhook emission run as a separate, idempotent `price_analysis`
  Celery task, one per affected variant, deduplicated per variant per job (§8, §26).
- **Reactor safety.** All DB and rate-limiter access inside spiders MUST be non-blocking —
  async driver bound to the Twisted reactor or `deferToThread`. No synchronous commits, no
  `time.sleep`, no blocking Redis round-trips on the reactor thread (§8, §12).
- **Idempotent dispatch.** Celery is at-least-once; every Scrapyd `schedule.json` call MUST be
  guarded (stable dispatch key, Redis `SET NX` and/or persisted Scrapyd `jobid`) so a retried
  dispatch never runs the same batch twice (§8).
- **Browser is a selective fallback**, never the default; keep browser concurrency low (§8, §14).

Rationale: blocking the reactor or double-dispatching destroys throughput and corrupts counts;
keeping analysis out of the spider is what keeps scraping fast and re-runnable.

### VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE)

Scraping uses internal access methods only — `DIRECT_HTTP`, `DIRECT_HTTP_RETRY`, `PROXY_HTTP`,
`PLAYWRIGHT_PROXY` (§11). External scraping/unlocker APIs (ScrapingBee, Zyte API, Bright Data
Unlocker, and similar) MUST NOT be used in v1. Only public product pages are scraped: no
login-required scraping, CAPTCHA solving, paywall bypass, private-account or credentialed
competitor access, or private-API abuse (§30). Competitors begin at `legal_status =
REVIEW_REQUIRED` and MUST be reviewed/approved before production scraping; heavily-blocking or
login/CAPTCHA-gated sites MUST be marked `DISABLED` or `REVIEW_REQUIRED`. Raw HTML and
screenshots MUST NOT be stored in v1 — persist extracted observations, request attempts,
errors, and strategy stats only (§30, §38).

Rationale: this product monitors publicly available pricing. Compliance guardrails and the
"no external unlockers" rule are legal and architectural commitments, not preferences.

### VII. Monetary & Extraction Correctness

Money MUST use `Decimal` in Python and `NUMERIC(18,4)` in PostgreSQL — floats are forbidden for
prices (§19). Currencies MUST NOT be compared across mismatches in v1: a competitor price in a
different currency than the client variant is saved, marked `comparable = false`, excluded from
analysis, and flagged `CURRENCY_MISMATCH` (§19, §23). Every extraction MUST return a tunable
confidence score; a wrong price is worse than a missing one, so values below the configured
minimum (default 0.75) are rejected, and a single bare number on a page is not a price (§17).
Price validation rules (currency, min/max, reject/prefer text) live in the DB and MUST pass
before a price is accepted (§18). The variant alert decision tree MUST be implemented exactly as
one ordered, deterministic computation (§23).

Rationale: incorrect prices silently mislead repricing decisions; correctness, confidence, and
determinism protect the integrity of every downstream alert.

### VIII. Scale-Safe Data & Concurrency

The system MUST be built for 2,000 products and 10,000–20,000 matches per workspace from the
start (§39). This requires:

- **Distributed domain rate limiting** (Redis-backed, per workspace+domain+access method) plus
  domain concurrency semaphores — per-worker limits are insufficient (§12).
- **In-flight deduplication** via a spider-owned, fencing-token match lock
  (`lock:scrape:{workspace_id}:{match_id}`) acquired before fetch and released after persistence,
  plus `unique(scrape_job_id, match_id)` on targets (§13).
- **No hot-row contention.** Per-variant analysis is coalesced (one task per variant per job),
  and parent-job counters are aggregated from `scrape_job_targets`, never incremented once per
  target (§21, §26).
- **Hot reads from current-state tables** (`match_current_prices`, `variant_price_states`,
  `variant_alert_states`), never by scanning historical observations (§15, §22).
- **All Postgres traffic through PgBouncer** (transaction pooling) with capped per-process pools;
  no direct Postgres connections from spider processes; design for `SET LOCAL` / xact-scoped
  advisory locks only (§4, §6).
- **Partitioning and retention.** Append-heavy tables (`price_observations`, `request_attempts`,
  `webhook_events`, `price_alert_events`) MUST be monthly-partitioned with the first real-data
  load, and retention MUST be partition-drop, never bulk `DELETE` (§29).

Rationale: these are the constraints that separate a working demo from a system that survives
production volume; each one prevents a specific, predictable failure at scale.

## Technology & Security Constraints

**Locked stack (§3):** Python, FastAPI, PostgreSQL, SQLAlchemy + Alembic, Celery + Redis, a
custom DB-driven scheduler, Scrapy + Scrapyd, and `scrapy-playwright` (browser service only).
Deployment targets a multi-service platform (Railway or similar). Substituting a core component
is a constitutional amendment, not an implementation choice.

**Identifiers (§21):** primary keys are application-generated UUIDv7 (time-ordered,
insert-friendly) and are treated as public API IDs.

**Secrets & auth (§33):** API keys and refresh tokens are stored only as hashes; full API keys
are shown once; keys support scopes, revocation, and `last_used_at`. Proxy passwords, webhook
secrets, and future integration tokens MUST be encrypted (Fernet key from environment, with
`key_version` for rotation). Roles are `SUPER_ADMIN`, `WORKSPACE_ADMIN`, `READ_ONLY`.

**Error codes (§34):** failures MUST use the defined structured error-code vocabulary (e.g.
`PRICE_NOT_FOUND`, `LOW_CONFIDENCE_PRICE`, `CURRENCY_MISMATCH`, `BLOCKED`, `RATE_LIMITED`,
`LOCKED_ALREADY_RUNNING`, `LEGAL_REVIEW_REQUIRED`) so debugging, the strategy optimizer,
rediscovery triggers, and client reporting share one language.

**API surface (§24):** the public API is versioned under `/v1`; high-volume list endpoints use
cursor-based pagination (default limit 50, max 500).

## Development Workflow & Quality Gates

**Spec-driven delivery (§35, §36).** The system MUST be built incrementally through Spec Kit,
one small independent spec at a time (`/specify → /clarify → /plan → /tasks → /implement`), never
from one giant prompt. The recommended order is phases 00–16; the scheduler, browser service,
and strategy optimizer MUST NOT be built first. The first deliverable is the vertical MVP slice:
DB config → API trigger → Celery → Scrapyd → HTTP spider → fixture-page scrape → observation →
current price → alert → API comparison (§37). Every plan MUST pass a Constitution Check against
these principles before implementation; unavoidable deviations MUST be documented and justified.

**Testing & validation.** Workspace isolation MUST have cross-workspace access tests (Principle
II). The alert decision tree's boundary values MUST be tested for determinism (§23). MVP phases
07–09 MAY use fixture HTML pages and plain tables, but reactor-safety, idempotent dispatch, and
partitioning MUST land before real-data scale (§29, §35).

**Observability from early phases (§31).** Structured JSON logs; job state in PostgreSQL (the
Celery result backend stays disabled by default); and per-domain success/error/latency, queue
depth, rate-limit hits, proxy usage, browser-fallback counts, strategy promotion/rediscovery
events, PgBouncer/Postgres saturation, reactor responsiveness, requeue/overflow counts, and
dedup skips. No external monitoring dependency is required for MVP.

**Scope discipline (§38).** v1 MUST NOT build: frontend/dashboard, billing, automatic product
matching, automatic repricing, external scraping-API integrations, CAPTCHA solving, login
bypass, raw HTML/screenshot archives, AI matching, full SaaS roles/permissions, marketplace-wide
crawling, or browser-first scraping for all URLs.

## Governance

This constitution supersedes other practices and conventions for Crawmatic. `PROJECT_SPEC.md`
is the detailed source of truth; this document is its binding summary of non-negotiables. When
the two conflict on a non-negotiable, the stricter rule applies and the conflict MUST be raised
for amendment.

**Amendments.** Changes to this constitution MUST be made by editing this file with: a written
rationale, a version bump per the policy below, an updated Sync Impact Report, and propagation to
dependent templates (`plan-template.md`, `spec-template.md`, `tasks-template.md`) and runtime
guidance. Re-run `/speckit.constitution` to keep the report and dependent artifacts in sync.

**Versioning policy (semantic):**
- **MAJOR** — backward-incompatible governance changes or removal/redefinition of a principle.
- **MINOR** — a new principle or section, or materially expanded mandatory guidance.
- **PATCH** — clarifications, wording, and non-semantic refinements.

**Compliance review.** Every `/plan` MUST complete its Constitution Check, and every PR/review
MUST verify compliance with these principles — especially the NON-NEGOTIABLE ones (Workspace
Isolation, Disciplined Scraping Runtime, Internal-Only & Legally Compliant Access). Complexity
or deviation MUST be justified in writing against the principle it strains; unjustified
violations block merge.

**Version**: 1.0.0 | **Ratified**: 2026-06-27 | **Last Amended**: 2026-06-27
