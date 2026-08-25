# S-Tech 30-target replay fixtures (EPA B4, READY-003)

## What this is

The exact 30 `stech.ink` targets the 2026-08-24 production canary ran,
paired with each product's live Shopify `products/{handle}.js` document
captured on 2026-08-25, and with the legacy
`competitor_product_matches.competitor_variant_identifier` value that was
in production for that match at capture time.

This is the regression corpus for the bug in
`PRODUCTION_READINESS_REPORT_2026-08-24.md`: the Shopify adapter compared
that untyped legacy value straight against `variants[].id` and, finding
no match, emitted terminal `NOT_LISTED` — for **26 of these 30 targets**,
every one of which had returned HTTP 200 with a perfectly healthy product
JSON. A false delisting silently removes a real competitor price from
every downstream comparison, so this corpus exists to make that
particular regression impossible to reintroduce quietly.

## Provenance

| | |
|---|---|
| Target set | `evidence/canary-2026-08-24/request_attempts.csv`, rows whose `url` is on `stech.ink` (exactly 30, 30 distinct `match_id`s, 30 distinct URLs) |
| Canary outcome | 26 × `NOT_LISTED` / `identity_validation_result=NOT_LISTED`, 4 × success/`VALID` — all 30 with `status_code=200`, `adapter_key=shopify_product_json` |
| Product JSON captured | 2026-08-25, direct HTTPS GET of each canary URL (they are already the `.js` endpoints) |
| Legacy identifiers | read-only `SELECT` against the production engine database, same day |
| Fetch budget | 31 requests total (1 × `robots.txt` + 30 × product), ≤ 2 req/domain/s, hard cap 40, `User-Agent: crawmatic-identifier-backfill/1` |
| Request log | `evidence/b4-request-log-2026-08-25.csv` (every request, no auth headers) |
| Integrity | `SHA256SUMS.json` — sha256 of each `product.json` as written |

`robots.txt` was fetched and honored before any product request. No
proxy was used; no authenticated request was made.

## Sanitization

The Shopify `products/{handle}.js` document is a **public catalog
document** — it carries no cookies, tokens, credentials or personal data.
Two large, identity-irrelevant blobs are nonetheless stripped to keep the
corpus small and free of embedded third-party CDN URLs:

* `description` → `""`, `images` → `[]`, `featured_image`/`media` removed;
* per variant: `featured_image`, `quantity_price_breaks`,
  `selling_plan_allocations` removed.

**Every identity-bearing field is preserved verbatim**: `handle`,
`variants[].id`, `variants[].sku`, `variants[].barcode`,
`variants[].price`, `variants[].available`, `title`, `vendor`. That is
the entire structure `scrape_core.adapters.variant_resolution` reads, so
the replay exercises the real resolution path against real data.

## Layout

```
<match_id>/product.json    sanitized Shopify product document
<match_id>/expected.json   legacy identifier + the canary's 2026-08-24 verdict
SHA256SUMS.json            capture timestamp + per-fixture sha256
```

`expected.json` deliberately records the *canary's* verdict, not a
hand-written expectation: the replay asserts that today's resolver does
better than the recorded production behaviour, which is a claim you can
check against the evidence CSV rather than against someone's opinion.

## What the corpus actually contains

All 30 products are live, HTTP 200, and **single-variant**. Of the 30
legacy identifier values:

* 4 are `NULL` (these are the 4 the canary got right — with no identifier
  the old code fell through to its default-variant branch);
* 17 are the product **handle** (the exact misread that caused the bug);
* 9 are a stale supplier SKU, a partial part number, or a truncated
  handle that matches nothing in today's JSON — the product is plainly
  there, so the honest answer is its sole variant, never `NOT_LISTED`.

## Refresh policy

Re-capture only when a resolver change needs evidence these documents
cannot supply, and re-capture the **whole** set in one bounded, logged
run so the corpus stays internally consistent. Merchant catalogs move:
a partial refresh would silently mix two points in time. Update
`SHA256SUMS.json` and this file's provenance table in the same change.

## Test

`tests/unit/test_stech_30_replay.py` — asserts ZERO false `NOT_LISTED`
across all 30 and at least 26 now-resolvable identities.
