# Contract: URL Normalization + Versioned Pattern (`app_shared/url_pattern.py`)

Pure, framework-agnostic (stdlib `urllib.parse` + `re`). Implements the §15 `derive_url_pattern` algorithm plus the identity-normalization the match unique key needs, behind a single algorithm-version constant (FR-010/011, research D3).

## API
- `URL_PATTERN_ALGORITHM_VERSION: int = 1` — stored per row as `url_pattern_version`; bumped when the derivation changes; versions never mixed in lookups; backfill on a bump is out of scope.
- `normalize_url(url: str) -> str` — canonical **identity** URL.
- `derive_url_pattern(url: str) -> str` — versioned **grouping** pattern.
- `derive_match_url_fields(url: str) -> tuple[str, str, int]` — `(normalized_competitor_url, url_pattern, URL_PATTERN_ALGORITHM_VERSION)` in one call.

(Callers pass a URL that has already been `validate_competitor_url`'d; these functions assume a parseable http(s) URL.)

## `normalize_url` steps (identity — query KEPT)
1. `urlsplit`; lowercase scheme and host.
2. Strip leading `www.` from host.
3. Strip default port (`:80` for http, `:443` for https).
4. Remove fragment.
5. Remove a single trailing slash from the path (but keep root `/` → empty path acceptable; a bare host normalizes without a trailing slash).
6. **Keep** the query string as-is (it can distinguish the product, e.g. `?variant=123`).
7. Reassemble: `{scheme}://{host}{path}{?query}`.

## `derive_url_pattern` steps (grouping — §15, scheme + query DROPPED)
1. Start from the normalized host + path (drop scheme and query).
2. Split the path into segments.
3. Preserve a leading **locale prefix** segment matching `^[a-z]{2}(-[a-z]{2})?$` (e.g. `ar`, `en`, `en-us`).
4. For each remaining segment:
   - If the previous *kept* path key is a **known product key** (`products`, `product`, `p`, `item`), replace this segment with `*`.
   - Else if the segment is **id-like**, replace it with `:id`.
   - Else keep it.
5. Reassemble as `host/seg1/seg2/...` (no leading scheme, no trailing slash).

### id-like segment (version 1 thresholds)
A segment is id-like iff any of:
- all digits — `segment.isdigit()`;
- UUID-like — matches `^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`;
- long mixed alphanumeric — `len >= 8` and contains both a letter and a digit;
- mostly digits — `len >= 4` and digit-ratio ≥ 0.5.

(Thresholds are deliberately conservative so ordinary slugs like `iphone-15` are NOT id-like; they live behind the version bump.)

### Known product path keys → wildcard
`/products/<slug>` → `/products/*`; `/product/<slug>` → `/product/*`; `/p/<id-or-slug>` → `/p/*`; `/item/<id-or-slug>` → `/item/*`; locale-prefixed `/ar/products/<slug>` → `/ar/products/*`.

## Worked examples
| Input | `normalize_url` | `derive_url_pattern` |
|-------|-----------------|----------------------|
| `https://www.Competitor.com/ar/products/iphone-15/?utm=x#frag` | `https://competitor.com/ar/products/iphone-15?utm=x` | `competitor.com/ar/products/*` |
| `http://competitor.com:80/p/9f8a7b6c/` | `http://competitor.com/p/9f8a7b6c` | `competitor.com/p/*` |
| `https://competitor.com/catalog/123456?variant=7` | `https://competitor.com/catalog/123456?variant=7` | `competitor.com/catalog/:id` |
| `https://competitor.com/item/550e8400-e29b-41d4-a716-446655440000` | (same, no trailing slash) | `competitor.com/item/*` |

## Unit tests (no DB)
- `normalize_url`: host lowercased, `www.` stripped, default port stripped, fragment removed, trailing slash removed, **query preserved**; two raw URLs differing only in scheme-case/`www.`/trailing-slash/fragment normalize equal (so they collide on the unique key), while two differing in `?variant=` do NOT.
- `derive_url_pattern`: product-slug → `*` for each known key (incl. locale-prefixed); id-like segments → `:id` (all-digits, UUID, long mixed, mostly-digits); ordinary slug kept; locale prefix preserved; scheme + query dropped.
- `derive_match_url_fields` returns the version constant (`== URL_PATTERN_ALGORITHM_VERSION`) as the third element.
- Version constant is an `int` and referenced (not hardcoded) by the router/upsert path.
