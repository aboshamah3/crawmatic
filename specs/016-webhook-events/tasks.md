---
description: "Task list for SPEC-16 Webhook Events implementation"
---

# Tasks: Webhook Events (SPEC-16)

**Input**: Design documents from `/srv/crawmatic/crawmatic/specs/016-webhook-events/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/rest-api.md, contracts/events.md, quickstart.md (all present)

**Tests**: INCLUDED. The spec mandates exhaustive unit tests (enums, payload builders + size guard, schema serialization / secret-hiding, cursor logic, scope mapping, offline-migration render) and integration tests that skip cleanly via the existing `pytest.mark.skipif` DB probe (no live Postgres/Redis/Celery in this build env) for the live-only cases (migration round-trip, RLS cross-workspace denial, partition creation, poll pagination across partitions, event creation at seams, retention drop).

**Organization**: Tasks are grouped by user story (US1 poll API = P1/MVP, US2 endpoint CRUD = P2, US3 event creation = P2) to enable independent implementation and testing.

## Reuse-first ground rules (from plan.md / research.md — DO NOT violate)

- **DO NOT** re-add `webhooks:read` / `webhooks:write` to `app_shared/security/scopes.py` (already present, test-pinned). VERIFY only.
- **DO NOT** add or modify a `PARTITIONED_TABLES` entry in `app_shared/maintenance/registry.py` (`webhook_events` already registered) or change `RETENTION_WEBHOOK_EVENTS_DAYS=90` in `config.py` (test-pinned to `len==4` and `webhook_events: 90`). VERIFY only.
- **REUSE verbatim** (no new/divergent copy): SSRF validator `app_shared.url_safety.validate_competitor_url` (string/literal check, **no DNS resolution** at save time — DNS re-resolution is the deferred delivery-time control, out of v1 scope); Fernet `app_shared.security.encryption.encrypt_secret`; keyset cursors in `app_shared.pagination`; `app_shared.messaging.enqueue`; SPEC-15 maintenance machinery. NOTE: `has_secret` is NOT a reused function — it is a **derived response boolean** (`secret_encrypted is not None`) built by explicit field mapping in the endpoint schema (T021).
- **ONE** Alembic migration, `down_revision = '4a1dca402f78'` (the verified current single head); single-head guard must stay green.
- Producer seams enqueue by task **name** only (`app_shared.messaging.enqueue`) — never import the worker task (Constitution I boundary).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 / US2 / US3 (Setup, Foundational, Polish carry no story label)

## Path Conventions (this monorepo, per plan.md)

- Shared, scraping-free: `libs/shared/app_shared/`
- HTTP surface: `apps/api/app/`
- Background tasks + seams: `apps/workers/app/workers/`
- Migration: `alembic/versions/`
- Tests: `tests/unit/`, `tests/integration/`

---

## Phase 1: Setup (Shared Enums, Constants & Invariant Verification)

**Purpose**: Add the small shared additive pieces every story consumes, and VERIFY the pre-existing invariants the plan says are already in place (so the build does not accidentally duplicate them).

- [X] T001 VERIFY (no edit) that `WEBHOOKS_READ = "webhooks:read"` and `WEBHOOKS_WRITE = "webhooks:write"` already exist in `libs/shared/app_shared/security/scopes.py` (Scope enum ~lines 31–32) and are referenced by `tests/unit/test_scopes.py`. If present, make NO change; if somehow absent, stop and flag — do not silently re-add.
- [X] T002 VERIFY (no edit) that `PARTITIONED_TABLES` in `libs/shared/app_shared/maintenance/registry.py` already contains the `webhook_events` entry (`partition_key="created_at"`, `feeds_rollups=False`, `retention_setting="RETENTION_WEBHOOK_EVENTS_DAYS"`) and that `RETENTION_WEBHOOK_EVENTS_DAYS: int = 90` exists in `libs/shared/app_shared/config.py` (~line 234). Confirm `tests/unit/test_partition_registry.py` still asserts `len(PARTITIONED_TABLES) == 4` and `tests/unit/test_retention_eligibility.py` expects `webhook_events: 90`. Make NO change to any of these.
- [X] T003 VERIFY the current single Alembic head is `4a1dca402f78` by running `uv run alembic heads` (expect exactly one head) so the new migration's `down_revision` is correct; record the value for T009.
- [X] T004 Add two StrEnums to `libs/shared/app_shared/enums.py`: `WebhookEventStatus` (`PENDING`/`DELIVERED`/`FAILED`; v1 writes only `PENDING`) and `WebhookEventType` with exactly the 8 members from data-model.md (`PRICE_ALERT_CREATED="price.alert.created"`, `PRICE_ALERT_UPDATED="price.alert.updated"`, `PRICE_ALERT_RESOLVED="price.alert.resolved"`, `PRICE_ALERT_REOPENED="price.alert.reopened"`, `SCRAPE_JOB_COMPLETED="scrape.job.completed"`, `SCRAPE_JOB_PARTIAL="scrape.job.partial_failed"`, `SCRAPE_JOB_FAILED="scrape.job.failed"`, `DOMAIN_STRATEGY_UPDATED="domain.strategy.updated"`).
- [X] T005 [P] Add `CREATE_WEBHOOK_EVENT = "webhook_events.create_webhook_event"` to `libs/shared/app_shared/task_names.py`.
- [X] T006 [P] Unit test the enums in `tests/unit/test_webhook_enums.py`: assert both StrEnums' exact member→value mappings, that `WebhookEventType` has exactly 8 members with the strings above, and that `WebhookEventStatus.PENDING == "PENDING"` (StrEnum value equality).

**Checkpoint**: Shared enums + task-name constant land; pre-existing scopes/registry/config invariants confirmed untouched.

---

## Phase 2: Foundational (Models, Migration, Isolation Registration)

**Purpose**: Create the two tables and wire workspace-isolation. BLOCKS every user story — the poll API (US1), CRUD (US2), and event creation (US3) all require the models + migration + RLS + scoping registration.

**⚠️ CRITICAL**: No user-story work can begin until this phase is complete.

- [X] T007 Create `libs/shared/app_shared/models/webhooks.py` with two models (mirror precedents named in research.md R3):
  - `WebhookEndpoint(Base, WorkspaceScopedBase, TimestampMixin)` — plain table `webhook_endpoints` (mirrors `refresh_rules`): `name String(200) not null`, `url Text not null`, `secret_encrypted Text null`, `secret_key_version Integer null`, `enabled Boolean not null default true`, `event_types JSONB not null default list`; single-column uuidv7 `id` PK; only FK is `workspace_id`.
  - `WebhookEvent(Base, WorkspaceScopedBase)` — born-partitioned table `webhook_events` (mirror `models/alerts.py::PriceAlertEvent`): NO `TimestampMixin`; `created_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)` (composite PK `(id, created_at)` via `PrimaryKeyConstraint("id","created_at", name="pk_webhook_events")`), `event_type String(64) not null`, `payload JSONB not null`, `status String(32) not null default WebhookEventStatus.PENDING`, `delivered_at TZDateTime null`; `__table_args__` includes `ForeignKeyConstraint(["workspace_id"],["workspaces.id"], name="fk_webhook_events_workspace_id_workspaces")`, indexes `ix_webhook_events_ws_created_id` on `(workspace_id, created_at, id)` and `ix_webhook_events_ws_type_created` on `(workspace_id, event_type, created_at)`, and ends with `{"postgresql_partition_by": "RANGE (created_at)"}`. Only `workspace_id` gets a real FK (soft references otherwise — FR-019).
- [X] T008 Export `WebhookEndpoint` and `WebhookEvent` from `libs/shared/app_shared/models/__init__.py`.
- [X] T009 Add both models to `WORKSPACE_OWNED_MODELS` in `libs/shared/app_shared/repository.py` (single source of truth). Do NOT edit `scripts/check_workspace_scoping.py` — it imports `WORKSPACE_OWNED_MODELS` automatically (no hardcoded list to mirror); just run it afterward to VERIFY the guard stays green.
- [X] T010 Create the single Alembic migration `alembic/versions/<newrev>_webhook_events_and_endpoints.py` with `down_revision = '4a1dca402f78'` (value confirmed in T003). It MUST: (a) `op.create_table("webhook_endpoints", ...)` plain with `workspace_id` FK + `ix_webhook_endpoints_workspace_id`; (b) `op.create_table("webhook_events", ...)` with `sa.PrimaryKeyConstraint("id","created_at", name="pk_webhook_events")` and `postgresql_partition_by="RANGE (created_at)"` plus the two composite indexes; (c) copy the `_month_partition_bounds(base)` helper verbatim (from migration `e4a75b48360c`) and `CREATE TABLE webhook_events_YYYY_MM PARTITION OF webhook_events FOR VALUES FROM (...) TO (...)` for current + next month; (d) `for stmt in emit_rls_policy("webhook_endpoints"): op.execute(stmt)` AND `for stmt in emit_rls_policy("webhook_events"): op.execute(stmt)`; (e) `downgrade()` drops child partitions then both parents. Do NOT touch `PARTITIONED_TABLES`.
- [X] T011 [P] Unit test `tests/unit/test_migration_offline_webhooks.py` mirroring `tests/unit/test_migration_offline_alerts.py`: render the migration offline (no DB) and assert it emits `CREATE TABLE webhook_endpoints`, a partitioned `CREATE TABLE webhook_events ... PARTITION BY RANGE (created_at)`, the two child `PARTITION OF` statements, and the RLS trio (`ENABLE`/`FORCE ROW LEVEL SECURITY` + policy) for BOTH tables.
- [X] T012 [P] Unit test `tests/unit/test_webhook_models.py`: assert table names, that `WebhookEvent` PK is `(id, created_at)` and carries the `postgresql_partition_by` table arg, that `WebhookEndpoint` has a single-column `id` PK with `created_at`/`updated_at` (+ `onupdate`), that both models expose `workspace_id`, and that `event_types`/`payload` are JSON-typed.
- [X] T013 [P] Unit test `tests/unit/test_webhook_single_head.py` (or extend the existing single-head assertion): assert `alembic` reports exactly one head after adding the new migration, so `tests/unit/test_strategy_single_head.py::test_alembic_heads_reports_exactly_one_head` stays green (linear chain, `down_revision='4a1dca402f78'`).

**Checkpoint**: Both tables modelled, migrated, RLS-protected, and scoping-registered. User stories can now proceed (in parallel if staffed).

---

## Phase 3: User Story 1 - Poll workspace events for integration (Priority: P1) 🎯 MVP

**Goal**: Read-only, keyset-paginated poll API over `webhook_events` — `GET /v1/webhook-events` (list, `event_type` filter, cursor) and `GET /v1/webhook-events/{id}` — workspace-scoped, `webhooks:read`.

**Independent Test**: Seed N events in a workspace, list with page size P<N, walk `next_cursor` to exhaustion → every event returned exactly once, deterministically ordered by `(created_at, id)`, gapless across a month boundary; single-event fetch returns the matching payload; a caller from another workspace sees none (404 / absent); an invalid cursor → 422 `INVALID_CURSOR`.

### Implementation for User Story 1

- [ ] T014 [US1] Create `apps/api/app/schemas/webhooks.py` with `WebhookEventResponse` (`id`, `event_type`, `payload`, `status`, `created_at`, `delivered_at` — always null in v1) and `WebhookEventListResponse` (`{items: list[WebhookEventResponse], next_cursor: str | None}`), mirroring `apps/api/app/schemas/alerts.py::AlertEventListResponse`.
- [ ] T015 [US1] Create `apps/api/app/routers/webhooks.py` with `GET /v1/webhook-events` — `require_scopes("webhooks:read")`, params `limit?` (via `clamp_limit`, default 50 / max 500 / min 1), `cursor?`, `event_type?`; build `scoped_select(WebhookEvent, workspace_id)` + optional `where(event_type==...)` + `keyset_predicate` + `order_by(created_at, id)` + `limit+1` + `paginate`; on `decode_cursor` raising `InvalidCursor` return `422 {"error":{"code":"INVALID_CURSOR",...}}` (mirror `apps/api/app/routers/alerts.py::_invalid_cursor`). Empty/past-the-end → `{items: [], next_cursor: null}` (not an error).
- [ ] T016 [US1] Add `GET /v1/webhook-events/{id}` to `apps/api/app/routers/webhooks.py` — `require_scopes("webhooks:read")`, `scoped_get(session, WebhookEvent, id, workspace_id)`; not in workspace → `404 NOT_FOUND`.
- [ ] T017 [US1] Mount the router in `apps/api/app/main.py`: add `webhooks` to the router import block and `app.include_router(webhooks.router)` (paths are written inline as full `/v1/...`, no `prefix=`).
- [ ] T018 [P] [US1] Unit test `tests/unit/test_webhook_event_schema.py`: build `WebhookEventResponse` from a stub ORM-like object and assert `delivered_at` serializes as `null`, `status` is the string value, and the field set matches the contract; assert `WebhookEventListResponse` shape `{items, next_cursor}`.
- [ ] T019 [P] [US1] Unit test `tests/unit/test_webhook_poll_cursor.py`: exercise the reused `app_shared.pagination` round-trip used by the events list — `encode_cursor(created_at, id)`/`decode_cursor` round-trips, `clamp_limit` bounds (1/50/500), `decode_cursor(bad_token)` raises `InvalidCursor`, and `keyset_predicate` ordering is monotonic over `(created_at, id)` (cross-partition stability, SC-001).
- [ ] T020 [US1] Integration test `tests/integration/test_api_webhook_events.py` guarded by the existing `pytest.mark.skipif` DB probe (mirror `tests/integration/test_alert_events_history_live.py`): seed events spanning two monthly partitions, walk the full backlog via cursor (exactly-once, gapless, deterministic — SC-001); `event_type` filter; single fetch by id; cross-workspace fetch → 404 and absent from list; no-workspace-context session → 0 rows (SC-004); invalid cursor → 422; missing `webhooks:read` scope → 403.

**Checkpoint**: US1 is the deployable MVP — events (however created/seeded) are pollable and workspace-isolated.

---

## Phase 4: User Story 2 - Register and manage webhook endpoints safely (Priority: P2)

**Goal**: Full CRUD for `webhook_endpoints` — `POST/GET/GET{id}/PATCH/DELETE /v1/webhook-endpoints` — with SSRF validation at save time, encrypted secret (never returned), `webhooks:write` for mutations / `webhooks:read` for reads.

**Independent Test**: Create with a public https URL → round-trips with `has_secret` (never the raw secret); private/loopback/metadata/userinfo/non-http(s) URL → rejected `422 UNSAFE_URL`, nothing persisted; PATCH name/enabled/event_types/secret persists and advances `updated_at`; DELETE → 404 afterward; another workspace can never see/mutate (404/absent); `webhooks:read`-only key is refused write ops (403).

### Implementation for User Story 2

- [ ] T021 [US2] Extend `apps/api/app/schemas/webhooks.py` with `WebhookEndpointCreate` (`extra="forbid"`: `name`, `url`, `enabled=true`, `event_types=[]`, `secret?`), `WebhookEndpointUpdate` (all optional, tri-state via `model_dump(exclude_unset=True)`; `secret` omitted=unchanged / `null`=clear / value=re-encrypt), `WebhookEndpointResponse` (`id`, `workspace_id`, `name`, `url`, `enabled`, `event_types`, `has_secret`, `created_at`, `updated_at` — NO `secret*`), `WebhookEndpointListResponse` (`{items, next_cursor}`). Include a `_to_response(orm)` helper that maps fields EXPLICITLY and derives `has_secret = orm.secret_encrypted is not None` (never `model_validate(orm_obj)`), mirroring `ProxyProviderResponse._to_response` — FR-005.
- [ ] T022 [US2] Add `POST /v1/webhook-endpoints` to `apps/api/app/routers/webhooks.py` — `require_scopes("webhooks:write")`; call `validate_competitor_url(url)` and map `UnsafeUrlError` → `422 {"error":{"code":"UNSAFE_URL","message":str(exc),"reason":exc.reason.value}}` persisting nothing (mirror `apps/api/app/routers/matches.py:101–105`); if `secret` present, `encrypt_secret` → store `secret_encrypted`/`secret_key_version`; insert scoped to `workspace_id`; return `201` `WebhookEndpointResponse` via `_to_response`.
- [ ] T023 [US2] Add `GET /v1/webhook-endpoints` (list, `require_scopes("webhooks:read")`, `limit?`/`cursor?` keyset over `(created_at, id)`, `scoped_select`) and `GET /v1/webhook-endpoints/{id}` (`scoped_get`, cross-ws → 404) to `apps/api/app/routers/webhooks.py`.
- [ ] T024 [US2] Add `PATCH /v1/webhook-endpoints/{id}` to `apps/api/app/routers/webhooks.py` — `require_scopes("webhooks:write")`, `scoped_get` then apply `exclude_unset` fields; if `url` present re-validate (same `UNSAFE_URL` rule); secret tri-state (omit=unchanged, `null`=clear both columns, value=re-encrypt); `updated_at` advances (FR-004); cross-ws → 404; return `200` `WebhookEndpointResponse`.
- [ ] T025 [US2] Add `DELETE /v1/webhook-endpoints/{id}` to `apps/api/app/routers/webhooks.py` — `require_scopes("webhooks:write")`, `scoped_get` then delete; `204` on success; cross-ws → 404; subsequent get/list absent.
- [ ] T026 [P] [US2] Unit test `tests/unit/test_webhook_response_guard.py` mirroring `tests/unit/test_access_guards.py`: assert the JSON of `WebhookEndpointResponse` NEVER contains `secret`, `secret_encrypted`, or `secret_key_version`, and DOES contain `has_secret: bool` derived from `secret_encrypted is not None`.
- [ ] T027 [P] [US2] Unit test `tests/unit/test_webhook_endpoint_schema.py`: `WebhookEndpointCreate` rejects unknown fields (`extra="forbid"`), defaults `enabled=true`/`event_types=[]`; `WebhookEndpointUpdate` distinguishes omitted vs `null` `secret` via `model_dump(exclude_unset=True)`; `event_types` bounded (≤64 entries, each ≤200 chars) per data-model.md.
- [ ] T028 [US2] Integration test `tests/integration/test_api_webhook_endpoints.py` guarded by the existing `pytest.mark.skipif` DB probe (mirror `tests/integration/test_scrape_profiles_isolation_live.py`): create/list/get/patch/delete round-trip; reject each SSRF class (private/loopback/link-local/metadata/userinfo/non-http(s)) with `422 UNSAFE_URL` and no row persisted (SC-002); secret stored encrypted and never returned; cross-workspace list/get/patch/delete → absent/404 (SC-004); no-context → 0 rows; `webhooks:read`-only key refused on write ops → 403, permitted on reads (US2 AS6).

**Checkpoint**: US1 + US2 both work independently.

---

## Phase 5: User Story 3 - Events are automatically created on domain changes (Priority: P2)

**Goal**: A `create_webhook_event` Celery task on a new `webhook_events` queue, plus fire-and-forget POST-commit enqueue at the three seams (SPEC-09 alerts, SPEC-08 jobs, SPEC-12 strategy), using shared size-guarded payload builders. Never blocks/fails/rolls back the source op.

**Independent Test**: Trigger each source change (alert transition, terminal job status, strategy promotion/rediscovery) → exactly one event of the expected `event_type` with a descriptive payload appears in the correct workspace; re-triggering an identical signal produces no contradictory/malformed duplicate; a broker outage at the seam does not fail the committed source op.

### Implementation for User Story 3

- [ ] T029 [P] [US3] Create `libs/shared/app_shared/webhooks/payloads.py` (new package, scraping-free) with pure builders — one per source — that map source enum values to `(event_type, payload)` per contracts/events.md: `build_alert_event(...)` (AlertEventType→`price.alert.*`, payload with variant/product/alert_state ids + prev/new type+severity + transition), `build_job_event(...)` (ScrapeJobStatus terminal→`scrape.job.*`, payload with job id/status/counts/total), `build_strategy_event(...)` (promotion→ACTIVE / rediscovery→DEGRADED, both `domain.strategy.updated`, payload with profile id/domain/new_status/change/method). Include a shared size guard that asserts `json.dumps(payload)` is `< 8 KiB` (FR "very large payload"). Builders also compute the `dedup_key` strings from contracts/events.md.
- [ ] T030 [P] [US3] Unit test `tests/unit/test_webhook_payloads.py`: for each builder assert the correct `event_type` string and payload keys/values for every source enum member (incl. that `AlertEventType.UNCHANGED` and `ScrapeJobStatus.CANCELLED` produce NO event); assert `dedup_key` format; assert the `< 8 KiB` size guard raises on an oversized payload.
- [ ] T031 [US3] Create `apps/workers/app/workers/tasks_webhooks.py` with `@app.task(name=CREATE_WEBHOOK_EVENT)` `create_webhook_event(*, workspace_id, event_type, payload, dedup_key=None) -> None` (mirror `tasks_analysis.py::recompute_variant` shape): opens its own `with get_session() as session:` + `set_workspace_context(session, workspace_id)`, optional best-effort Redis `SET NX dedup_key` to collapse retries (mirror `pipelines.py`), inserts one `WebhookEvent(status=PENDING, delivered_at=None)`, commits. No outbound HTTP (FR-010/SC-007). Never imports source domain code.
- [ ] T032 [US3] Wire Celery in `apps/workers/app/workers/celery_app.py`: add `"webhook_events": {}` to `app.conf.task_queues`, add `CREATE_WEBHOOK_EVENT: {"queue": "webhook_events"}` to `app.conf.task_routes`, add `"app.workers.tasks_webhooks"` to `include=[...]`, and import the `CREATE_WEBHOOK_EVENT` constant.
- [ ] T033 [US3] Add the SPEC-09 seam in `apps/workers/app/workers/tasks_analysis.py::recompute_variant`: strictly AFTER the existing `session.commit()` (locate the post-commit seam by function, not line — `recompute_variant` starts ~L283; the `~line` hints in this file are approximate) and only when `event_type is not None`, build the payload via `build_alert_event(...)` and `enqueue(CREATE_WEBHOOK_EVENT, queue="webhook_events", kwargs={...})`, wrapped in a narrow `try/except` that logs and continues (broker error must never fail the committed op — FR-009/SC-005).
- [ ] T034 [US3] Add the SPEC-08 seam in `apps/workers/app/workers/tasks_jobs.py::finalize_jobs`: collect finalized `(job_id, status)` for terminal statuses (COMPLETED/PARTIAL_FAILED/FAILED; not CANCELLED) in the loop, then AFTER the single `session.commit()` (locate by function — `finalize_jobs`; anchors approximate) `enqueue` one `create_webhook_event` per finalized job via `build_job_event(...)`, wrapped in the same narrow try/except.
- [ ] T035 [US3] Add the SPEC-12 strategy seam. NOTE (analyze N1): the genuine status transitions do NOT surface in `flush_stats` — `apply_promotion` (→ ACTIVE, returns `promoted: bool`) and `apply_rediscovery` (→ DEGRADED, returns `triggered: bool`) both fire INSIDE `libs/shared/app_shared/strategy/flush.py::flush_profile` (promotion in the per-method loop ~L271, rediscovery ~L306), and `apply_rediscovery` ALSO fires in `apps/workers/app/workers/tasks_strategy.py::light_recheck` (~L665). Enqueue must be post-commit (FR-009/SC-005) and once per genuine transition (both apply_* already return True only on a real row change). Implement at BOTH real sites:
  - (a) Change `flush_profile` to accumulate genuine transitions (each: `profile_id`, `workspace_id`, new `StrategyStatus`, kind promote/rediscovery — only when the corresponding `apply_*` returned `True`) and return them to its caller (widen its `-> int` return to also carry the transition list, e.g. return `(keys_flushed, transitions)` or a small result object). Update `flush_profile`'s existing unit tests + any caller pinning the `int` return. Then in `flush_stats` (`apps/workers/app/workers/tasks_strategy.py`, ~L695), AFTER the single `session.commit()` (~L750), `enqueue(CREATE_WEBHOOK_EVENT, queue="webhook_events", kwargs=build_strategy_event(...))` once per collected transition, wrapped in the same narrow try/except (broker error never fails the committed flush).
  - (b) In `light_recheck` (~L635), the per-profile `triggered = apply_rediscovery(...)` (~L665) result is already in scope; collect `(profile_id, workspace_id)` for each `triggered` and, AFTER the loop's `session.commit()` (~L675), enqueue one `create_webhook_event` (`DOMAIN_STRATEGY_UPDATED`, DEGRADED) per triggered profile via `build_strategy_event(...)`, same narrow try/except.
  - Anchors approximate — locate by function name. This guarantees FR-008/SC-003 "each strategy change → exactly one event" across BOTH the flush and light-recheck rediscovery paths (the previously-missed `light_recheck` DEGRADED path is now covered).
- [ ] T036 [P] [US3] Unit test `tests/unit/test_webhook_enqueue_seams.py`: with `enqueue` and `get_session` monkeypatched/mocked (no live Celery/DB), assert each of the FOUR seam paths calls `enqueue(CREATE_WEBHOOK_EVENT, queue="webhook_events", kwargs=...)` exactly once per genuine transition with the expected `event_type`/payload — (1) alert `recompute_variant`, (2) job `finalize_jobs`, (3) strategy `flush_stats` post-commit for each transition surfaced by `flush_profile` (promotion→ACTIVE and rediscovery→DEGRADED), (4) strategy `light_recheck` post-commit for each `triggered` rediscovery (→DEGRADED). Assert the negative cases enqueue nothing: alert `event_type is None`, job `UNCHANGED`/`CANCELLED`, strategy `apply_promotion`/`apply_rediscovery` returning `False` (no genuine row change). Assert a raised broker error inside any seam is swallowed (source path completes, commit stands).
- [ ] T037 [US3] Integration test `tests/integration/test_webhook_event_creation_live.py` guarded by the existing `pytest.mark.skipif` DB probe: drive each seam (or invoke `create_webhook_event` directly) and assert exactly one `webhook_events` row of the expected type/payload in the correct workspace (SC-003), `status=PENDING`/`delivered_at is null` (SC-007), duplicate signal does not create a contradictory row, and (soft-ref/retention tolerance) that a poll still succeeds after an expired partition is dropped.

**Checkpoint**: All three stories independently functional; events now created live in production.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Guards, lint, and full-suite verification across all stories.

- [ ] T038 Run `uv run ruff check` and `uv run ruff format --check` over the new/edited files (`libs/shared/app_shared/{enums,task_names,repository,models/webhooks,models/__init__,webhooks/payloads}.py`, `apps/api/app/{routers,schemas}/webhooks.py`, `apps/api/app/main.py`, `apps/workers/app/workers/{tasks_webhooks,celery_app,tasks_analysis,tasks_jobs,tasks_strategy}.py`, migration, tests); fix all findings.
- [ ] T039 [P] Run the single-head guard `uv run pytest tests/unit/test_strategy_single_head.py` — must report exactly one Alembic head (linear chain, `down_revision='4a1dca402f78'`).
- [ ] T040 [P] Run the workspace-scoping guard `uv run python scripts/check_workspace_scoping.py` (and `tests/unit/` scoping tests) — confirms both `WebhookEndpoint` and `WebhookEvent` are in `WORKSPACE_OWNED_MODELS` (the script imports that set; there is no separate mirror list to keep in sync).
- [ ] T041 [P] Run the import-boundary guard `uv run pytest tests/unit/test_import_boundaries.py` — confirms `app_shared` (incl. new `webhooks/payloads.py`) pulls in no Scrapy/Twisted/Playwright/FastAPI and does not import `scrape_core`; seams enqueue by name only.
- [ ] T042 [P] Run the partition-registry / retention guards `uv run pytest tests/unit/test_partition_registry.py tests/unit/test_retention_eligibility.py` — confirm still green (`len(PARTITIONED_TABLES)==4`, `webhook_events: 90`) proving nothing was re-added.
- [ ] T043 Run the full suite `uv run pytest` (unit green; integration `webhook`/`live` cases skip cleanly via the DB probe in this build env) and confirm no regressions.
- [ ] T044 Execute `specs/016-webhook-events/quickstart.md` validation steps (or confirm they are all covered by the tests above) and note any live-only steps deferred for lack of a Postgres/Redis/Celery stack.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately. T004 (enums) blocks models/payloads/schemas; T005 (task name) blocks the Celery task/seams.
- **Foundational (Phase 2)**: Depends on Setup (needs the enums). BLOCKS all user stories. T007 (models) → T008/T009 (exports/scoping) → T010 (migration) → T011/T012/T013 (tests).
- **US1 (Phase 3, P1)**: Depends on Foundational. Independently testable. Creates `schemas/webhooks.py` + `routers/webhooks.py` + `main.py` mount (event endpoints).
- **US2 (Phase 4, P2)**: Depends on Foundational. Independently testable. EXTENDS the same `schemas/webhooks.py` + `routers/webhooks.py` (endpoint CRUD) — if US1 not yet done, create those files here instead; the two stories touch disjoint route handlers.
- **US3 (Phase 5, P2)**: Depends on Foundational (+ Setup T005). Independently testable. Adds `webhooks/payloads.py`, the Celery task, and the three seams. No dependency on US1/US2.
- **Polish (Phase 6)**: Depends on all targeted stories being complete.

### Within Each User Story

- Models before services/routers; schemas before route handlers; core route before integration test.
- Producer seams (T033–T035) depend on the payload builders (T029) and the task-name constant (T005), and on the Celery task existing (T031) for a live run — but the enqueue-by-name seam itself only needs the constant.

### Parallel Opportunities

- Setup: T005 and T006 are [P]; T001–T003 are read-only verifications that can run alongside.
- Foundational: T011, T012, T013 [P] after T010.
- US1: T018, T019 [P] (unit tests) alongside route work; T020 after routes.
- US2: T026, T027 [P] (unit/guard tests) alongside route work; T028 after routes.
- US3: T029/T030 [P] (builders + their test), T036 [P]; seams T033/T034/T035 touch different files and can be parallelized after T029.
- Once Foundational completes, **US1, US2, US3 can be built in parallel by different developers** (US1/US2 coordinate on the two shared api files; US3 is fully separate).
- Polish: T039–T042 [P] (independent guard runs); T038 then T043 sequential.

---

## Parallel Example: after Foundational, launch the three stories

```bash
# Developer A — US1 poll API (apps/api/app/{schemas,routers}/webhooks.py events + main mount)
# Developer B — US2 endpoint CRUD (same two api files, disjoint handlers + secret/SSRF)
# Developer C — US3 event creation (app_shared/webhooks/payloads.py, tasks_webhooks.py, 3 seams)

# Within US3, in parallel:
Task: "Create payload builders in libs/shared/app_shared/webhooks/payloads.py"
Task: "Unit test builders in tests/unit/test_webhook_payloads.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1 Setup → 2. Phase 2 Foundational (CRITICAL) → 3. Phase 3 US1 → **STOP & VALIDATE**: seed events, poll, confirm isolation + cursor. This is a demoable MVP (events pollable regardless of endpoints/auto-creation).

### Incremental Delivery

1. Setup + Foundational → foundation ready.
2. US1 → poll API (MVP).
3. US2 → endpoint registration + SSRF + encrypted secret.
4. US3 → automatic event creation at the three seams.
5. Polish → guards + full suite.

---

## Notes

- [P] = different files, no dependency on incomplete tasks.
- This build env has NO live Postgres/Redis/Celery: all `tests/integration/*webhook*`/`*live*` cases MUST use the existing `pytest.mark.skipif` DB probe and skip cleanly; unit tests must be fully green.
- NON-NEGOTIABLES: workspace isolation (RLS + app scoping, fail-closed 0 rows) and SSRF reuse (no second validator).
- Commit after each task or logical group; keep the single Alembic head linear (`down_revision='4a1dca402f78'`).
