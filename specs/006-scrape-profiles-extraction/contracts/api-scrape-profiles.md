# Contract: `/v1/scrape-profiles` API (`apps/api/app/routers/scrape_profiles.py` + `schemas/scrape_profiles.py`)

Scope-gated CRUD + bulk-upsert + workspace-default assignment. Runs on the SPEC-03 auth seam (`require_scopes`, `set_workspace_context` already applied). Pydantic schemas live in `apps/api` (never `app_shared`).

## Scopes

`scrape_profiles:read` (GET), `scrape_profiles:write` (POST/PATCH/DELETE/PUT). Added to `app_shared.security.scopes.Scope`.

## Endpoints

| Method | Path | Scope | Notes |
|--------|------|-------|-------|
| POST | `/v1/scrape-profiles` | write | Create; `validate_profile` first; stored `workspace_id = caller` (never global); `201`. Duplicate name → `409 DUPLICATE_PROFILE`. |
| GET | `/v1/scrape-profiles` | read | List own + global (`visible_profiles_select`), keyset `(created_at,id)` pagination (default 50 / max 500), `{items, next_cursor}`. |
| GET | `/v1/scrape-profiles/{id}` | read | Read own or global (visible); else `404`. |
| PATCH | `/v1/scrape-profiles/{id}` | write | Update own only (`owned_profile_get` → `404` for global/other-ws, FR-021); `validate_profile` on changed fields. |
| DELETE | `/v1/scrape-profiles/{id}` | write | Delete own only; `ON DELETE SET NULL` clears references (FR-023); `DeleteOutcome`. Global/other-ws id → `404`. |
| POST | `/v1/scrape-profiles/bulk-upsert` | write | Set-based (`profiles-bulk-upsert.md`); `{profiles:[...]}` → `{upserted, profiles, rejected:[{index,name,field,code,reason}]}`; `200`. |
| PUT | `/v1/scrape-profiles/workspace-default` | write | `{profile_id: uuid|null}` → set `workspaces.default_scrape_profile_id` after `assert_profile_assignable` (own+global ok, cross-ws `422`, null clears). |

## Schemas (`schemas/scrape_profiles.py`, Pydantic v2)

- `ScrapeProfileCreate` (`extra="forbid"`): `name` required; `mode`/`adapter_key`/`variant_strategy` typed with the new enums (defaults applied); the three `*_enabled` (default True); nullable extraction fields; JSONB bundles (`variant_selector_config`/`price_transform_rules`/`validation_rules`/`confidence_rules`/`headers`/`cookies`); `wait_for_selector`; `request_timeout_ms` (default 30000); `browser_timeout_ms`. `workspace_id` is **never** client-supplied (server sets caller's).
- `ScrapeProfileUpdate` (`extra="forbid"`): every field optional (partial); `name` optionally updatable (re-checks uniqueness).
- `ScrapeProfileResponse` (`from_attributes=True`): every stored column incl. `workspace_id` (nullable → `null` for global) + `created_at`/`updated_at`.
- `ScrapeProfileListResponse`: `{items, next_cursor}`.
- `ScrapeProfileBulkUpsertRequest`: `{profiles: list[ScrapeProfileBulkUpsertItem]}`.
- `ScrapeProfileBulkUpsertResult`: `{upserted:int, profiles:[Response], rejected:[{index,name,field,code,reason}]}`.
- `WorkspaceDefaultProfileAssignment`: `{profile_id: uuid|null}`.
- Reuses `app.schemas.catalog.DeleteOutcome`.

## Validation → error mapping

`ProfileValidationError(field, code, message)` → `422 {error:{code:"VALIDATION_ERROR", field, message}}` (single/create/update); collected into `rejected[]` (bulk). Cross-workspace assignment → `422 WORKSPACE_MISMATCH`; missing ref → `404 NOT_FOUND`; duplicate name → `409 DUPLICATE_PROFILE`; bad cursor → `422 INVALID_CURSOR`.

## Tests

- **Unit (no DB)**: router declares correct `require_scopes` per method (read vs write); router mounted in `main.py`; schema shape (enum fields, `extra="forbid"`, `workspace_id` not in create).
- **Live (marked)**: create→read round-trip byte-identical bundles; duplicate name rejected; list paginates own+global; PATCH/DELETE of a global id via tenant path → 404; bulk-upsert valid+invalid mix; workspace-default assignment own/global/cross-ws/null.
