# Labeled offer fixtures — governance (EPA C4, 2026-09-08)

`{noon,stech,amazon}.jsonl` are the **expected offers** the domain canary
(`scripts/run_domain_canary.py`) scores a live run against. One JSON
object per line:

| field | meaning |
|---|---|
| `domain` | bare competitor domain (`noon.com`, `stech.ink`, `amazon.sa`) |
| `match_id` | `competitor_product_matches.id` — the join key to a results file |
| `url` | the URL the label was captured from |
| `variant` | the identifier that names this offer's variant, or `null` |
| `seller` | merchant of record, or `null` where no evidence captured one |
| `price` | decimal string, or `null` when no price is expected |
| `currency` | ISO code, or `null` when no price is expected |
| `availability` | `IN_STOCK` / `OUT_OF_STOCK` / `UNKNOWN` |
| `expected_error_code` | the `ScrapeErrorCode` a correct run should produce, or `null` |
| `label_source` | provenance, per row |

**Scored fields are `price`, `currency`, `availability` only** — the
acceptance bar (>= 95 % agreement) is defined over those three in
`docs/ops/DOMAIN_STRATEGIES_2026-09.md`. `seller` and `variant` are
recorded for triage, never scored: see the seller note below.

## Row counts

| file | rows | source |
|---|---|---|
| `amazon.jsonl` | 30 | 2026-08-24 canary evidence bundle, + `tests/fixtures/amazon_labeled/<ASIN>/expected.json` for the 5 re-captured ASINs |
| `noon.jsonl` | 34 | `tests/fixtures/noon_labeled/db_evidence_labels.json` (the B5/B5b proxy-certified set) |
| `stech.jsonl` | 30 | `tests/fixtures/stech_30_targets/<match_id>/product.json` (captured Shopify product JSON) |

## Provenance and honesty notes

* **Nothing here is synthesized.** Every price, currency and availability
  value is copied from committed repo fixtures or from the owner's
  `/srv/crawmatic/evidence/canary-2026-08-24/` bundle (`price_observations.csv`
  joined to `request_attempts.csv` for the URL). Where the evidence
  records no value, the field is `null` or `UNKNOWN` — never guessed.
* **`seller` is `null` for both marketplaces.** The 2026-08 evidence
  bundle stores structured extraction results only (price, currency,
  stock, title) and never captured a buybox/marketplace seller for
  amazon.sa or noon.com. `stech.ink` is a single-merchant Shopify
  storefront, so its seller is the domain itself — a fact, not an
  inference. This is exactly why `seller` is outside the scored set.
* **`availability` is weak on amazon.sa.** All 60 of that domain's
  2026-08-24 `price_observations` rows carry an empty `stock_status`, so
  29 of the 30 amazon labels are `UNKNOWN`; only the ASIN re-captured in
  English locale (`B0H1MXM57R`) has a positive in-stock signal. An
  availability comparison against those rows therefore mostly asserts
  "still unknown". Read the amazon availability column with that in mind
  — `price` and `currency` carry the real signal for that domain until a
  capture with genuine stock text replaces these labels.
* **The 2026-08-25 amazon.sa locale finding still stands** (see
  `tests/fixtures/amazon_labeled/FIXTURES.md`): outside the `/-/en/` URL
  form, amazon.sa renders the Arabic currency word and no `SAR` mapping
  exists in this codebase. A canary run against non-`/-/en/` URLs will
  disagree on `currency` for reasons that are about the URL, not the
  strategy.
* These labels are a **point-in-time** statement. Prices move; a
  disagreement is a prompt to look, not proof of a broken strategy. The
  runbook (`docs/ops/DOMAIN_STRATEGIES_2026-09.md`) says how to tell the
  two apart and when to re-label.
