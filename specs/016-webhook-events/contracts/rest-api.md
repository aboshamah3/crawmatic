# Contract: Webhook REST API (`/v1`)

Router: `apps/api/app/routers/webhooks.py` (new). Schemas: `apps/api/app/schemas/webhooks.py` (new).
Mounted in `apps/api/app/main.py`. All routes require the workspace-scoped principal
(`get_current_principal`) and a scope via `require_scopes(...)`. Cross-workspace access returns
404 (get) / absent (list); no-workspace-context session returns 0 rows (fail-closed).

Error envelope (existing convention): `4xx {"error": {"code": "<CODE>", "message": "<msg>", ...}}`.

---

## Webhook endpoints (CRUD)

### `POST /v1/webhook-endpoints` — scope `webhooks:write`
Request (`extra="forbid"`):
```json
{
  "name": "My integration",
  "url": "https://hooks.example.com/crawmatic",
  "enabled": true,
  "event_types": ["price.alert.created", "scrape.job.failed"],
  "secret": "optional-plaintext-shared-secret"
}
```
Behavior: validate `url` via `validate_competitor_url` → on `UnsafeUrlError` return
`422 {"error":{"code":"UNSAFE_URL","message":...,"reason":"<UnsafeUrlReason>"}}`, persist nothing.
If `secret` present, `encrypt_secret` → store `secret_encrypted`/`secret_key_version`.
Response `201` = `WebhookEndpointResponse` (below).

### `GET /v1/webhook-endpoints` — scope `webhooks:read`
Query: `limit?` (default 50, max 500), `cursor?` (keyset over `(created_at, id)`).
Response `200`: `{ "items": [WebhookEndpointResponse...], "next_cursor": string|null }`.
Only the caller's workspace rows.

### `GET /v1/webhook-endpoints/{id}` — scope `webhooks:read`
`scoped_get(WebhookEndpoint, id, workspace_id)`. `200` = `WebhookEndpointResponse`; not in workspace
→ `404 {"error":{"code":"NOT_FOUND",...}}`.

### `PATCH /v1/webhook-endpoints/{id}` — scope `webhooks:write`
Request (all optional, tri-state via `model_dump(exclude_unset=True)`):
```json
{ "name": "...", "url": "https://...", "enabled": false,
  "event_types": ["..."], "secret": "new-secret" | null }
```
- `url` present → re-validate (same `UNSAFE_URL` rule).
- `secret`: omitted = unchanged; `null` = clear (`secret_encrypted`/`secret_key_version` → NULL);
  non-null = re-encrypt.
- `updated_at` advances (FR-004). `200` = `WebhookEndpointResponse`. Not in workspace → `404`.

### `DELETE /v1/webhook-endpoints/{id}` — scope `webhooks:write`
Deletes the row. `204` on success; not in workspace → `404`. Subsequent get/list → absent.

### `WebhookEndpointResponse` (never exposes the secret — FR-005)
```json
{
  "id": "uuid",
  "workspace_id": "uuid",
  "name": "string",
  "url": "string",
  "enabled": true,
  "event_types": ["string", "..."],
  "has_secret": true,
  "created_at": "2026-07-06T12:00:00Z",
  "updated_at": "2026-07-06T12:00:00Z"
}
```
`has_secret` is a derived boolean (`secret_encrypted is not None`), built by **explicit field
mapping** (never `model_validate(orm_obj)`). `secret_encrypted`/`secret_key_version` are never
serialized (guard test enforces this).

---

## Webhook events (poll)

### `GET /v1/webhook-events` — scope `webhooks:read`
Query:
- `limit?` int (default 50, max 500, min 1 — `clamp_limit`).
- `cursor?` opaque keyset token; invalid/stale → `422 {"error":{"code":"INVALID_CURSOR",...}}`
  (FR-015).
- `event_type?` string filter (FR-014).

Behavior: `scoped_select(WebhookEvent, workspace_id)`, optional
`where(WebhookEvent.event_type == event_type)`, optional `keyset_predicate`, `order_by(created_at, id)`,
`limit + 1`, then `paginate`. Deterministic, gapless, stable across month boundaries (SC-001).
Response `200`:
```json
{ "items": [WebhookEventResponse...], "next_cursor": "string|null" }
```
Empty workspace / past-the-end → `{ "items": [], "next_cursor": null }` (not an error — edge case).

### `GET /v1/webhook-events/{id}` — scope `webhooks:read`
`scoped_get(WebhookEvent, id, workspace_id)` (id-only lookup on the partitioned PK is fine; RLS +
app scope enforce workspace). `200` = `WebhookEventResponse`; not in workspace → `404` (FR-015).

### `WebhookEventResponse`
```json
{
  "id": "uuid",
  "event_type": "price.alert.created",
  "payload": { "...": "..." },
  "status": "PENDING",
  "created_at": "2026-07-06T12:00:00Z",
  "delivered_at": null
}
```
`delivered_at` is always `null` in v1 (SC-007). `workspace_id` MAY be included but is redundant
(always the caller's).

---

## Scope + isolation summary

| Route | Scope | Isolation |
|---|---|---|
| `POST/PATCH/DELETE /v1/webhook-endpoints*` | `webhooks:write` | RLS + `scoped_*`; 404 cross-ws |
| `GET /v1/webhook-endpoints*` | `webhooks:read` | RLS + `scoped_*`; absent/404 cross-ws |
| `GET /v1/webhook-events*` | `webhooks:read` | RLS + `scoped_*`; absent/404 cross-ws |

A key lacking the required scope → `403` (`require_scopes`). No workspace context → 0 rows.
`webhooks:read` alone permits only reads (FR-017 / US2 AS6).
