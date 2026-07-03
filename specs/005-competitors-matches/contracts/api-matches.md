# Contract: `/v1/matches` (`apps/api/app/routers/matches.py`)

Scope-gated CRUD + set-based bulk-upsert for competitor-product matches (US2/US3). Same auth seam + scoped-helper + RLS discipline as `api-competitors.md`. Registered in `app.main`.

## Endpoints
| Method | Path | Scope | Notes |
|--------|------|-------|-------|
| POST | `/v1/matches` | `matches:write` | create one match (URL-safety + normalize + pattern) |
| GET | `/v1/matches` | `matches:read` | list, keyset cursor pagination (default 50 / max 500) |
| GET | `/v1/matches/{id}` | `matches:read` | `scoped_get`; `404 NOT_FOUND` |
| PATCH | `/v1/matches/{id}` | `matches:write` | partial update; if `competitor_url` changes, re-validate + re-derive normalized/pattern/version |
| DELETE | `/v1/matches/{id}` | `matches:write` | hard-delete now; structured for archive-by-status; `DeleteOutcome` |
| POST | `/v1/matches/bulk-upsert` | `matches:write` | set-based single-arbiter upsert + reject-and-report |

## Request/response DTOs (`apps/api/app/schemas/matches.py`, Pydantic v2)
- `MatchCreate` — variant reference (one of `product_variant_id` / `variant_external_id` / `variant_sku`), `competitor_id`, `competitor_url` (required), optional `competitor_variant_identifier`/`competitor_variant_sku`/`competitor_variant_options`/`external_title`/`scrape_profile_id`/`access_policy_id`/`priority`/`status`. **Not** accepted from the client: `product_id` (derived from the variant), the health fields (FR-017), `normalized_competitor_url`/`url_pattern`/`url_pattern_version` (server-derived).
- `MatchUpdate` — all mutable fields optional; health fields still server-owned.
- `MatchResponse` — all columns incl. `normalized_competitor_url`, `url_pattern`, `url_pattern_version`, health defaults, `created_at`/`updated_at`.
- `MatchListResponse` — `{items, next_cursor}`.
- `MatchBulkUpsertRequest` — `{matches: [MatchBulkUpsertItem]}` (item = same variant/competitor/url shape as `MatchCreate`).
- `MatchBulkUpsertResult` — `{upserted: int, matches: [MatchResponse], rejected: [{index, code, reason, url}]}`.
- Delete → `app.schemas.catalog.DeleteOutcome`.

## Create flow (`POST /v1/matches`)
1. `validate_competitor_url(payload.competitor_url)` → `422 UNSAFE_URL` on rejection (FR-007/008).
2. `derive_match_url_fields(...)` → `normalized_competitor_url`, `url_pattern`, `url_pattern_version`.
3. Resolve the variant in-workspace (scoped_get by id, or a scoped lookup by external_id/sku) → get `product_variant_id` + its `product_id`; a missing/other-workspace variant → `422 WORKSPACE_MISMATCH` / `404`.
4. Consistency-check `competitor_id` in-workspace (`assert_refs_in_workspace`).
5. Insert with health defaults; a duplicate `(workspace_id, product_variant_id, competitor_id, normalized_competitor_url)` → `409 DUPLICATE_MATCH` (FR-005: only the exact tuple dedupes; the same variant may have many other matches).

## Bulk-upsert flow
Per `contracts/matches-bulk-upsert.md`: `prepare_match_urls` (reject-and-report) → `dedup_last_wins` → variant resolution (one scoped `IN(...)`) → competitor consistency (one scoped `IN(...)`) → `build_matches_upsert` (one statement) → response with `upserted`, `matches`, and `rejected[]`. Health fields are never reset on conflict.

## Behaviors mapped to requirements
- **Save-time safety (FR-007/008/009, SC-004).** Every create/update/bulk path validates; unsafe never stored.
- **Versioned pattern (FR-010/011, SC-002/005).** Every stored match carries `url_pattern` + `url_pattern_version`.
- **Unlimited matches / exact-tuple dedupe (FR-005, SC-003).** Many matches per variant; only the 4-col tuple collides.
- **Reference integrity (FR-006, SC-007).** product/variant/competitor resolve in-workspace; `product_id` derived from the variant.
- **Set-based bulk (FR-013, SC-006).** One statement for all safe rows; unsafe reported.
- **Scope gating (FR-015, SC-008).** `matches:read`-only refused on writes.
- **Deletion (FR-016).** Hard-delete now; structured for archive-by-status; outcome reported.

## Unit tests (no DB)
- Router declares correct `require_scopes` per method (read vs write; bulk-upsert = write).
- Router registered in `app.main`.
(Live create/unsafe-reject/duplicate/bulk/isolation → integration, PG host.)
