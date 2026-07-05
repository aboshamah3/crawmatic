# Contract: Refresh Rules REST API (US1)

Workspace-scoped CRUD + enable/disable for `refresh_rules`. Mirrors the SPEC-05 competitors router
(`apps/api/app/routers/competitors.py`): `/v1` prefix, `require_scopes(...)` dependency yielding
`(session, principal)`, cursor pagination, `{"error":{"code","message"}}` envelope, per-request
RLS via `set_workspace_context` (committed by the `get_current_principal` dependency).

- Router: `apps/api/app/routers/refresh_rules.py`, `APIRouter(prefix="/v1/refresh-rules", tags=["refresh-rules"])`; registered in `apps/api/app/main.py`.
- Schemas: `apps/api/app/schemas/refresh_rules.py` (Pydantic v2, `extra="forbid"`).
- Scopes: `refresh_rules:read` / `refresh_rules:write` (same scope-string convention as competitors).

## Schemas

`RefreshRuleCreate` (POST body):
- `name: str` (non-empty)
- `scope: ScrapeScope`
- `product_id / product_variant_id / product_group_id / competitor_id / match_id: uuid.UUID | None = None`
- `cron_expression: str | None = None`
- `interval_minutes: int | None = None` (`ge=1`)
- `priority: int = 0`
- `enabled: bool = True`

Cross-field validation (also enforced by DB CHECKs, research R9):
- exactly one of `cron_expression` / `interval_minutes` → else `422 INVALID_CADENCE`.
- `cron_expression` parseable by croniter → else `422 INVALID_CRON`.
- scope↔target-id matrix (WORKSPACE ⇒ no target id; others ⇒ exactly their id) → else
  `422 SCOPE_TARGET_MISMATCH`.
- the supplied target id must resolve in-workspace via `scoped_get` → else `422 SCOPE_TARGET_MISMATCH`
  (cross-workspace / missing target is indistinguishable from "not yours", never leaks existence).

`RefreshRuleUpdate` (PATCH body): every field `| None = None`, `extra="forbid"`; empty body →
`422 EMPTY_UPDATE` (strategy.py precedent). Includes `enabled` — enable/disable is a PATCH field,
not a separate action subresource (repo has no `/enable` idiom). Re-running validation after applying
the patch keeps the cadence/scope invariants intact; changing cadence recomputes `next_run_at`.

`RefreshRuleResponse` (`from_attributes=True`): all columns incl. `id`, `scope`, target ids,
cadence, `priority`, `enabled`, `next_run_at`, `last_run_at`, `locked_at`, `created_at`,
`updated_at`.

`RefreshRuleListResponse`: `{ items: list[RefreshRuleResponse], next_cursor: str | None }`.

## Endpoints

| Method / path | Scope | Behavior |
|---|---|---|
| `POST /v1/refresh-rules` | write | validate; compute first `next_run_at` (research R1); insert with `workspace_id=principal.workspace_id`; `201`. `IntegrityError` (CHECK) → `409`/`422`. |
| `GET /v1/refresh-rules` | read | `scoped_select(RefreshRule, ws)`, keyset on `(created_at, id)`, `clamp_limit`, `paginate`; `{items,next_cursor}`. |
| `GET /v1/refresh-rules/{id}` | read | `scoped_get`; `None` → `404`. |
| `PATCH /v1/refresh-rules/{id}` | write | `scoped_get`→404; `model_dump(exclude_unset=True)`; re-validate cadence/scope; recompute `next_run_at` if cadence changed; `flush`. |
| `DELETE /v1/refresh-rules/{id}` | write | `scoped_get`→404; `session.delete`; `DeleteOutcome(id, outcome="hard_deleted")`. |

Enable/disable = `PATCH {"enabled": false|true}`. A rule disabled mid-flight is simply not claimed
next pass (spec Edge Cases). Disabling does not clear `next_run_at`.

## Acceptance mapping

- US1 AS-1/2 → POST computes `enabled=true` + first `next_run_at`.
- US1 AS-3 → PATCH `enabled=false` persists, never claimed.
- US1 AS-4 / SC-004 → RLS + `scoped_*` ⇒ cross-workspace invisibility (cross-workspace denial test).
- US1 AS-5 → `INVALID_CADENCE` on neither/both cadence.
- US1 AS-6 → `SCOPE_TARGET_MISMATCH` on missing/cross-workspace target id.

## Error codes

`INVALID_CADENCE`, `INVALID_CRON`, `SCOPE_TARGET_MISMATCH`, `EMPTY_UPDATE`, `INVALID_CURSOR`
(existing) — all in the structured `{"error":{"code","message"}}` envelope.
