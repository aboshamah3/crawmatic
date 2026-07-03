# Contract: error-code vocabulary (`app_shared.enums.ScrapeErrorCode` / `scrape_core.errors`)

Structured error codes from the §34 vocabulary, so debugging, the later strategy optimizer, access-policy tuning, and client reporting share one language (Constitution §34). `ScrapeErrorCode` is a `StrEnum` (`VARCHAR`) in `app_shared.enums`; `scrape_core.errors` holds the fetch-failure classifier + helpers.

## Codes used by this slice (subset of §34)

| Code | Emitted when |
|------|--------------|
| `HTTP_403` | fetch returns 403 |
| `HTTP_404` | fetch returns 404 |
| `HTTP_429` | fetch returns 429 |
| `TIMEOUT` | request times out |
| `DNS_ERROR` | host does not resolve |
| `PRICE_NOT_FOUND` | no extraction strategy yields a price |
| `INVALID_PRICE_FORMAT` | candidate is non-Decimal / non-finite / over-scale / non-positive |
| `LOW_CONFIDENCE_PRICE` | candidate confidence below `min_accepted_confidence` (default 0.75) |
| `CURRENCY_MISMATCH` | competitor currency ≠ client variant currency (observation saved `comparable=false`) |
| `BLOCKED` | SSRF/unsafe-target rejection **or** robots-policy skip (no body download) |
| `UNKNOWN_ERROR` | unclassified failure |

Forward-compat codes present in §34 but not exercised here (proxies/browser/optimizer are later specs): `VARIANT_NOT_FOUND`, `CURRENCY_NOT_FOUND`, `STOCK_NOT_FOUND`, `PROXY_FAILED`, `PLAYWRIGHT_FAILED`, `SELECTOR_BROKEN`, `STRATEGY_DEGRADED`, `RATE_LIMITED`, `LOCKED_ALREADY_RUNNING`, `LIMIT_REACHED`, `LEGAL_REVIEW_REQUIRED`.

## Placement

- `error_code` columns on `price_observations` / `request_attempts` / `match_current_prices` store the string value.
- `CURRENCY_MISMATCH` is a **warning** on an otherwise successful (`comparable=false`) observation; the rest accompany `success=false`.

## Note

An SSRF/unsafe-target rejection is surfaced as `BLOCKED` (the closest §34 member) with a descriptive `error_message`; there is no dedicated SSRF code in §34. The same applies to a robots-policy skip. This keeps the persisted vocabulary within §34 while the message carries the specific cause.
