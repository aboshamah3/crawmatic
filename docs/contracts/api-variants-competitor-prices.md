# `GET /v1/variants/competitor-prices` (bulk competitor prices)

Router: `apps/api/app/routers/variants.py::list_all_competitor_prices`. Scope `alerts:read`.

One item per `competitor_product_matches` row in the caller's workspace, each with the match's
latest known price (`match_current_prices`, null price fields until the first successful
scrape), its competitor's name, `product_variant_id` and `match_status`. Keyset-paginated over
the matches' `(created_at, id)`; envelope `{items, next_cursor}` as every list route
(`specs/004-catalog-products-variants-groups/contracts/pagination.md`).

## Query parameters

| Name | Type | Default | Meaning |
|---|---|---|---|
| `limit` | int | server default | Page size, clamped to `[1, MAX_LIMIT]` (`app_shared.pagination`). |
| `cursor` | string | none | Opaque `next_cursor` of the previous page. Malformed: `422 INVALID_CURSOR`. |
| `include_archived` | bool | `false` | **Added 2026-10-06.** `false`: matches with `status = ARCHIVED` are left out. `true`: every match, as before this date. |

## `include_archived` (2026-10-06, risk review P6)

ARCHIVED matches were removed by the merchant and are never scraped again; their price rows only
age. Since 2026-10-06 the route leaves them out by default. ACTIVE, PAUSED and FAILED matches are
always returned. The filter is applied inside the keyset page (before `LIMIT`), so a page is never
short because archived rows were dropped, and a cursor taken with one value of the flag must be
reused with the same value.

Behaviour change for existing callers: a caller that relied on seeing ARCHIVED rows (for example
to delete them from a local copy) must pass `include_archived=true`, or treat "absent from a full
snapshot" as removed.

Index: `ix_cpm_ws_created_id` on `competitor_product_matches (workspace_id, created_at, id)`
(migration `e2b8d4f6a1c3`) serves the keyset walk with or without the filter.

The per-variant route `GET /v1/variants/{variant_id}/competitor-prices` is unchanged: it still
returns every match of the variant, ARCHIVED included.
