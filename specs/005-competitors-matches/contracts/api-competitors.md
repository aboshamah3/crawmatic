# Contract: `/v1/competitors` (`apps/api/app/routers/competitors.py`)

Scope-gated CRUD for competitors (US1). Every endpoint runs on the SPEC-03 auth seam (`app.deps.get_current_principal` → `set_workspace_context` already applied to the yielded session) and is gated via `app.deps.require_scopes(...)`. All reads/writes go through `app_shared.repository.scoped_select`/`scoped_get`; RLS backs them as the second isolation layer. Registered in `app.main`.

## Endpoints
| Method | Path | Scope | Notes |
|--------|------|-------|-------|
| POST | `/v1/competitors` | `competitors:write` | create; `domain` unique per workspace |
| GET | `/v1/competitors` | `competitors:read` | list, keyset cursor pagination (default 50 / max 500) |
| GET | `/v1/competitors/{id}` | `competitors:read` | `scoped_get`; `404 NOT_FOUND` if absent/other-workspace |
| PATCH | `/v1/competitors/{id}` | `competitors:write` | partial update (`model_dump(exclude_unset=True)`) |
| DELETE | `/v1/competitors/{id}` | `competitors:write` | hard-delete now; structured for archive-by-status; `DeleteOutcome` |

## Request/response DTOs (`apps/api/app/schemas/competitors.py`, Pydantic v2)
- `CompetitorCreate` — `name` (required), `domain` (required), `status?`, `legal_status?`, `robots_policy?`, `default_scrape_profile_id?`, `default_access_policy_id?`, `max_concurrent_requests?`, `max_requests_per_minute?`. Omitted enum/status fields fall back to the model defaults (`ACTIVE`/`REVIEW_REQUIRED`/`RESPECT`).
- `CompetitorUpdate` — all fields optional (partial).
- `CompetitorResponse` — all columns incl. `id`, `created_at`, `updated_at` (`model_validate` from the ORM row).
- `CompetitorListResponse` — `{items: [CompetitorResponse], next_cursor: str | None}`.
- Delete → `app.schemas.catalog.DeleteOutcome{id, outcome}` (reused).

## Behaviors
- **Domain uniqueness (FR-003).** A second create with an existing `(workspace_id, domain)` violates `unique(workspace_id, domain)` → mapped to `409 {"error":{"code":"DUPLICATE_DOMAIN"}}` (pre-checked via a scoped `select` or by catching the IntegrityError).
- **Isolation (FR-002/015).** `get`/`update`/`delete` use `scoped_get`; a cross-workspace id returns `404` (never another workspace's row); RLS is the second layer.
- **Deletion (FR-016).** No dependent history exists in this spec → hard delete; the branch is structured so a future `status = ARCHIVED` swap is a one-line change; the response `outcome` says which happened.
- **Scope gating (FR-015).** A `competitors:read`-only credential is refused (`403 FORBIDDEN`) on any write.

## Unit tests (no DB)
- Router declares the correct `require_scopes` per method (read vs write).
- Router is registered in `app.main` (`/v1/competitors` present in the OpenAPI paths).
(Live create/uniqueness/isolation/delete-outcome → integration, PG host.)
