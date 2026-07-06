# Phase 1 Data Model: Webhook Events

Two workspace-owned tables + two new StrEnums. Grounded in existing patterns (see research.md).
Module: `libs/shared/app_shared/models/webhooks.py`; enums in `libs/shared/app_shared/enums.py`.

---

## Enums (new, `app_shared/enums.py`)

### `WebhookEventStatus(StrEnum)`
```
PENDING    = "PENDING"     # v1: recorded, not delivered (default). delivered_at is null.
DELIVERED  = "DELIVERED"   # reserved for the future delivery feature (unused in v1)
FAILED     = "FAILED"      # reserved for the future delivery feature (unused in v1)
```
v1 only ever writes `PENDING` (FR-011). Stored in a `String(32)` column.

### `WebhookEventType(StrEnum)` — stable event-type strings (FR-008; master doc §22)
```
PRICE_ALERT_CREATED    = "price.alert.created"      # AlertEventType.CREATED
PRICE_ALERT_UPDATED    = "price.alert.updated"      # AlertEventType.UPDATED
PRICE_ALERT_RESOLVED   = "price.alert.resolved"     # AlertEventType.RESOLVED
PRICE_ALERT_REOPENED   = "price.alert.reopened"     # AlertEventType.REOPENED
SCRAPE_JOB_COMPLETED   = "scrape.job.completed"     # ScrapeJobStatus.COMPLETED
SCRAPE_JOB_PARTIAL     = "scrape.job.partial_failed"# ScrapeJobStatus.PARTIAL_FAILED
SCRAPE_JOB_FAILED      = "scrape.job.failed"        # ScrapeJobStatus.FAILED
DOMAIN_STRATEGY_UPDATED= "domain.strategy.updated"  # StrategyStatus ACTIVE (promo) / DEGRADED (rediscovery)
```
Stored in a `String(64)` column (free string, producer-validated by this enum). Endpoint
`event_types` is a free JSONB list of arbitrary strings (forward-compatible; unknown types are
permitted and simply never match — edge case).

---

## Entity: `webhook_endpoints` (plain, tenant-only)

Mirrors `refresh_rules` (SPEC-13). `Base + WorkspaceScopedBase + TimestampMixin`.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `Uuid` | no | PK, uuidv7 (`Base.id`, `default=new_uuid7`) |
| `workspace_id` | `Uuid` | no | FK → `workspaces.id`; indexed (`WorkspaceScopedBase`); RLS anchor |
| `name` | `String(200)` | no | human label |
| `url` | `Text` | no | **SSRF-validated at create+update** via `validate_competitor_url` (FR-002/003) |
| `secret_encrypted` | `Text` | yes | Fernet ciphertext; unused in v1; never returned raw (FR-005) |
| `secret_key_version` | `Integer` | yes | companion key version for decrypt/rotation |
| `enabled` | `Boolean` | no | default `true` |
| `event_types` | `JSONB` | no | list[str]; default `[]`; subscription intent only in v1 |
| `created_at` | `TZDateTime` | no | `TimestampMixin` |
| `updated_at` | `TZDateTime` | no | `TimestampMixin` (`onupdate` → advances on update, FR-004) |

- Indexes: `ix_webhook_endpoints_workspace_id` (from `WorkspaceScopedBase`).
- RLS: `emit_rls_policy("webhook_endpoints")` in the migration.
- Constraints: no FK other than `workspace_id`. (Optional future: `unique(workspace_id, name)` — not
  required by spec; omit in v1 to avoid over-constraining.)
- Validation rules:
  - `url` → `validate_competitor_url(url)`; `UnsafeUrlError` → `422 UNSAFE_URL` (nothing persisted).
  - `event_types` → list of strings, bounded length (e.g. ≤ 64 entries), each ≤ 200 chars.
  - `secret` (request-only, plaintext) → encrypted via `encrypt_secret` before persist; never stored
    or echoed in plaintext.

## Entity: `webhook_events` (born monthly-partitioned)

Mirrors `price_alert_events` (SPEC-09). `Base + WorkspaceScopedBase`, **no `TimestampMixin`** because
`created_at` is a PK column.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | `Uuid` | no | PK part 1, uuidv7 |
| `created_at` | `TZDateTime` | no | **PK part 2 = partition key**; `mapped_column(TZDateTime(), primary_key=True)` |
| `workspace_id` | `Uuid` | no | FK → `workspaces.id`; indexed; RLS anchor |
| `event_type` | `String(64)` | no | one of `WebhookEventType` (FR-008) |
| `payload` | `JSONB` | no | bounded change descriptor (FR-006; size-guarded by builder) |
| `status` | `String(32)` | no | `WebhookEventStatus`, default `PENDING` (FR-011) |
| `delivered_at` | `TZDateTime` | yes | always null in v1 (FR-010, SC-007) |

- PK: `PrimaryKeyConstraint("id", "created_at", name="pk_webhook_events")`.
- Partitioning: `{"postgresql_partition_by": "RANGE (created_at)"}`; migration creates current +
  next month child partitions `webhook_events_YYYY_MM` and emits RLS on the parent (propagates).
- FKs: **only** `workspace_id`. Payload references to variant/job/strategy are **soft references**
  (no FK into or out of the partitioned table) — FR-019, Constitution §22.
- Indexes (for keyset poll + event_type filter):
  - `ix_webhook_events_workspace_id` (from `WorkspaceScopedBase`).
  - `ix_webhook_events_ws_created_id` on `(workspace_id, created_at, id)` — supports the
    `ORDER BY (created_at, id)` keyset scan within a workspace.
  - `ix_webhook_events_ws_type_created` on `(workspace_id, event_type, created_at)` — supports the
    `event_type` filter + ordering (FR-014). (Both indexes are created on the parent and inherited.)
- Retention: 90 days, by monthly partition drop through the **already-registered** SPEC-15
  maintenance job (`RETENTION_WEBHOOK_EVENTS_DAYS=90`). No new job/scheduler (FR-018, SC-006).
- Immutability: rows are append-only; no update path in v1 except the future delivery feature
  setting `status`/`delivered_at`.

---

## Relationships (soft references only)

```
workspaces 1───* webhook_endpoints        (hard FK: workspace_id)
workspaces 1───* webhook_events            (hard FK: workspace_id)

webhook_events.payload  ─ soft ─>  product_variants / variant_alert_states  (alert.* events)
webhook_events.payload  ─ soft ─>  scrape_jobs                              (scrape.job.* events)
webhook_events.payload  ─ soft ─>  domain_strategy_profiles                 (domain.strategy.* events)
webhook_endpoints.event_types ─ soft ─> WebhookEventType strings (intent only; no FK)
```

No hard FK crosses into `webhook_events` (partitioned); readers tolerate references into dropped
(expired) partitions (FR-019).

---

## Registration checklist (must-do to satisfy isolation + partitioning)

- [ ] `WebhookEndpoint`, `WebhookEvent` exported in `app_shared/models/__init__.py`.
- [ ] Both added to `app_shared/repository.py::WORKSPACE_OWNED_MODELS` (CI guard
      `scripts/check_workspace_scoping.py` mirrors this set — keep them in sync).
- [ ] `emit_rls_policy(...)` emitted for **both** tables in the migration.
- [ ] Migration `down_revision = '4a1dca402f78'`; single-head guard stays green.
- [ ] Do **NOT** touch `PARTITIONED_TABLES` (webhook_events already there) or the scope enum
      (`webhooks:read`/`webhooks:write` already there).

## Payload shapes (built by `app_shared/webhooks/payloads.py`)

See contracts/events.md for concrete JSON per `event_type`. All payloads: small, JSON-serializable,
size-guarded (< 8 KiB serialized), and identify the affected entity + the nature of the change
(FR-008, edge case "very large payload").
