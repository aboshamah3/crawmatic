# Contract: extraction strategies (`scrape_core.extraction`)

Pure, framework-agnostic (`parsel` + stdlib `json`/`re`), no reactor. Fully unit-testable against fixture HTML strings (FR-007/008, US3, SC-003/SC-007).

## Ordered chain (this MVP)

`extract(body, profile) -> ExtractionCandidate | None` tries, in order, **first hit wins**:

1. **JSON-LD** (`jsonld.py`) — parse `<script type="application/ld+json">`; read `Product`/`Offer` → `offers.price`, `offers.priceCurrency`, `availability`, `name`. Method `JSON_LD`, default confidence **0.95**. Only if `profile.jsonld_enabled`.
2. **CSS** (`css.py`) — `parsel` CSS selectors from the profile (`price_selector`, `old_price_selector`, `currency_selector`, `stock_selector`, `title_selector`). Method `CSS`, default confidence **0.85**.
3. **Regex** (`regex.py`) — DB regex rules (`price_regex`, `old_price_regex`, `currency_regex`, `stock_regex`) applied to the body. Method `REGEX`, default confidence **0.75**. A lone unlabeled number falls to the `SINGLE_NUMBER` heuristic, confidence **0.40**.

If none yields a price → `None` (the spider records a `success=false` observation, `error_code=PRICE_NOT_FOUND`).

## `ExtractionCandidate`

`raw_price_text`, `currency`, `method: ExtractionMethod`, `confidence: float`, `selector_used`, `raw_title`, `stock: StockStatus | None`, `matched_text` (surrounding text used by `reject_if_text_contains`).

## Confidence source

Defaults come from `app_shared.profiles.confidence.DEFAULT_CONFIDENCE_RULES` (`jsonld` 0.95, `css` 0.85, `regex` 0.75, `single_number` 0.40) overlaid by the profile's validated `confidence_rules` via `resolve_confidence_rules` (Principle IV/VII) — **never** hardcoded literals in the extractor.

## Notes

- `price` is **not** parsed to `Decimal` here — the raw text is handed to validation (contracts/price-validation.md), which owns the §19 money boundary.
- Deferred per master decision #4: platform/product pattern, embedded-JSON, XPath, Playwright (later specs) — the chain leaves seams for them.
