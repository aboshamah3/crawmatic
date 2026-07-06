# Phase 1 Data Model — SPEC-14 Browser Scraping Service

**No new persistent schema. No migration.** (FR-017) This feature consumes existing SPEC-06 profile
fields and writes the existing SPEC-07 observation/current-price/attempt tables through the reused
`BatchedPersistencePipeline`. This document records the **shapes** the browser path reads/writes and
the one JSON structure the plan defines (`variant_selector_config`).

---

## 1. Consumed existing entities (no change)

### `scrape_profiles` (SPEC-06, `libs/shared/app_shared/models/scrape_profiles.py`) — browser fields

| Field | Type | Role in browser path |
|---|---|---|
| `mode` | `ScrapeProfileMode` (`HTTP`/`BROWSER`/`CUSTOM`) | `BROWSER` routes the match to the browser service (dispatch) |
| `wait_for_selector` | `Text | None` | selector the browser waits for before extraction (FR-003) |
| `browser_timeout_ms` | `Integer | None` | per-target wait/nav bound; `NULL` → `Settings.SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS` (R10) |
| `variant_selector_config` | `JSONB | None` | variant-interaction script (§2 below); `NULL` → no interaction (FR-004) |
| `price_selector`/`_xpath`/`_regex`, `currency_*`, `stock_*`, `title_*`, `validation_rules`, `confidence_rules`, `variant_strategy`, `adapter_key`, … | as SPEC-06 | reused by the shared extraction/validation against the **rendered** DOM |

No column is added or altered.

### `competitor_product_matches` (SPEC-05) — per-match variant source for `value_from`

| Field | Type | Role |
|---|---|---|
| `competitor_variant_options` | `JSONB | None` | source for `value_from: "options.<key>"` (R2) |
| `competitor_variant_identifier` | `Text | None` | source for `value_from: "identifier"` |
| `competitor_variant_sku` | `Text | None` | source for `value_from: "sku"` |
| `competitor_url`, `product_id`, `product_variant_id`, `competitor_id`, `scrape_profile_id`, `access_policy_id`, `url_pattern` | as SPEC-05 | reused by `load_targets` (unchanged) |

### Written tables (reused pipeline, unchanged shape)

`price_observations`, `request_attempts` (records `access_method = PLAYWRIGHT_PROXY`, R5),
`match_current_prices` (successful items only), `scrape_job_targets` (terminalized by the pipeline).
All via `scrape_core.pipelines._flush_batch` — no browser-specific persistence code.

---

## 2. `variant_selector_config` JSON shape (plan-defined, R2)

Stored in the existing `scrape_profiles.variant_selector_config` JSONB column. Interpreted by a
**two-function** split (R2, variant-selection.md) so the blocking `value_from` resolution runs
off-reactor while translation stays pure:
`scrape_core.browser.variant.resolve_variant_values(config, match) -> dict[str, str]` (off-reactor,
called inside `load_targets`, resolves each action's `value_from` against the match row) followed by
`scrape_core.browser.variant.parse_variant_config(config, resolved_values) -> list[PageMethod]` (pure
translation, no DB/match access).

```jsonc
{
  "version": 1,                 // int, required; only 1 supported (unknown → SELECTOR_BROKEN)
  "actions": [                  // list, required, ordered; executed before extraction
    {
      "type": "select_option",  // one of the allowlist below
      "selector": "select#size",// CSS selector (required for element actions)
      "value_from": "options.size"   // OR "value": "L"  (element actions needing a value)
    },
    { "type": "click", "selector": "button.add-to-cart" },
    { "type": "wait_for_selector", "selector": ".price[data-ready]", "state": "visible" }
  ],
  "settle": {                   // optional; applied after `actions`, before extraction
    "wait_for_selector": ".price-final",
    "load_state": "networkidle"
  }
}
```

**Allowlisted `type` → `PageMethod`:**

| `type` | `PageMethod` | required keys | value source |
|---|---|---|---|
| `click` | `PageMethod("click", selector)` | `selector` | — |
| `select_option` | `PageMethod("select_option", selector, value)` | `selector` + (`value` \| `value_from`) | literal or `value_from` |
| `fill` | `PageMethod("fill", selector, value)` | `selector` + (`value` \| `value_from`) | literal or `value_from` |
| `wait_for_selector` | `PageMethod("wait_for_selector", selector, state=?, timeout=T)` | `selector` | — |
| `wait_for_timeout` | `PageMethod("wait_for_timeout", timeout_ms)` | `timeout_ms` | — |
| `wait_for_load_state` | `PageMethod("wait_for_load_state", state)` | `state` | — |

**`value_from` resolution** (off-reactor, in `load_targets`, from the match row):
`options.<key>` → `competitor_variant_options[<key>]`; `identifier` → `competitor_variant_identifier`;
`sku` → `competitor_variant_sku`. A `value_from` that resolves to `None`/missing → `SELECTOR_BROKEN`.

**Forbidden:** any `type` not in the allowlist (notably `evaluate`, `route`, `add_init_script`,
arbitrary callables) → `SELECTOR_BROKEN`, never executed (R2 security).

**Timeout:** every `wait_*` action and the injected settle wait carry the effective browser timeout
(R10) as their `timeout`.

**Empty/absent:** `variant_selector_config IS NULL` → no interaction (FR-004, US3 AS2). `actions: []`
with only a `settle` is valid (settle-only).

---

## 3. Transport shape — `ScrapeResult` (reused, `scrape_core/items.py`, unchanged)

The browser spider emits the SAME `ScrapeResult` dataclass via the extracted shared
`build_scrape_result(...)`. Browser-relevant field population:

| Field | Browser value |
|---|---|
| `access_method` | `AccessMethod.PLAYWRIGHT_PROXY` (always; R5) |
| `proxy_provider_id` / `proxy_country` | set only when proxied (R7), else `None` |
| `attempt_number` | `1` (single attempt, R4) |
| `extraction_method` | from the reused extractor against rendered DOM (e.g. `CSS`/`JSON_LD`/`PLAYWRIGHT`) |
| `error_code` | per R3 table |
| `match_lock_key`/`_token` | threaded from `request.meta` for pipeline release (reused) |
| all scoping/observation/attempt fields | identical to HTTP |

No new field on `ScrapeResult`.

---

## 4. Configuration surface (new `Settings` knobs — env/DB-tunable, Principle IV)

| Setting | Default | Purpose |
|---|---|---|
| `SCRAPE_BROWSER_DEFAULT_TIMEOUT_MS` | `30000` | wait/nav bound when `browser_timeout_ms` unset (R10) |
| `BROWSER_CONCURRENT_REQUESTS` | `2` | browser project `CONCURRENT_REQUESTS` (R9, low bound) |
| `BROWSER_MAX_CONTEXTS` | `1` | browser project `PLAYWRIGHT_MAX_CONTEXTS` (R9) |

Reused unchanged: `MATCH_LOCK_BROWSER_TTL_SECONDS` (already exists, used by `acquire_lock(mode=
PLAYWRIGHT_PROXY)`), `SCRAPE_FLUSH_MAX_ITEMS`/`SCRAPE_FLUSH_INTERVAL_SECONDS`, `SCRAPYD_BROWSER_URLS`,
all SPEC-10/11/12 access/limit/strategy knobs.

---

## 5. State & invariants

- A browser attempt writes exactly **one** `price_observation` + **one** `request_attempt`; a
  successful attempt also upserts **one** `match_current_prices` (failure/rejected never overwrites the
  current price — reused `_flush_batch` invariant).
- Persistence is **batched** (flush at N items or T seconds) and **off-reactor** (SC-007), inherited
  from the pipeline.
- A batch carries exactly one `mode`; a browser batch never lands on an HTTP node and vice-versa
  (dispatch invariant, unchanged).
- No raw HTML/screenshot is persisted (constitution VI) — only extracted observations/attempts.
