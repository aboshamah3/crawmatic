# Implementation Plan: Webhook Events

**Branch**: `016-webhook-events` | **Date**: 2026-07-06 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/srv/crawmatic/crawmatic/specs/016-webhook-events/spec.md`

## Summary

SPEC-16 adds poll-based webhook integration readiness. Two workspace-scoped tables:

- **`webhook_endpoints`** — plain (non-partitioned) CRUD table recording where webhooks will
  *eventually* be delivered. URL is validated at save time by the **existing** SSRF validator
  (`app_shared.url_safety.validate_competitor_url`). Secret stored Fernet-encrypted
  (`secret_encrypted` + `secret_key_version`), never returned raw. No delivery in v1.
- **`webhook_events`** — monthly-partitioned-from-birth append table (PK `(id, created_at)`,
  `PARTITION BY RANGE (created_at)`) recording domain changes, polled via a keyset-paginated API.
  90-day retention is handled by the **already-registered** SPEC-15 maintenance machinery — the
  table is *already* in `PARTITIONED_TABLES` and `RETENTION_WEBHOOK_EVENTS_DAYS=90`; this feature
  only creates the actual table so the existing `table_exists` gate activates.

Events are created (fire-and-forget, after the source `session.commit()`) on a new Celery
`create_webhook_event` task bound to a new `webhook_events` queue, enqueued at three existing
seams: alert-state transitions (SPEC-09 `recompute_variant`), scrape-job finalization
(SPEC-08 `finalize_jobs`), and strategy status changes (SPEC-12 promotion/rediscovery). No new
scheduler/beat entry, no new maintenance job, no automatic delivery, `delivered_at` stays null.

The whole value of this spec is **reuse**. The design below cites the exact existing code each
requirement plugs into.

## Technical Context

**Language/Version**: Python 3.13 (monorepo, `uv` workspace; sync with `uv sync --all-packages`)

**Primary Dependencies**: FastAPI (`apps/api`), SQLAlchemy 2.x + Alembic (`libs/shared/app_shared`),
Celery + Redis (`apps/workers`), `cryptography.fernet` (existing encryption util), `uuid6` (uuidv7).
No new third-party dependency is introduced.

**Storage**: PostgreSQL (via PgBouncer transaction pooling). `webhook_endpoints` plain;
`webhook_events` native range-partitioned by month.

**Testing**: pytest. Unit tests run in this build env; integration tests that need a live
Postgres/Redis skip cleanly via the existing skipif DB probe (no container engine here).

**Target Platform**: Linux services (`api-service`, `worker-service`, `scheduler-service`).

**Project Type**: Web service (monorepo: `apps/api`, `apps/workers`, shared `libs/shared/app_shared`).
Webhooks are scraping-free → all shared code lands in `app_shared`, never `libs/scrape-core`
(Constitution Principle I).

**Performance Goals**: Poll API uses opaque keyset cursor over `(created_at, id)` (SPEC-04
`app_shared.pagination`) for stable cross-partition ordering; default page 50, max 500. Event
creation is off the request/scrape hot path (dedicated queue). Built for 10k–20k matches/workspace.

**Constraints**: Workspace isolation NON-NEGOTIABLE (RLS + app-scoping, fail-closed to 0 rows);
SSRF NON-NEGOTIABLE (reuse existing validator, no second validator); partition-drop retention only
(no bulk DELETE); event creation must never block/fail/roll back the source operation.

**Scale/Scope**: 2 tables, 1 Celery task, 1 new queue, 6 REST endpoints, 3 enqueue seams, 1 Alembic
migration (`down_revision = '4a1dca402f78'`, the current single head).

## Constitution Check

*GATE: evaluated against `.specify/memory/constitution.md` v1.0.1. Re-checked post-design.*

| Principle | Verdict | How this plan complies |
|---|---|---|
| **I. API-First / Service-Oriented** | PASS | Models + validator reuse + `enqueue` seam live in `libs/shared/app_shared` (scraping-free). Router in `apps/api`, task in `apps/workers`. No Scrapy/Twisted import; nothing in `libs/scrape-core`. Producer enqueues by task *name* via `app_shared.messaging.enqueue` (never imports the worker task), preserving the dependency boundary. |
| **II. Workspace Isolation (NON-NEGOTIABLE)** | PASS | Both tables carry non-null `workspace_id` (via `WorkspaceScopedBase`), get `emit_rls_policy(...)` in the migration (`FORCE ROW LEVEL SECURITY`, fail-closed `NULLIF(current_setting('app.workspace_id',true),'')::uuid`), are added to `WORKSPACE_OWNED_MODELS`, and all reads use `scoped_select`/`scoped_get`. Cross-workspace + no-context (0-row) tests mandated. |
| **III. Variant-Level Pricing & Matching** | N/A | Feature does not change pricing/matching; payloads only *reference* variant/job/strategy ids by soft reference. |
| **IV. Database-Driven Configuration** | PASS | Retention window is DB/env-tunable via existing `RETENTION_WEBHOOK_EVENTS_DAYS` setting; endpoint subscriptions are data (`event_types` JSONB), not code. |
| **V. Disciplined Scraping Runtime (NON-NEGOTIABLE)** | PASS | No scraping code touched. Event creation is a separate idempotent Celery task, enqueued *after* commit — mirrors the "analysis is a separate task, not in the spider" rule. |
| **VI. Internal-Only & SSRF (NON-NEGOTIABLE)** | PASS | Reuses `app_shared.url_safety.validate_competitor_url` verbatim for `webhook_endpoints.url` at create+update; maps `UnsafeUrlError` to the established `422 {"error":{"code":"UNSAFE_URL",...}}`. No second validator, no external HTTP made (no delivery in v1). |
| **VII. Monetary & Extraction Correctness** | N/A | No money math; payloads carry pre-computed values as opaque JSON. |
| **VIII. Scale-Safe Data & Concurrency (partitioning)** | PASS | `webhook_events` born-partitioned monthly (PK includes `created_at`); retention = partition drop through the *existing* maintenance job (table already registered in `PARTITIONED_TABLES`, `RETENTION_WEBHOOK_EVENTS_DAYS=90`). No FK into the partitioned table (soft references only); readers tolerate dropped partitions. Keyset pagination avoids OFFSET scans. |
| **Tech/Security constraints** | PASS | uuidv7 PKs (`Base.id` default `new_uuid7`); webhook secret encrypted via existing versioned Fernet (`secret_encrypted` + `secret_key_version`), never returned; `webhooks:read`/`webhooks:write` already in the scope catalog; API under `/v1`, cursor pagination default 50/max 500. |

**Result**: No violations. Complexity Tracking table left empty.

### Grounding corrections discovered during research (important)

Two things the task brief flagged as "must add" are **already present** — adding them again would
break existing tests. The plan explicitly does NOT re-add them:

1. **Scopes already exist.** `WEBHOOKS_READ = "webhooks:read"` and `WEBHOOKS_WRITE = "webhooks:write"`
   are already in `app_shared.security.scopes.Scope` (lines 31–32). The recurring "new scope not
   added to the enum" bug does **not** apply here — no enum edit is needed. Routers just reference
   the string literals `"webhooks:read"` / `"webhooks:write"` via `require_scopes(...)`.
2. **Maintenance registry already includes `webhook_events`.** `PARTITIONED_TABLES` in
   `app_shared/maintenance/registry.py` (entry at lines 67–75) already lists `webhook_events`
   (partition_key `created_at`, `retention_setting="RETENTION_WEBHOOK_EVENTS_DAYS"`, `feeds_rollups=False`),
   and `config.py:234` already defines `RETENTION_WEBHOOK_EVENTS_DAYS: int = 90`.
   `tests/unit/test_partition_registry.py` pins the registry length to exactly 4 and asserts the
   `webhook_events` entry. **Do NOT add a registry entry or a 5th `PartitionedTable`** — only create
   the real table so the existing `table_exists` gate stops skipping it. Retention/partition-create
   then activate automatically with no scheduler change.

## Project Structure

### Documentation (this feature)

```text
specs/016-webhook-events/
├── plan.md              # This file
├── research.md          # Phase 0 — reuse decisions, grounded citations
├── data-model.md        # Phase 1 — entities, columns, enums, indexes, RLS
├── quickstart.md        # Phase 1 — runnable validation scenarios
├── contracts/
│   ├── rest-api.md      # 6 endpoints, request/response schemas, error codes
│   └── events.md        # event_type taxonomy, payload shapes, status enum, enqueue seams
└── tasks.md             # Phase 2 (/speckit-tasks — NOT created here)
```

### Source Code (repository root) — files this feature adds/edits

```text
libs/shared/app_shared/
├── models/
│   ├── webhooks.py                 # NEW: WebhookEndpoint, WebhookEvent models
│   └── __init__.py                 # EDIT: export the two models
├── enums.py                        # EDIT: add WebhookEventType, WebhookEventStatus StrEnums
├── repository.py                   # EDIT: add both models to WORKSPACE_OWNED_MODELS
├── task_names.py                   # EDIT: add CREATE_WEBHOOK_EVENT = "webhook_events.create_webhook_event"
├── webhooks/
│   └── payloads.py                 # NEW: pure builders mapping (source enum) -> (event_type, payload)
│                                   #      + payload size guard; imported by the enqueue seams
├── url_safety.py                   # REUSE (no change): validate_competitor_url / UnsafeUrlError
├── security/encryption.py          # REUSE (no change): encrypt_secret / decrypt_secret
├── pagination.py                   # REUSE (no change): clamp_limit / encode/decode_cursor / keyset
├── messaging.py                    # REUSE (no change): enqueue(name, queue=, kwargs=)
└── maintenance/registry.py         # REUSE (no change): webhook_events already registered

alembic/versions/
└── <newrev>_webhook_events_and_endpoints.py   # NEW: down_revision='4a1dca402f78'
                                                #      creates webhook_endpoints (plain) +
                                                #      webhook_events (partitioned) + RLS + partitions

apps/api/app/
├── routers/webhooks.py             # NEW: 6 endpoints (/v1/webhook-endpoints*, /v1/webhook-events*)
├── schemas/webhooks.py             # NEW: request/response Pydantic (has_secret derived bool)
└── main.py                         # EDIT: import + include_router(webhooks.router)

apps/workers/app/workers/
├── tasks_webhooks.py               # NEW: @app.task(name=CREATE_WEBHOOK_EVENT) create_webhook_event
├── celery_app.py                   # EDIT: add "webhook_events" queue, CREATE_WEBHOOK_EVENT route,
│                                   #       import constant, add module to include=[...]
├── tasks_analysis.py               # EDIT: enqueue after commit when event_type is not None (SPEC-09)
├── tasks_jobs.py                   # EDIT: enqueue after commit for each finalized terminal job (SPEC-08)
└── tasks_strategy.py               # EDIT: enqueue after commit on promotion/rediscovery (SPEC-12)

tests/
├── unit/test_webhook_models.py                 # NEW
├── unit/test_webhook_payloads.py               # NEW: taxonomy + payload builders + size guard
├── unit/test_migration_offline_webhooks.py     # NEW: mirror test_migration_offline_alerts.py
├── unit/test_webhook_response_guard.py          # NEW: response never exposes secret_encrypted
├── integration/test_api_webhook_endpoints.py    # NEW (skipif no DB): CRUD + SSRF reject + isolation
└── integration/test_api_webhook_events.py       # NEW (skipif no DB): poll/pagination + isolation + seams
```

**Structure Decision**: Existing monorepo layout. Shared, scraping-free logic (models, enums,
payload builders, task-name constant) → `libs/shared/app_shared`. HTTP surface → `apps/api`.
Background task + enqueue seams → `apps/workers`. This matches the SPEC-13 `refresh_rules` precedent
(closest tenant-only CRUD template) and the SPEC-09 `price_alert_events` precedent (closest
born-partitioned template).

## Key design decisions (resolved here; grounded in existing enums)

1. **`webhook_events.status`** → new `WebhookEventStatus` StrEnum, v1 value **`PENDING`**
   (recorded-but-not-delivered). Reserve `DELIVERED`/`FAILED` for the future delivery feature.
   `delivered_at` stays null in v1 (FR-010/FR-011, SC-007). Stored as `String(32)` column (mirrors
   how `price_alert_events.event_type` stores an enum via a String column).

2. **`event_type` taxonomy** → new `WebhookEventType` StrEnum with concrete stable strings derived
   from the master doc §22 list plus the actual source enums (stored as a `String(64)` column;
   producer-side validated by the enum). Endpoint `event_types` remains a free JSONB list of strings
   (forward-compatible — an unknown subscribed type is permitted and simply never matches, per the
   edge case).

   | Source (existing enum) | Enum member(s) | `event_type` string |
   |---|---|---|
   | `AlertEventType.CREATED` (SPEC-09) | transition | `price.alert.created` |
   | `AlertEventType.UPDATED` | transition | `price.alert.updated` |
   | `AlertEventType.RESOLVED` | transition | `price.alert.resolved` |
   | `AlertEventType.REOPENED` | transition | `price.alert.reopened` |
   | `ScrapeJobStatus.COMPLETED` (SPEC-08) | terminal | `scrape.job.completed` |
   | `ScrapeJobStatus.PARTIAL_FAILED` | terminal | `scrape.job.partial_failed` |
   | `ScrapeJobStatus.FAILED` | terminal | `scrape.job.failed` |
   | `StrategyStatus.ACTIVE` (promotion) / `DEGRADED` (rediscovery) (SPEC-12) | status change | `domain.strategy.updated` |

   (`AlertEventType.UNCHANGED` is never persisted, so it never produces an event — matches the
   existing `if event_type is not None` guard.) The master-doc extras `match.scrape.failed` and
   `product.comparison.updated` are **out of v1 scope** for enqueue seams (no wired commit point for
   them in this spec's three sources); they may be added later without schema change since
   `event_type` is a free String column.

3. **Both tables** get `workspace_id`, RLS, app-scoping; no-context session → 0 rows (fail-closed via
   the `NULLIF(current_setting('app.workspace_id',true),'')::uuid` policy predicate).

4. **Payload bounding** (edge case / "very large payload"): payloads are built by pure builders in
   `app_shared/webhooks/payloads.py` from a fixed set of fields (ids + change descriptors), and the
   builder asserts the serialized JSON is under a bounded size (e.g. 8 KiB) so a source can never
   store an unbounded blob.

5. **Idempotency / at-least-once** (FR-009): Celery is at-least-once. The task tolerates duplicates
   (v1 accepts occasional duplicate rows rather than contradictory ones). Best-effort dedup mirrors
   the existing `pipelines.py` Redis `SET NX` precedent: the enqueue passes a stable `dedup_key`
   (e.g. `f"{event_type}:{entity_id}:{discriminator}"`); the task may `SET NX` it before insert to
   collapse retries. This is best-effort, not a correctness dependency.

6. **Fire-and-forget safety** (FR-009 / SC-005): every enqueue happens strictly **after** the source
   `session.commit()` and awaits no result (`app_shared.messaging.enqueue` uses `send_task`, never
   `.get()`). Because there is no existing precedent for swallowing broker errors at these seams, the
   webhook enqueue calls are wrapped in a narrow `try/except` that logs and continues — guaranteeing a
   broker hiccup can never fail or roll back the already-committed source operation.

## Complexity Tracking

*No constitution violations — no entries.*
