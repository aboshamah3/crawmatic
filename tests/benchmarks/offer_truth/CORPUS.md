# Offer-truth labeled benchmark corpus — governance (Task W3.3, READY-012)

Styled after `tests/fixtures/noon_labeled/FIXTURES.md` and the B5/B5b
certification fixture sets (provenance, real-vs-synthetic labeling,
refresh policy). This corpus scores the W3.2 ranked-extraction machinery
(`app_shared.strategy.candidate_ranking.collect_candidates`/`rank`, via
`scrape_core.extraction.pipeline.collect_extraction_candidates`/
`extract_ranked`) plus two benchmark-local shims for the two domains
that have no HTML strategy at all (Noon's `PLATFORM_JSON` catalog-API
hits, S-Tech's Shopify `products/{handle}.js` variants) — see
`scripts/run_offer_benchmark.py`'s module docstring for exactly what
each shim does and does not implement.

## What this is NOT

This is not a benchmark of a shipped `seller`/`shipping`/`coupon`/
`landed_total` extractor, because **no such extractor exists yet
anywhere in this codebase.** `OfferObservation` (W3.1,
`libs/shared/app_shared/observations/offer_observation.py`) defines the
*contract* for those fields; nothing currently populates most of them
from a real page. Per the task's step 2 ("reports precision/recall +
error magnitude SEPARATELY for ... seller, shipping, coupon/member
conditions, landed total"), this corpus carries real ground-truth values
for those fields anyway, specifically so the scored report shows the
**true, current recall — honestly near zero** for them, rather than
silently only measuring the two-ish fields (price, identity) that
happen to already work. A 0%-recall field in the scored report is a
real finding (a capability gap), not a corpus bug. See
`scripts/run_offer_benchmark.py`'s report for exactly which fields have
structural 0% coverage today and why.

Two exceptions carry genuine, correctly-extractable `old_price` signal:
Noon's `price`/`sale_price` pair (when `sale_price` is set, `price` is
the real old price) and S-Tech's `compare_at_price`. `seller` has one
genuine, trivial, always-true signal on S-Tech (a single-merchant
storefront: the seller is always `"S-Tech"` itself) — every other
seller/shipping/coupon/landed_total ground-truth value in this corpus
is a documented gap, not a false claim of capability.

## Corpus size

| Domain | Cases | Real (reused fixtures) | Synthetic |
|---|---|---|---|
| Amazon (`amazon/`) | 22 | 2 | 20 |
| Noon (`noon/`) | 22 | 2 | 20 |
| S-Tech (`stech/`) | 20 | 3 | 17 |
| **Total** | **64** | **7** | **57** |

Counted from disk, not from memory: `ls tests/benchmarks/offer_truth/<domain> | wc -l`
per directory, and `grep -l '"synthetic": true'` for the last column.
The scored `total_cases` printed by `scripts/run_offer_benchmark.py`
is the same 64.

Every case carries `"synthetic": true|false` and a `provenance` block.
`synthetic: false` cases point at a file already committed under
`tests/fixtures/` (via `input.path`, relative to the repo root) —
**no bytes are duplicated into this corpus for those cases**, keeping
this directory's own footprint tiny (`du -sh` ≈ 296K for all 64 case
files). `synthetic: true` cases embed their (small, hand-authored) HTML/
JSON directly in the case file's `input` block — there was nothing to
reuse for the categories the task asks for that no existing capture
exercises (stale-structured-data conflicts, marketplace multi-offer
disagreement, adversarial data corruption, ...), and the task's own step
1 explicitly directs synthesizing those from existing captures' *shapes*
rather than inventing a live fetch. Every synthetic case's `provenance`
states its modeling rationale.

**No live fetches were made for this task.** The two real-file domains'
fixtures were already committed by Tasks B4/B5/B5b; this task only reads
them (`input.path`), never re-fetches or modifies them. The sealed A1
canary evidence bundle under `/srv/crawmatic/evidence/` was not touched.

## Case file schema

One JSON file per case, under `<domain>/<case_id>.json`
(`domain` ∈ `amazon`/`noon`/`stech`). See `schema.py` for the loader and
the authoritative field list (`BenchmarkCase`, `ExpectedFields`). Briefly:

```jsonc
{
  "case_id": "...",
  "domain": "amazon" | "noon" | "stech",
  "category": "baseline" | "stale_structured_data" | "multi_product"
             | "variant" | "marketplace" | "locale_format" | "bundle"
             | "adversarial",
  "synthetic": true | false,
  "provenance": { "source": "...", "note": "..." },
  "input": {
    "kind": "html_file" | "html_inline" | "platform_json_file"
          | "platform_json_inline" | "shopify_json_file"
          | "shopify_json_inline" | "redos_direct",
    // ...kind-specific fields — see scripts/run_offer_benchmark.py's
    // `_run_case` dispatcher for exactly what each kind expects.
  },
  "expected": {
    "identity": { "primary_id": "...", "title_contains": "..." },
    "current_price": "129.00" | null,   // exact decimal STRING, never a float
    "old_price": "499.00" | null,
    "currency": "SAR" | null,
    "stock": "IN_STOCK" | "OUT_OF_STOCK" | "UNKNOWN" | null,
    "seller": "..." | null,
    "shipping": "..." | null,
    "coupon": "..." | null,
    "landed_total": "..." | null,
    "expect_conflict": false,   // page genuinely has an unresolved material disagreement
    "expect_no_price": false    // correct answer is "nothing to report", not a miss
  }
}
```

`null` on a ground-truth field means "no claim for this field on this
case" (unknown, not zero — the same rule `OfferObservation` itself
follows), and the scorer skips that field entirely for that case rather
than counting a vacuous true negative.

## Category coverage (why each category exists, per domain)

* **`baseline`** — clean, healthy pages: in-stock, out-of-stock,
  discounted. The positive/negative controls everything else is measured
  against.
* **`stale_structured_data`** — a cached JSON-LD/API block disagreeing
  with the live-read price. Amazon's two cases directly reproduce the
  shape of the real W3.2 regression fixture
  (`tests/fixtures/html/noon_product_real.html`, `price:499` vs. the
  correct `129`) for a different domain, at both a material (must
  Conflict) and a non-material (must still Winner) divergence.
* **`multi_product`** — a result set naming more than one product; the
  system must resolve the exact queried identity, never the first hit.
* **`variant`** — SKU/color/size variant resolution, direct exercise of
  the B4 typed-identifier philosophy (`scrape_core.adapters.
  variant_resolution`'s algebra, exercised here via a benchmark-local
  simplification — see the script docstring) on S-Tech, plus a
  same-page-multiple-Product-blocks identity test on Amazon.
* **`marketplace`** — multiple sellers/offers under one listing
  (Amazon JSON-LD `offers[]`, a duplicate-SKU-different-price Noon
  case) and the seller-vs-vendor distinction on S-Tech.
* **`locale_format`** — Arabic ريال forms (B4's real fix), the
  deliberately-unmapped Qatari riyal (B4's "never guess" rule), EU/US
  numeric grouping, and adversarial numeric encodings (Arabic-Indic
  digits, grouped strings, minor-unit precision).
* **`bundle`** — multi-packs and bundle-priced listings; verifies
  identity/price extraction is unaffected by pack framing, while
  `landed_total` stays honestly unclaimed.
* **`adversarial`** — malformed/corrupted/hostile inputs: text
  injection into a price node, the SINGLE_NUMBER confidence gate,
  currency-driven identity conflicts, negative prices, missing/absent
  fields, string-typed booleans, barcode collisions, a direct
  `search_bounded` ReDoS-shape refusal.

## Refresh policy

The 7 real-file cases point at fixtures owned by Tasks B4/B5/B5b —
their own `FIXTURES.md` refresh policies govern recapture (14 days for
Amazon, 7 days for Noon, no stated ceiling for the S-Tech replay corpus
since it is a fixed regression fixture, not a freshness-sensitive
benchmark input). If those fixtures are recaptured with different
values, update this corpus's matching `expected.json`-derived ground
truth in the same change — a stale expected value here would silently
break the benchmark's honesty, not just its pass rate.

The 57 synthetic cases have no capture staleness (they were never
fetched) and do not need recapture; they should be revisited only when
the extraction pipeline itself changes shape (a new strategy, a new
`ExtractionMethod`, a schema change to `OfferObservation`) in a way that
could change what "correct" means for them.

## Known, documented coverage gaps (read before treating a low score as a bug)

1. **`old_price` is 0%-recall on Amazon and on every domain path except
   Noon's `sale_price`/`price` pair and S-Tech's `compare_at_price`.**
   No CSS/JSON-LD strategy surfaces a typed old-price field today
   (`extract_css` folds it into `matched_text`; `extract_jsonld` has
   nowhere in schema.org `Offer` to read it from without a second,
   divergent parser).
2. **`seller`/`shipping`/`coupon`/`landed_total` are 0%-recall
   everywhere except S-Tech's trivial `"S-Tech"` self-seller fact.**
   No extractor for any of these exists in `scrape_core.extraction` or
   `app_shared.strategy.candidate_ranking` as of this task.
3. **Noon's currency is a hardcoded domain assumption (`SAR`), not a
   per-hit extracted fact** — the catalog-API payload carries no
   currency field at all. `noon_adversarial_non_saudi_storefront_
   currency_gap` deliberately fails against this to keep that gap
   visible in the scored report rather than papering over it with a
   currency the shim cannot actually see.

These are why the §15.3 auto-repricing gate's default 99%
financial-field-agreement threshold is very unlikely to pass against
this corpus today — see `scripts/run_offer_benchmark.py` and
`tests/benchmarks/test_offer_benchmark_gate.py` for the gate itself.
That is the correct, honest outcome of this task, not a bug in it: the
system genuinely is not ready for unattended repricing yet, and this
benchmark's whole purpose is to make that measurable instead of assumed.
