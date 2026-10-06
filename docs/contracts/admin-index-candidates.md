# `GET /v1/admin/index/workspaces/{workspace_id}/candidates`

Router: `apps/api/app/routers/catalog_index.py::workspace_candidates`. Auth: the SaaS service
token only (`require_service_token`). Internal: excluded from the public OpenAPI.

Proposes catalog-index candidates for the workspace's ACTIVE variants, walked in variant-id
order. Filters out the workspace's own store, any URL already matched in ANY status, archived
competitors, hosts beyond the competitor-domain cap, and anything the accept route would reject
(dry run in a rolled-back savepoint).

## Query parameters

| Name | Type | Default | Bounds | Meaning |
|---|---|---|---|---|
| `max_candidates` | int | 100 | 1..500 | Stop once this many candidates are collected. |
| `variant_limit` | int | 50 | 1..200 accepted | Variants to scan. **Clamped to 50** (`MAX_VARIANTS_PER_REQUEST`) since 2026-10-06; a larger value is not refused. |
| `per_variant` | int | 3 | 1..5 | Candidates kept per variant. |
| `currency` | string | `SAR` | max 8 | Index price currency; `''` disables the filter. |
| `cursor` | uuid | none | | `next_cursor` of the previous response. |

## Response

`{generation, candidates[], next_cursor, variants_scanned}`.

## Work cap and `next_cursor` (2026-10-06, risk review P7)

One request looks up at most 50 variants (each lookup is up to three index queries), whatever
`variant_limit` asks for. `next_cursor` is the id of the last variant scanned and is non-null
whenever variants may remain: the scan stopped early on `max_candidates`, or the page was full
(`variants_scanned == 50`). Pass it back as `cursor` until `next_cursor` is `null`. A null
`next_cursor` means every remaining ACTIVE variant was scanned. `variants_scanned` can be smaller
than the page when `max_candidates` was reached first; the cursor then resumes right after the
last scanned variant, so no variant is skipped.

## Title-stage ordering (2026-10-06, risk review P7)

`app_shared.catalog_index.lookup` now ranks the title stage's full-text matches by
`ts_rank(...) DESC, price ASC, domain ASC` before `LIMIT 200` (`SCAN_LIMIT`), so a common phrase
keeps its best-ranked rows for the Jaccard re-rank instead of an arbitrary 200. Response shape is
unchanged; the chosen candidates can differ (better) from before.
