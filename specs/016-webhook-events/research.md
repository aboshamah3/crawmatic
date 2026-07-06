# Phase 0 Research: Webhook Events (reuse map)

This is a reuse-heavy spec. Every decision below is grounded in existing code (absolute paths +
line numbers verified during planning). No NEEDS CLARIFICATION remain.

---

## R1 — SSRF / URL-safety validator (FR-002, FR-003; Constitution VI)

**Decision**: Reuse `validate_competitor_url` from
`/srv/crawmatic/crawmatic/libs/shared/app_shared/url_safety.py` (line 93) for
`webhook_endpoints.url` at both create and update. Do not write a new validator.

- Signature: `def validate_competitor_url(url: str) -> None` — returns `None` when safe, raises
  `UnsafeUrlError(ValueError)` (lines 59–69) whose `.reason` is an `UnsafeUrlReason` StrEnum
  (`INVALID_URL`, `BAD_SCHEME`, `USERINFO_PRESENT`, `PRIVATE_OR_INTERNAL_IP`, `INTERNAL_HOSTNAME`).
- Synchronous, stdlib-only (`urllib.parse` + `ipaddress`), scraping-free → correct for `app_shared`.
- Rejects: non-http(s) scheme, userinfo (`user:pass@host`), localhost, 10/8, 172.16/12, 192.168/16,
  loopback, link-local 169.254 + `fe80::/10`, unique-local `fc00::/7`, cloud metadata
  169.254.169.254 / `metadata.google.internal`, reserved/multicast/unspecified, and internal
  docker/railway hostnames.
- **Known limitation (matches SPEC-05 behavior):** no DNS resolution — a public hostname that
  *resolves* to a private IP is caught only at fetch/delivery time. Since v1 has **no delivery**,
  save-time string+IP-literal validation is the full FR-002 surface; the resolved-IP re-check
  (`scrape_core/safety/fetch.py::validate_resolved_target`) is deferred with the delivery feature
  (spec Assumptions). The reusable resolved-IP primitive `app_shared.url_safety._reject_ip(ip)` is
  available in `app_shared` if delivery-time checks are added later without pulling in scrape-core.

**Error contract to mirror** (from `apps/api/app/routers/matches.py:101–105`): map `UnsafeUrlError`
to `422 {"error": {"code": "UNSAFE_URL", "message": str(exc), "reason": exc.reason.value}}`.

**Alternatives rejected**: a webhook-specific validator (violates FR-003 and Constitution VI —
"no second, divergent validator").

Call-site precedents to copy: `matches.py:257` (create), `matches.py:533` (update),
`proxy_providers.py:100/199` (base_url create/update).

---

## R2 — SPEC-15 maintenance registry / 90-day retention (FR-018, FR-019; Constitution VIII)

**Decision**: Do **nothing** to the registry except create the real table. `webhook_events` is
**already registered**.

- `/srv/crawmatic/crawmatic/libs/shared/app_shared/maintenance/registry.py` `PARTITIONED_TABLES`
  (lines 48–76) already contains:
  ```python
  PartitionedTable(name="webhook_events", partition_key="created_at",
                   feeds_rollups=False, retention_setting="RETENTION_WEBHOOK_EVENTS_DAYS")
  ```
- `/srv/crawmatic/crawmatic/libs/shared/app_shared/config.py:234` already defines
  `RETENTION_WEBHOOK_EVENTS_DAYS: int = 90`.
- Partition-create (`maintenance/partitions.py::create_missing_partitions`) and retention-drop
  (`maintenance/retention.py::run_retention`) both iterate `PARTITIONED_TABLES` and skip absent
  tables via `table_exists` (a `to_regclass` probe). Once the real `webhook_events` parent exists,
  both jobs pick it up automatically — current + next month partitions created, and whole monthly
  partitions older than 90 days dropped (never bulk DELETE).
- A single scheduler cadence (`PARTITION_CREATE_INTERVAL_SECONDS`, `RETENTION_INTERVAL_SECONDS`)
  drives one global task each — **no new Celery beat/scheduler entry needed** (FR-018, SC-006).

**Constraint**: `tests/unit/test_partition_registry.py` asserts `len(PARTITIONED_TABLES) == 4` and
pins the `webhook_events` entry; `tests/unit/test_retention_eligibility.py` expects
`webhook_events: 90`. **Adding a registry entry would break these** — the registration is already
done. Soft references only into the partitioned events table (FR-019); readers tolerate dropped
partitions.

**Alternatives rejected**: adding a new maintenance job / scheduler entry (violates FR-018);
DELETE-based retention (violates Constitution VIII "partition drop, never bulk DELETE").

---

## R3 — Born-partitioned monthly table pattern (FR-006, FR-007; Constitution VIII)

**Decision**: Model `webhook_events` exactly like `price_alert_events` (SPEC-09, the closest
`created_at`-partitioned template) and `webhook_endpoints` like a plain tenant table
(`refresh_rules`, SPEC-13).

Pattern (verified in `models/alerts.py::PriceAlertEvent` + migration `e4a75b48360c`):

- Model: `class WebhookEvent(Base, WorkspaceScopedBase)` — `Base` supplies uuidv7 `id`
  (`default=new_uuid7`), `WorkspaceScopedBase` supplies indexed non-null `workspace_id`.
- Partition key is the second PK column, declared explicitly (NOT via `TimestampMixin`):
  `created_at: Mapped[datetime] = mapped_column(TZDateTime(), primary_key=True)` → composite
  `PRIMARY KEY (id, created_at)`.
- `__table_args__` tuple ends with `{"postgresql_partition_by": "RANGE (created_at)"}`, preceded by
  `ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_webhook_events_workspace_id_workspaces")`.
  Only `workspace_id` gets a real FK (RLS anchor); all other ids are soft references (no FK) per
  Constitution §22 (retention tolerance).
- Migration (`op.create_table(...)` with `sa.PrimaryKeyConstraint("id","created_at", name="pk_webhook_events")`
  + `postgresql_partition_by="RANGE (created_at)"`), then copy the module-level
  `_month_partition_bounds(base)` helper verbatim (current + next month) and
  `op.execute(f"CREATE TABLE webhook_events_{suffix} PARTITION OF webhook_events FOR VALUES FROM ('{start}') TO ('{end}');")`.
  `downgrade()` drops partitions then the parent.
- RLS emitted once on the parent (propagates to partitions):
  `for stmt in emit_rls_policy("webhook_events"): op.execute(stmt)` from `app_shared.models.rls`.

**Current single Alembic head = `4a1dca402f78`** (`variant_price_daily_rollups`, SPEC-15). Verified
no migration declares `down_revision='4a1dca402f78'`, so it is the sole leaf. The new migration sets
`down_revision = '4a1dca402f78'`. The single-head guard
`tests/unit/test_strategy_single_head.py::test_alembic_heads_reports_exactly_one_head` must stay
green (linear chain). Add `tests/unit/test_migration_offline_webhooks.py` mirroring
`test_migration_offline_alerts.py` (asserts CREATE TABLE, RLS trio, single head).

Helpers: `models/base.py` (`Base`, `TZDateTime`, `TimestampMixin`, `WorkspaceScopedBase`,
`NAMING_CONVENTION`), `ids.py` (`new_uuid7`), `models/rls.py` (`emit_rls_policy`). Register both
models in `repository.py::WORKSPACE_OWNED_MODELS` and import in `models/__init__.py`.

`webhook_endpoints` is **not** partitioned: plain `Base + WorkspaceScopedBase + TimestampMixin`
(single-column `id` PK, `created_at`/`updated_at` with `onupdate`), like `refresh_rules`.

**Alternatives rejected**: creating `webhook_events` plain and converting later (FR-007 forbids —
table rewrite under data); single-column `id` PK on the partitioned table (Postgres requires the
partition key in the PK).

---

## R4 — Workspace scoping + RLS + scope catalog (FR-016, FR-017; Constitution II)

**Decision**: Reuse the SPEC-03 stack end-to-end; the `refresh_rules` router (SPEC-13) is the
closest template.

- **Scopes already exist** — `WEBHOOKS_READ = "webhooks:read"`, `WEBHOOKS_WRITE = "webhooks:write"`
  in `app_shared/security/scopes.py:31–32`. **No enum edit** (the historical "scope missing from
  enum" bug does not recur here). `tests/unit/test_scopes.py` already references them.
- Enforce on routes with `require_scopes("webhooks:read")` / `require_scopes("webhooks:write")`
  from `apps/api/app/deps.py:321` (note: plural name, takes the raw string). It returns
  `(session, principal)`; `principal.workspace_id` is the scoping key.
- Workspace context: `get_current_principal` (`deps.py:266`) resolves+authorizes exactly one
  workspace, opens the transaction, and calls `set_workspace_context(session, workspace_id)`
  (`database.py:88`, `SELECT set_config('app.workspace_id', :wsid, true)` — LOCAL, PgBouncer-safe).
- App-level scoping: `scoped_select(model, workspace_id)` / `scoped_get(session, model, id, workspace_id)`
  (`repository.py:110/121`); add both models to `WORKSPACE_OWNED_MODELS` (single source of truth,
  mirrored by CI guard `scripts/check_workspace_scoping.py`).
- Fail-closed to 0 rows (no-context): DB RLS predicate `workspace_id = NULLIF(current_setting('app.workspace_id',true),'')::uuid`
  maps unset/empty context to NULL → matches zero rows; app layer raises if a workspace-owned model
  is queried without `workspace_id`.
- Router registration: add `webhooks` to the import block and `app.include_router(webhooks.router)`
  in `apps/api/app/main.py` (each router writes full `/v1/...` paths inline, no `prefix=`).

**Alternatives rejected**: PK-only lookups (violates Constitution II); a bespoke scope check.

---

## R5 — Pagination (FR-013, FR-014, FR-015; SC-001)

**Decision**: Reuse `app_shared/pagination.py` (SPEC-04) — opaque keyset cursor over `(created_at, id)`.

- `clamp_limit(requested)` → default 50, max 500, min 1 (`DEFAULT_LIMIT=50`, `MAX_LIMIT=500`).
- `encode_cursor(created_at, id)` / `decode_cursor(token)` (base64url JSON); `decode_cursor` raises
  `InvalidCursor(ValueError)` on malformed input → map to 422 (mirror
  `alerts.py::_invalid_cursor`, FR-015).
- `keyset_predicate(model, after)` → `tuple_(created_at, id) > tuple_(c, id)`; order by
  `(created_at, id)`, fetch `limit + 1`, then `paginate(rows, limit)` → `{items, next_cursor}`.
- Cross-partition stability: ordering by `(created_at, id)` (both PK columns) is monotonic across
  monthly partitions, so a page that spans a month boundary stays correct and gapless (SC-001, edge
  case "events spanning a month boundary"). A poller that already advanced past a now-dropped
  partition is unaffected (edge case "polling during retention drop").

Template: `apps/api/app/routers/alerts.py::list_alert_events` (lines 102–128) +
`schemas/alerts.py::AlertEventListResponse` (`{items, next_cursor}`).

---

## R6 — Event-creation seams + Celery wiring (FR-008, FR-009; Constitution I, V)

**Decision**: New task `create_webhook_event` on a new `webhook_events` queue, enqueued
fire-and-forget after each source `session.commit()` via `app_shared.messaging.enqueue`.

Celery config (`apps/workers/app/workers/celery_app.py`):
- Add `"webhook_events": {}` to `app.conf.task_queues` (currently absent — lines 102–107).
- Add `CREATE_WEBHOOK_EVENT: {"queue": "webhook_events"}` to `app.conf.task_routes` (lines 108–120).
- Add `"app.workers.tasks_webhooks"` to `include=[...]` (lines 57–62) and import the constant.
- Task name constant in `app_shared/task_names.py`:
  `CREATE_WEBHOOK_EVENT = "webhook_events.create_webhook_event"`.
- Task shape mirrors `recompute_variant` (`tasks_analysis.py:282`): plain `@app.task(name=CREATE_WEBHOOK_EVENT)`
  taking JSON-serializable string kwargs, opening its own `with get_session() as session:` and
  `set_workspace_context(...)`, no decorator-level queue/retry.
- Enqueue via `enqueue(CREATE_WEBHOOK_EVENT, queue="webhook_events", kwargs={...})`
  (`app_shared/messaging.py:39`) — `send_task` by name, awaits no result, never imports the worker
  task (preserves Constitution I boundary).

Seams (each strictly after the existing commit; wrapped in narrow try/except so a broker error
cannot fail the source op — FR-009 / SC-005):
- **SPEC-09 alerts** — `tasks_analysis.py::recompute_variant`, after `session.commit()` (line 380),
  only when `event_type is not None` (CREATED/UPDATED/RESOLVED/REOPENED). Payload carries workspace,
  variant, product, alert_state id, prev/new type+severity.
- **SPEC-08 jobs** — `tasks_jobs.py::finalize_jobs`, collect finalized `(job_id, status)` in the
  per-job loop, enqueue after the single `session.commit()` (line 401) for each terminal status
  (COMPLETED/PARTIAL_FAILED/FAILED). Payload carries workspace, job id, status, counts.
- **SPEC-12 strategy** — `tasks_strategy.py::flush_stats` after commit (line 750) when
  `apply_promotion` returned `promoted=True` (→ ACTIVE); and on `apply_rediscovery` (→ DEGRADED,
  `rediscovery.py:435`). Payload carries workspace, strategy profile id, new status, method/domain.

Source enums (verbatim, `app_shared/enums.py`): `AlertEventType` (391–403),
`ScrapeJobStatus` (297–311), `StrategyStatus` (444–460). See contracts/events.md for the mapping.

**Alternatives rejected**: enqueue before commit (races the source rows / could emit an event for a
rolled-back change); importing the worker task from the producer (violates Constitution I);
synchronous event creation inside the source op (violates FR-009 "must not block").

---

## R7 — Encrypted secret (FR-005; Constitution Tech/Security)

**Decision**: Reuse the versioned Fernet convention from proxy providers (SPEC-10). Store two
columns `secret_encrypted: Text NULL` + `secret_key_version: Integer NULL`; accept plaintext
`secret` on create/update, encrypt before persist, expose only a derived `has_secret: bool`.

- `app_shared/security/encryption.py`: `encrypt_secret(plaintext) -> EncryptedSecret(ciphertext, key_version)`,
  `decrypt_secret(ciphertext, key_version) -> str` (raises `SecretDecryptionError`, never returns
  raw), `reencrypt_secret(...)` for rotation. Keys from `ENCRYPTION_KEYS` env,
  `ENCRYPTION_PRIMARY_KEY_VERSION`.
- Column precedent: `ProxyProvider.password_encrypted`/`password_key_version` (`models/access.py:88–89`).
- Response precedent: `ProxyProviderResponse` omits ciphertext columns and exposes
  `has_password: bool` built by **explicit field mapping** in `_to_response` (never
  `model_validate(orm_obj)`), structurally preventing serialization of the secret. For webhooks name
  it `has_secret`. Add a guard test mirroring `tests/unit/test_access_guards.py`
  (`FORBIDDEN` = {`secret`, `secret_encrypted`, `secret_key_version`}).
- Update semantics (tri-state via `model_dump(exclude_unset=True)`): omitted = unchanged,
  `null` = clear, non-null = re-encrypt (mirror `proxy_providers.py` update).

Secret is stored but **unused in v1** (no delivery, no signing) — FR-005 / FR-010.

**Alternatives rejected**: storing plaintext (violates FR-005 / Constitution §33); returning the
secret in responses; a single ciphertext column without `key_version` (breaks the established
rotation story).

---

## Open questions

None. All clarify-deferred decisions are resolved in plan.md "Key design decisions".
