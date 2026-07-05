# Implementation Plan: Scheduler

**Branch**: `013-scheduler` | **Date**: 2026-07-05 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/013-scheduler/spec.md`

## Summary

Deliver the custom DB-driven scheduler enqueuer plus its configuration surface. A new
workspace-owned `refresh_rules` table (RLS from its first migration) captures *what* to re-scrape
(one of six `ScrapeScope`s + target id) on *what cadence* (exactly one of a 5-field UTC
`cron_expression` or `interval_minutes`), exposed through a `/v1/refresh-rules` CRUD router
(create/read/list/update/delete + enable/disable via PATCH). The existing `apps/scheduler` loop is
extended with a poll-interval refresh pass that claims due rules with
`SELECT ... WHERE enabled AND next_run_at <= now() ORDER BY next_run_at FOR UPDATE SKIP LOCKED`,
resolves each rule's scope to its ACTIVE matches, creates a `type=SCHEDULED / source=SCHEDULER`
scrape job **through the reused SPEC-08 job service**, enqueues the Celery dispatch task **inside
the claiming transaction before commit**, and advances `next_run_at`/`last_run_at`/`locked_at`.
No global pass-lock; multiple scheduler instances are safe via SKIP LOCKED. Cadence math lives in a
new scraping-free `app_shared/scheduling/cadence.py` (adds `croniter`). The single design decision
requiring justification — a BYPASSRLS system session for the inherently cross-tenant claim — is
tracked in Complexity Tracking; app-level workspace scoping is preserved throughout.

Full rationale in [research.md](./research.md); entity in [data-model.md](./data-model.md);
behavior in [contracts/](./contracts/); validation in [quickstart.md](./quickstart.md).

## Technical Context

**Language/Version**: Python 3.12 (uv workspace).

**Primary Dependencies**: FastAPI (API), SQLAlchemy 2.0 + Alembic (sync `Session`), Celery + Redis
(dispatch), PostgreSQL (RLS, `FOR UPDATE SKIP LOCKED`), `croniter` (**new**, pure-Python cron parse
in `app_shared`). No Scrapy/Twisted/Playwright anywhere in this feature (FR-019).

**Storage**: PostgreSQL via PgBouncer transaction pooling. One new table `refresh_rules`
(workspace-owned, RLS-forced). Reuses `scrape_jobs`, `scrape_job_targets`,
`competitor_product_matches`, `product_group_items`.

**Testing**: pytest. Unit tests (no DB) for cadence math, scope-predicate selection, claim/enqueue
ordering, validation, import boundary. Live-DB integration tests (`*_live.py`, `skipif` probe) for
SKIP-LOCKED concurrency, RLS denial, alembic upgrade/downgrade, end-to-end pass — skip cleanly with
no Postgres (this build env has none).

**Target Platform**: Linux server; multi-service deploy. `refresh_rules` CRUD in `api-service`;
refresh pass in `scheduler-service` (`python -m app.scheduler.scheduler_app`).

**Project Type**: Backend monorepo (uv workspace: `apps/*` + `libs/*`). No frontend (v1 backend-only).

**Performance Goals**: 2k products / 10k–20k matches per workspace (Principle VIII). Partial index
`(next_run_at) WHERE enabled` keeps the due-scan cheap; bounded claim batch (`SCHEDULER_CLAIM_BATCH_LIMIT`)
keeps row-lock windows short; poll every `SCHEDULER_POLL_INTERVAL_SECONDS` (default 30s).

**Constraints**: enqueue-before-commit is non-negotiable (§28/FR-012); no global/advisory pass-lock
(FR-009); app-level workspace scoping mandatory + RLS defense-in-depth (Principle II); scraping-free
scheduling path (FR-019); UUIDv7 PKs, TIMESTAMPTZ timestamps, `/v1` + cursor pagination, structured
error codes (constitution Tech constraints).

**Scale/Scope**: 1 new table, 1 new CRUD router (~6 endpoints), 1 new shared cadence module, 1 new
shared job-service seam (`resolve_scope_matches` + `create_scope_job`), 1 new scheduler pass module,
extension of the existing scheduler loop, 1 Alembic migration, 2 new `Settings` knobs, 1 new
BYPASSRLS session helper.

## Constitution Check

*GATE: evaluated against `.specify/memory/constitution.md` v1.0.1.*

| Principle | Assessment |
|---|---|
| I. API-First / Service-Oriented / scraping-free `app_shared` | **PASS.** CRUD in `api-service`; pass in `scheduler-service`. New code in `app_shared`/`apps/scheduler`/`apps/api` imports no Scrapy/Twisted/Playwright; `croniter` is pure-Python. `tests/unit/test_import_boundaries.py` stays green; optional new scheduler-app boundary test added. |
| II. Workspace Isolation (NON-NEGOTIABLE) | **PASS (1 justified deviation).** `refresh_rules` is workspace-owned with app-level `scoped_select`/`scoped_get` **and** RLS from its first migration; registered in `WORKSPACE_OWNED_MODELS`; cross-workspace denial tests. Deviation: the cross-tenant claim runs on a BYPASSRLS system session — app-level scoping is retained for every job/target read/write, mirroring the existing auth BYPASSRLS seam. See Complexity Tracking. |
| III. Variant-Level Pricing & Explicit Matching | **PASS.** No pricing/matching change; scopes resolve to existing ACTIVE `CompetitorProductMatch` rows. |
| IV. Database-Driven Configuration | **PASS (embodies it).** Refresh rules are DB-driven "how often to scrape" config — a §9/§41 config surface, not hardcoded cadence. |
| V. Disciplined Scraping Runtime (NON-NEGOTIABLE) | **PASS.** Scheduler enqueues the existing idempotent dispatch task; does not start Scrapy. Enqueue-before-commit + SPEC-08 dispatch guard + SPEC-11 match locks make at-least-once safe. |
| VI. Internal-Only & Legally Compliant Access (NON-NEGOTIABLE) | **PASS.** No access-method or legal-surface change. |
| VII. Monetary & Extraction Correctness | **PASS.** No monetary logic; no floats introduced. |
| VIII. Scale-Safe Data & Concurrency | **PASS.** SKIP-LOCKED row claiming (no hot-row, no global lock, FR-017), partial due-index, bounded batch, xact-scoped locks only, PgBouncer-safe (`SET LOCAL`/no session state). |
| Tech & Security constraints | **PASS.** UUIDv7 PK, TIMESTAMPTZ, `/v1` prefix, cursor pagination (limit 50/max 500), structured `{"error":{"code","message"}}` vocabulary. |
| Workflow / scope discipline | **PASS.** Incremental spec; scheduler correctly sequenced after the MVP + jobs specs; no forbidden v1 scope (no frontend/billing/auto-match). |

**Gate result: PASS** (one deviation documented and justified below). Re-checked after Phase 1
design — unchanged.

## Project Structure

### Documentation (this feature)

```text
specs/013-scheduler/
├── plan.md              # This file
├── research.md          # Phase 0 — decisions R1–R9
├── data-model.md        # Phase 1 — refresh_rules entity + migration
├── quickstart.md        # Phase 1 — validation scenarios
├── contracts/
│   ├── refresh-rules-api.md    # US1 REST CRUD + enable/disable
│   ├── scheduler-loop.md       # US2/US3 claim + enqueue-before-commit pass
│   └── job-service-seam.md     # reused scope→match resolution + scoped job creation
├── spec.md
├── autospec-decisions.md
└── tasks.md             # (later, /speckit-tasks)
```

### Source Code (repository root)

```text
libs/shared/app_shared/
├── models/
│   ├── refresh_rules.py        # NEW — RefreshRule (Base+WorkspaceScopedBase+TimestampMixin)
│   └── __init__.py             # EDIT — export RefreshRule
├── repository.py               # EDIT — add RefreshRule to WORKSPACE_OWNED_MODELS
├── scheduling/
│   ├── __init__.py             # NEW
│   └── cadence.py              # NEW — compute_next_run_at (croniter + interval), validate_cron
├── jobs/
│   ├── scopes.py               # NEW — resolve_scope_matches (six scopes)
│   └── service.py              # EDIT — add create_scope_job (type/source params)
├── database.py                 # EDIT — add get_system_session (BYPASSRLS, cross-tenant claim)
├── config.py                   # EDIT — SCHEDULER_POLL_INTERVAL_SECONDS, SCHEDULER_CLAIM_BATCH_LIMIT, SYSTEM_DATABASE_URL
└── pyproject.toml              # EDIT — add croniter dependency

apps/scheduler/app/scheduler/
├── scheduler_app.py            # EDIT — add refresh-pass poll accumulator
└── refresh.py                  # NEW — run_refresh_pass(session, *, now, batch_limit)

apps/api/app/
├── routers/refresh_rules.py    # NEW — /v1/refresh-rules CRUD + PATCH enable/disable
├── schemas/refresh_rules.py    # NEW — Create/Update/Response/ListResponse
└── main.py                     # EDIT — include_router(refresh_rules.router)

alembic/versions/
└── <rev>_refresh_rules.py      # NEW — create table + CHECKs + partial index + RLS; down_revision=f30c60cfa2f7

tests/
├── unit/
│   ├── test_cadence.py                     # NEW — cron + interval + backlog math
│   ├── test_scope_resolution.py            # NEW — per-scope predicate selection
│   ├── test_create_scope_job.py            # NEW — zero-match + enqueue-before-commit ordering (fake session)
│   ├── test_refresh_rules_validation.py    # NEW — cadence/scope validation, error codes
│   └── test_scheduler_import_boundary.py   # NEW (optional) — apps/scheduler purity
└── integration/
    ├── test_refresh_rules_crud_live.py     # NEW — CRUD + cross-workspace RLS denial
    ├── test_refresh_rules_migration_live.py# NEW — alembic upgrade/downgrade
    └── test_scheduler_pass_live.py         # NEW — SKIP-LOCKED concurrency, backlog, zero-match, cascade
```

**Structure Decision**: Backend monorepo (uv workspace). Model + cadence + job seam + session go in
`libs/shared/app_shared` (scraping-free, shared by API and scheduler). The claim/enqueue pass lives
in `apps/scheduler`; the CRUD surface in `apps/api`. This honors Principle I's service boundaries and
FR-010/011's "reuse the job service, don't duplicate."

## Complexity Tracking

| Deviation | Why needed | Simpler alternative rejected because |
|---|---|---|
| Scheduler pass uses a **BYPASSRLS** system session (`get_system_session`, `SYSTEM_DATABASE_URL` → falls back to `AUTH_DATABASE_URL`) | The due-rule claim (§28) is inherently cross-tenant: one `SELECT ... FOR UPDATE SKIP LOCKED` must see due rules across **all** workspaces. Under `FORCE ROW LEVEL SECURITY` the pooler role with no `app.workspace_id` returns zero rows. App-level workspace scoping is fully preserved (job/target reads via `scoped_select(..., rule.workspace_id)`, inserts set `workspace_id` explicitly), so Principle II's mandatory control still holds; RLS is the defense-in-depth layer a trusted system component legitimately bypasses — identical to the existing `get_auth_session()` credential-lookup seam. | *Per-workspace claim loop* — no cheap "which workspaces have due rules" signal, N round-trips, loses one-shot SKIP-LOCKED batching. *Reuse `get_auth_session()`* — violates its documented "credentials ONLY" contract. *Make the pooler role BYPASSRLS* — would silently disable RLS for the entire API. |

No other deviations. The permitted-but-unused per-rule `pg_advisory_xact_lock` (FR-009) is
deliberately omitted because `SKIP LOCKED` already guarantees exclusive per-row claiming.

---

## Phase 0 — Outline & Research

Complete → [research.md](./research.md). All Technical Context unknowns resolved (R1 cron library
`croniter` + cadence math home; R2 cross-tenant claim vs RLS; R3 enqueue-before-commit reuse;
R4 scope→match resolution + zero-match; R5 SKIP-LOCKED claim; R6 `ScrapeScope` reuse; R7 FK
`ondelete=CASCADE`; R8 poll/batch knobs; R9 validation & error codes). No NEEDS CLARIFICATION
remain.

## Phase 1 — Design & Contracts

Complete. Artifacts: [data-model.md](./data-model.md), [contracts/refresh-rules-api.md](./contracts/refresh-rules-api.md),
[contracts/scheduler-loop.md](./contracts/scheduler-loop.md),
[contracts/job-service-seam.md](./contracts/job-service-seam.md), [quickstart.md](./quickstart.md).

**Agent context update**: skipped — this project does not use GitHub Copilot
(`.github/copilot-instructions.md` was removed; the `after_plan` agent-context hook is `enabled:
false` in `.specify/extensions.yml`, and user memory records "No GitHub Copilot"). No agent context
file to update.

## Phase 2

Task generation is performed by `/speckit-tasks` (not this command).
