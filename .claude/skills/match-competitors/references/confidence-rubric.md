# Confidence rubric

Same vocabulary as `mushtryati_product_matches.json` `_meta.confidence_levels` and
`specs/005-competitors-matches`. Every match gets both a label and a numeric score.

| Label | Score | Definition | Persisted? |
|---|---|---|---|
| `high` | 0.90–1.00 | Exact same product — **same brand** AND exact hard identifier confirmed **on the competitor page** in title + slug + description: MPN (e.g. `3JA28AE`) in the title/specs, ASIN matching the same-brand listing, or a full spec string identical incl. every variant attribute (capacity, color, RAM/storage, connectivity, yield) | yes |
| `medium` | 0.60–0.89 | **Same brand** and same model line confirmed, but one non-brand variant attribute could not be isolated (variant-selector page, Wi-Fi vs 5G). Brand must still be exact. MUST record what differs in `note` and set `competitor_variant_options` | yes (validator-checked) |
| `low` | <0.60 | Title similarity only — nothing hard confirmed | **never persisted**; goes to validator, then retry/human review |
| `gap` | — | No listing found after strategies 1–6 (list them in `strategies_tried`). A deliberate, valuable negative | yes (as gap) |

## Hard rules

- **STRICT SAME-BRAND (2026-07-22)**: a match requires the competitor listing to be the
  **exact same product from the exact same brand**. A Mint (منت) product matches ONLY a
  Mint listing. An OEM original (HP/Canon/Epson/Brother genuine) is **NOT a match** for a
  Mint compatible — not `medium`, not with a note: it is `no_match`/`gap`. The same holds
  for every other cross-brand pairing (FQ, A.W.D, S-TECH, HD, FMQ, Generic, …). The
  price gap between brands makes cross-brand "matches" worse than gaps.
- Verification is mandatory and **three-way**: open the product page (WebFetch, or
  `fetch.py` through the proxy) and confirm brand + part number in (1) the page **title**,
  (2) the URL **slug**, and (3) the **description/spec fields**. Quote the confirming
  strings in `evidence`. Where the slug is opaque (amazon `/dp/ASIN`, noon `N…/p/`),
  title + description must both confirm. If title, slug, and description disagree with
  each other on brand/color/part number ⇒ `no_match` (or review if it still looks like
  the same brand+product). A search-result snippet is NEVER enough for any match.
- **Original vs compatible**: for ink/toner, a third-party "compatible" cartridge is NOT a
  match for an original OEM cartridge (and vice versa) — `no_match`, never `medium`.
- Marketplace "Brand" fields lie (Mint listings are often labeled "Generic") — the
  title/description saying "Mint"/"منت" + the part number is the authority, but a listing
  whose title does NOT state the brand at all cannot be a match.
- **International/other-market versions** on amazon.sa: acceptable as `medium` with
  `note: "international version"`, never `high`.
- Refurbished ≠ new (`مجدد` category products must match refurbished listings and
  vice versa).
- Bundles (printer + cartridges, laptop + bag) are not matches for the bare product.

## Worked examples (from the gold fixture)

- MacBook Pro 16 M4 Pro 48/512 → amazon `B0DLHZSDXT` (Silver): `high` — exact chip/RAM/
  storage in title, color pinned in `competitor_variant_options`, alt-color ASIN in note.
- Same product → jarir `...-646493.html`: `high` — Jarir title carries M4 Pro 14-core CPU.
- Galaxy Tab A11+ 5G 128GB → jarir Wi-Fi-only page: `medium` — same model line, no 5G SKU
  on Jarir; note says "closest variant-group match, not exact connectivity match".
- No listing on a competitor after all strategies: `gap`, keep `strategies_tried`.

## Addendum (field-validated 2026-07-24)
- **noon marketplace cap:** noon listings are `medium` max unless the page shows a brand store
  or a verified local seller line (then `high` after three-way). Seller/description legs that
  don't render keep the entry at medium.
- **Zero-offer listings** (amazon "Currently unavailable", no active seller): count as matches
  at `medium` with note `zero_offer` — export policy decided at upload time.
- **Capacity variants** (std vs high/XL/H): never a match — `gap` with the variant candidate in
  `note`. Regional/pack suffixes (D/E/P/PS/-S tiers): `medium` max with suffix note.
- **Sold-out/backordered but live listings** on local stores: count normally (links-only rule).
- Score/label bands are enforced at checkpoint time: high .90–1.00, medium .60–.89; floor any
  surviving medium to 0.60; `low` is still never persisted.
