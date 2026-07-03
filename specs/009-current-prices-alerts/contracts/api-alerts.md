# Contract: Read endpoints (FR-017..020)

All under `/v1`, on the SPEC-03 auth seam (`app.deps.get_current_principal` →
`set_workspace_context` already applied to the yielded session), scope-gated via
`app.deps.require_scopes("alerts:read")` (the `Scope.ALERTS_READ` member already exists — no
new scope, no scope migration). Every read goes through `app_shared.repository.scoped_select`
/ `scoped_get` with RLS as the second isolation layer. Routers never import `apps/workers`.

Error envelope reuses the repo shape: `{"error": {"code": "...", "message": "..."}}`;
`NOT_FOUND` → 404, `INVALID_CURSOR` → 422 (via `pagination.InvalidCursor`).

## `GET /v1/variants/{variant_id}/price-comparison` — FR-017

- Router: `apps/api/app/routers/variants.py` (existing `/v1/variants` prefix).
- Scope: `alerts:read`.
- Reads `variant_price_states` for `(workspace_id, variant_id)`; **404** if the variant is
  unknown or cross-workspace, **or** if no price state exists yet (documented: 404 with a
  "no comparison computed yet" message — a variant that has never been analyzed). Reuse the
  unscoped-lookup vs scoped-get pattern to distinguish 404 from cross-workspace.
- Response (`schemas/alerts.py::PriceComparisonResponse`):

```json
{
  "product_variant_id": "…",
  "client_price": "2999.0000",
  "currency": "SAR",
  "cheapest_competitor_price": "2799.0000",
  "average_competitor_price": "2899.0000",
  "highest_competitor_price": "3099.0000",
  "comparable_competitor_count": 3,
  "alert_type": "HIGH_PRICE",
  "alert_severity": "HIGH",
  "calculated_at": "2026-07-03T12:00:00Z"
}
```

Money serialized as decimal strings (repo convention); nullable benchmarks → `null`.

## `GET /v1/alerts/current` — FR-018 (list)

- Router: `apps/api/app/routers/alerts.py`.
- Scope: `alerts:read`. Cursor-paginated (`app_shared.pagination`: `clamp_limit`,
  `decode_cursor`, `keyset_predicate`, `paginate`; cursor over `(created_at, id)`).
- Query params: `limit` (default 50, max 500), `cursor`, optional `type` (`AlertType`),
  optional `severity` (`AlertSeverity`).
- Builds `scoped_select(VariantAlertState, ws)`, adds `type`/`severity` `WHERE` filters when
  present, adds `keyset_predicate` when `cursor` present,
  `.order_by(VariantAlertState.created_at, VariantAlertState.id).limit(limit + 1)`, then
  `paginate(...)`.
- Response envelope `{ "items": [AlertStateResponse, …], "next_cursor": str | null }`.
  `AlertStateResponse`: `product_variant_id, type, severity, status, client_price,
  benchmark_price, cheapest_competitor_price, average_competitor_price, message, details,
  first_seen_at, last_seen_at, resolved_at`.
- Invalid `type`/`severity` value → 422; malformed cursor → 422.

## `GET /v1/alerts/current/{variant_id}` — FR-018 (single)

- Scope: `alerts:read`. `scoped_get(VariantAlertState by unique(ws, variant))`; 404 if none.
- Response: `AlertStateResponse`.

## `GET /v1/alert-events` — FR-019

- Router: `apps/api/app/routers/alerts.py`.
- Scope: `alerts:read`. Cursor-paginated over `price_alert_events` `(created_at, id)`.
- Query params: `limit`, `cursor`, optional `variant_id` filter.
- `scoped_select(PriceAlertEvent, ws)` + optional `product_variant_id == variant_id`,
  `keyset_predicate`, `.order_by(created_at, id).limit(limit+1)`, `paginate(...)`.
- Response envelope `{ "items": [AlertEventResponse, …], "next_cursor": … }`.
  `AlertEventResponse`: `id, product_variant_id, alert_state_id, event_type, previous_type,
  new_type, previous_severity, new_severity, message, details, created_at`.

## Registration

- `apps/api/app/main.py` includes the new `alerts` router. `price-comparison` is added to the
  already-included `variants` router.
- `apps/api/app/schemas/alerts.py` holds `PriceComparisonResponse`, `AlertStateResponse`,
  `AlertStateListResponse` (`{items, next_cursor}`), `AlertEventResponse`,
  `AlertEventListResponse`.

## Acceptance (FR-020, SC-005, SC-008)

- price-comparison returns the stored `variant_price_states` values consistently (SC-005);
  unknown/cross-workspace/never-analyzed variant → 404.
- `alerts/current` filters by type/severity and pages deterministically; `alert-events`
  filters by variant and pages.
- **Workspace isolation**: a caller with workspace A never sees B's price state, alert
  state, or events; a no-workspace-context request yields zero rows (RLS). Cross-workspace
  tests required for every endpoint (Principle II).
- Missing `alerts:read` scope → 403 on every endpoint.
