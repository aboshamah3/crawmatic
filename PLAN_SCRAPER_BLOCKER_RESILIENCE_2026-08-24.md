# Scraper blocker resilience implementation plan

Date: 2026-08-24  
Source run: `01a0336a-f47b-73db-a01b-05b67f0f7f55`  
Workspace: `01a020de-871c-7760-8273-59f3b67d9c18`

## Scope and status

This document is the handoff for a fresh implementation session. The current
session performed read-only production analysis and public-page/API probes
only. It did not change scraper code, backend code, database configuration,
competitor configuration, or production data.

The objective is not to replace one scraper with another. It is to retain all
known access and extraction methods as versioned profile candidates, select the
right pair for a competitor and failure condition, and fall through to another
candidate when the observed outcome warrants it.

## Final run facts

The run completed `PARTIAL_FAILED` at 2026-08-24 15:33:56 UTC:

| Result | Count |
|---|---:|
| Total targets | 4,372 |
| Completed | 2,315 |
| Failed | 2,057 |
| Skipped | 0 |

Final failure codes:

| Error code | Count |
|---|---:|
| `PRICE_NOT_FOUND` | 1,103 |
| `HTTP_403` | 782 |
| `BLOCKED` | 107 |
| `TIMEOUT` | 27 |
| `HTTP_404` | 25 |
| `UNKNOWN_ERROR` | 12 |
| `PROXY_FAILED` | 1 |

Amazon (`1,207` failures) and Noon (`791` failures) account for 97.1% of
the reported failures. There is also an independent lifecycle bug: 16 targets
reported as failed have a successful price observation later in the same job.
The corrected run-level result, without changing any scrape result, would be
2,331 completed and 2,041 failed.

## Proven findings and test evidence

### 1. Target lifecycle accounting is terminalizing intermediate failures

Sixteen failed targets contain a later successful observation in the same job:

| Competitor / final error | False failures |
|---|---:|
| S-Tech / `TIMEOUT` | 6 |
| Afaq / `TIMEOUT` | 5 |
| Noon / `HTTP_403` | 2 |
| S-Tech / `UNKNOWN_ERROR` | 2 |
| S-Tech / `HTTP_403` | 1 |

Root cause:

- `generic_price_spider.errback()` emits the failed attempt before dispatching
  its retry (`apps/scrapers/price_monitor/spiders/generic_price_spider.py`,
  around lines 749-767).
- `BatchedPersistencePipeline` terminalizes the job target for every emitted
  item (`libs/scrape-core/scrape_core/pipelines.py`, around lines 358-405).
- `mark_target()` intentionally refuses to change an already terminal target
  (`libs/shared/app_shared/jobs/targets.py`, around lines 84-112).
- Therefore, the first failed attempt wins the target status permanently even
  when attempt 2 succeeds and writes the correct price.

This is a reporting/orchestration defect, not a scraping-method failure.

### 2. Amazon `PRICE_NOT_FOUND`: raw HTTP contains no usable price; rendered DOM does

Raw-method gate on five targets tried the current proxied product URL,
canonical `/dp/{ASIN}`, query variants, AOD, direct HTTP, and a reader service.
The normal pages returned HTTP 200 and roughly 299-311 KB, but contained no
current-price signals usable by the production profile. AOD returned 404 and
the reader route returned a challenge/error response.

A direct and proxied Playwright gate on a previously priced target both
rendered a roughly 1 MB DOM and recovered `SAR 498.29`, matching its prior
price.

A larger stratified direct-Playwright cohort tested 30 current PNF targets:

| Cohort | Result |
|---|---:|
| HTTP 200 | 30/30 |
| Browser/block errors | 0/30 |
| Previously priced targets recovered | 20/20 |
| Recovered prices within 0.5x-2x prior price | 20/20 |
| Never-priced targets with no price candidate | 10/10 |
| Never-priced targets with unavailable copy | 10/10 |

Proven route: rendered Playwright DOM, direct browser first, proxied browser if
the direct browser receives a block/shell. Existing JSON-LD/CSS/embedded
JSON/regex extraction remains in place over the rendered DOM.

Important validation finding: whole-document text is not a safe stock test.
Several correctly priced Amazon pages also contained unrelated “out of stock”
copy elsewhere in the document. Availability must be scoped to the selected
offer/buy box, and a valid current offer must take precedence over unrelated
stock text.

### 3. Amazon robots `BLOCKED`: policy/config drift and stale decisions

All 107 failed URLs were re-evaluated against the current Amazon robots file,
using both the project parser and the configured browser user agent. All 107
original paths and all 107 canonical `/dp/{ASIN}` paths are currently allowed.
The robots payload was identical across the tested host/UA variants.

The production competitor is currently `RESPECT`, although the project history
records explicit operator approval for Amazon `IGNORE_AFTER_APPROVAL` on
2026-07-27. The implementation also caches robots decisions for the spider
lifetime without revalidation and fetches robots with the fetcher's default UA
rather than the configured scraper UA.

A rendered follow-up sample covered every distinct stratum available in the
blocked group (six targets): 6/6 fetched with HTTP 200, four exposed a price,
one was unavailable, and one received Amazon's small generic shell. The shell
case is the condition for proxied-browser fallback, not a robots denial.

Proven route: restore the already-approved policy, canonicalize to `/dp/{ASIN}`
before evaluation, and make robots denials policy outcomes rather than retryable
network failures. If `RESPECT` is retained for any domain, add TTL/revalidation
and use the configured UA when fetching the robots file.

### 4. Noon product pages: current route is broadly blocked; public catalog JSON works

The previously successful product-page recipe no longer works against the
current provider route. Tests included:

| Product-page method | Samples | Result |
|---|---:|---|
| Chrome/plain TLS and navigation-header variants, SA/KW/QA/AE | 30 | 30 HTTP 403 |
| Additional EG/OM/BH/JO/US/DE/no-country exits | 14 | 14 HTTP 403 |
| Direct and proxied Playwright | 6 | 6 HTTP/2 protocol failures |
| Reader service, cached and no-cache | 10 | 10 small error documents |

Changing request headers, TLS impersonation, country, browser, or reader cache
is not a current workaround. Repeating those page requests burns time and
proxy traffic.

The public storefront endpoint
`/_svc/catalog/api/v3/u/search/?q={SKU}&page=1`, through the existing Saudi
proxy, returned HTTP 200 JSON while the product page returned 403.

Stratified endpoint tests:

| Cohort | HTTP 200 | Exact SKU hits with price |
|---|---:|---:|
| 20 prior-priced + 10 never-priced current `HTTP_403` targets | 30/30 | 16/30 |
| All 10 current Noon timeout/unknown targets | 10/10 | 3/10 |
| Combined | 40/40 | 19/40 |

All exact hits were buyable and had positive prices. All 15 exact hits in the
prior-priced portion of the first cohort were within 0.5x-2x the previous
price. A missing exact hit means the requested SKU is no longer in the current
search index; fuzzy results must never be substituted.

Identifier handling must use `competitor_variant_identifier` first and accept
both `N...` and `Z...` Noon identifiers. URL fallback must capture the generic
final `/[A-Z0-9]+/p/` segment; an `N`-only regex loses valid `Z` products.

Proven route: exact-SKU public catalog JSON plus platform-field extraction.
Use `sale_price` when positive, otherwise `price`; record `price` as the
list/old price when it differs; use `is_buyable` for stock. Keep every product
page method as a cooled-down canary/fallback so it can be restored when route
health recovers.

### 5. S-Tech: public Shopify product JSON avoids all transport failures

All 15 final S-Tech failures (`HTTP_403`, `PROXY_FAILED`, 10 timeouts, and three
unknown transport errors) were tested through the public `{product_url}.js`
endpoint using direct impersonated HTTP:

| Result | Count |
|---|---:|
| HTTP 200 JSON | 15/15 |
| Valid price | 15/15 |
| Exact match to the latest prior price | 15/15 |
| Transport/parser error | 0/15 |

Ten were currently available and five unavailable, while all retained a
public price. Shopify prices are integer minor units and must be divided by 100
through a profile transform, never treated as whole SAR.

Proven route: `SHOPIFY_PRODUCT_JSON` over direct HTTP as primary. Keep proxied
HTML and direct HTML as fallback/canary methods.

### 6. Afaq: ordinary direct retry works; one listing is gone

All five timeout targets were re-fetched with direct Chrome-impersonated HTTP.
All five returned HTTP 200 in 0.3-7.6 seconds, and the existing production
JSON-LD profile recovered exactly the latest prior price on all five.

The one HTTP 404 target remains 404. Its attempted `.js` route is not a JSON
product API on this storefront, and all tested internal search paths also
returned 404 with no matching product link.

Proven route: direct HTTP plus bounded retry/backoff for timeouts; terminal
`NOT_LISTED` for the exact 404 after the repair/search adapter finds no exact
identity.

### 7. FQ Toners and Alshamel PNF: deleted product URLs redirect to the homepage

All 19 targets were tested without following redirects:

| Domain | Targets | Result |
|---|---:|---|
| `fqtoners.com` | 14 | 14/14 HTTP 302 to site root |
| `alshamel.sa` | 5 | 5/5 HTTP 302 to site root |

Following the redirect produces a normal HTTP 200 homepage. Rendering those
pages does not restore product identity. Worse, FQ's homepage exposes a valid
JSON-LD price of `92.02`, so a browser or extractor that ignores the final URL
can assign the homepage's featured-product price to every deleted target.

Proven route: fetch without redirects (or retain redirect history), validate
product identity/final URL before extraction, and classify an exact product URL
redirecting to the storefront root as `NOT_LISTED`. Do not run the price
extractor on the destination homepage.

### 8. HTTP 404 URL recovery

| Competitor | Tested | Finding / route |
|---|---:|---|
| Amazon | 5 | Direct product checks produced two true 404s and three small generic shells; exact-ASIN searches found zero exact product links. Treat the exact listings as not listed; do not accept similar ASINs. |
| eXtra | 12 | Three proxied page checks confirmed 404. The public Unbxd catalog endpoint returned 200 for all 12 queries but zero exact `uniqueId` matches. Treat as not listed; retain Unbxd as exact-ID repair/price method for future cases. |
| Jarir | 7 | Rendered exact product-ID search found one same-ID listing with a changed slug and six no-result IDs. The repaired listing returned HTTP 200 and JSON-LD `SAR 679.00`. |
| Afaq | 1 | Exact URL and internal search variants remain 404 with no product match. |

The correct workaround for 404 is not blind retry. Run a domain-specific exact
identity repair step; update/retry only when the same immutable product ID is
found. Otherwise record `NOT_LISTED` without fabricating a price.

### 9. Unknown transport classification is too coarse

The 12 `UNKNOWN_ERROR` targets are recognizable transport classes:

- CONNECT tunnel 502 (`curl` code 7): proxy failure;
- abrupt TLS close (`curl` code 35): TLS/connection failure;
- certificate issuer failure (`curl` code 60): TLS verification failure;
- HTTP/2 SETTINGS/protocol failure (`curl` code 16): protocol failure;
- Scrapy “Ignoring non-200 response”: recover the response status from the
  failure object and classify the actual HTTP code;
- Twisted `ConnectionLost`: connection failure.

Each should drive a defined fallback and metric instead of landing in
`UNKNOWN_ERROR`.

## Required profile architecture

### Preserve the two independent axes

Access and extraction remain separate concepts:

- Access transport: direct HTTP, direct retry, impersonated HTTP, proxied HTTP,
  direct Playwright, proxied Playwright.
- Request/response adapter: normal product HTML, rendered HTML, Shopify product
  JSON, public catalog search JSON, exact-ID URL repair, or another platform
  adapter.
- Extraction: JSON-LD, embedded JSON, CSS, regex, platform JSON fields, and
  rendered-DOM extraction.

A runnable method candidate is the versioned combination of an access
transport and a scrape profile/adapter. Do not overwrite a previous winner
when a new candidate is proven.

### Extend rather than replace the current models

Keep `scrape_profiles` as the reusable extraction/request definition. Add:

1. `scrape_profiles.adapter_config JSONB` for endpoint templates, identifier
   sources, exact-match predicates, JSON field paths, transforms, locale/
   currency rules, redirect rules, and identity rules.
2. `scrape_profiles.version` (or immutable profile revisions) so an attempt can
   be reproduced after a profile changes.
3. A new workspace-owned `domain_strategy_methods` association with at least:
   `domain_strategy_profile_id`, `scrape_profile_id`, `access_method`, ordered
   priority, `fallback_on` error/outcome set, enabled state, proof state
   (`CANDIDATE`, `PROVEN`, `QUARANTINED`, `DISABLED`), cooldown/canary times,
   proof sample size, and timestamps. Keep old rows when ordering changes.
4. A preferred method pointer on `domain_strategy_profiles` for the fast path.
   Retain the existing preferred access/extraction columns during migration as
   backward-compatible cached fields.
5. Stats keyed by the combined strategy-method row, not only a free-text access
   or extraction method. Existing `strategy_attempt_stats` can be migrated or
   supplemented; do not delete its history.
6. Attempt audit fields for strategy method ID, scrape profile ID/version,
   adapter key, final URL, identity-validation result, and whether the result
   was terminal for the target.

Use current enum members where they already express the concept:
`PLATFORM_JSON` exists as an extraction method, and
`SHOPIFY_PRODUCT_JSON`/`PLAYWRIGHT_RENDERED` already exist as adapter keys.
Add `PLAYWRIGHT_DIRECT` to `AccessMethod`; the browser spider currently records
an unproxied browser request as `PLAYWRIGHT_PROXY` with null proxy fields, which
prevents accurate method health/cost reporting. Add an impersonated-HTTP method
only if it is a distinct executable transport rather than a header option.

### Activate the adapter layer

`ScrapeProfile.adapter_key` is validated and persisted but is not consumed by
either scraper. Implement a registry such as:

- `DEFAULT_HTTP` -> current HTML extraction pipeline;
- `PLAYWRIGHT_RENDERED` -> current pipeline over rendered DOM;
- `SHOPIFY_PRODUCT_JSON` -> exact handle/product response, variant selection,
  minor-unit price transform;
- `PUBLIC_CATALOG_JSON` or explicit Noon/Unbxd adapters -> request template,
  recursive/exact identifier match, field-path extraction;
- `EXACT_ID_URL_REPAIR` -> domain search, exact immutable-ID assertion,
  canonical URL output, then re-enter the target's candidate list.

Adapters return one common result containing candidate price, old price,
currency, stock, canonical/final URL, identity evidence, and extraction method.
They must pass through the existing money validation boundary.

### Outcome-conditioned resolver

Resolve an ordered candidate list for the target. Advance based on the actual
outcome, not just exception retries:

- `HTTP_200 + PRICE_NOT_FOUND` -> next extraction/access candidate (required
  for Amazon raw HTTP -> browser).
- `HTTP_403`, block signature, small generic shell -> next access candidate;
  update a per-domain/per-method breaker.
- timeout, proxy failure, TLS/protocol failure -> bounded retry or next
  transport/adapter.
- `HTTP_404` -> exact-ID repair candidate only; no ordinary retry loop.
- redirect to site root, cross-product identity, or exact API miss -> terminal
  `NOT_LISTED`/`IDENTITY_MISMATCH`.
- robots rejection -> `SKIPPED`/`POLICY_BLOCKED`, never a paid retry, unless an
  approved `IGNORE_AFTER_APPROVAL` policy applies.
- valid current offer -> success takes precedence over unrelated OOS text.

Open circuit breakers per `(domain, strategy_method)` after a configured streak
or rate, not for the whole competitor. A quarantined method remains stored and
receives a small scheduled canary sample so it can recover automatically.

### Correct target terminalization before adding more retries

Add an explicit `terminal`/`chain_complete` flag to the runtime `ScrapeResult`.
Persist every observation and request attempt, but call `mark_target()` only for:

- a successful result;
- a terminal unavailable/not-listed/policy outcome; or
- the last exhausted strategy candidate.

Intermediate failures must leave the target `STARTED` (or a new non-terminal
attempting state). A later success must be allowed to complete it. Preserve the
existing protection against genuinely late results reopening a finished target.
The strategy-chain owner, not the generic item pipeline, decides terminality.

After deployment, run a one-time reconciliation for affected jobs: where a
target is `FAILED` but the same job/match has a successful observation, change
the target to `COMPLETED`, clear its target error, and recompute job counters.
Make this an auditable admin task with dry-run output; do not embed an ad-hoc SQL
mutation in deployment.

## Initial domain playbooks

| Domain | Ordered behavior |
|---|---|
| `amazon.sa` | Canonical ASIN. Use a historically healthy raw method when its method circuit is closed; on raw PNF/shell use direct rendered Playwright; on browser block/shell use proxied Playwright. Retain raw direct/proxy methods as candidates/canaries. Scoped buy-box availability only. |
| `noon.com` | Saudi-proxied public catalog JSON exact-SKU lookup is primary while the product-page circuit is open. Retain proxied page HTTP, direct/proxied browser, and prior page extraction as quarantined canaries. Exact miss -> not listed, never fuzzy substitution. |
| `stech.ink` | Direct Shopify product JSON primary; direct HTML and proxied HTML retained as fallback/canary. |
| `afaqalhasoob.com` | Direct impersonated HTML + JSON-LD, one bounded retry/backoff; browser only after repeated transport failure. 404 -> exact repair, then not listed. |
| `fqtoners.com` | Product request with redirect identity validation. Root redirect -> not listed before extraction. Retain product-page JSON-LD/CSS methods for URLs that remain valid. |
| `alshamel.sa` | Same root-redirect guard as FQ; retain existing valid-product extraction methods. |
| `jarir.com` | Existing direct product profile. On 404, rendered exact numeric-ID search; rewrite URL only for the same ID, then retry normal JSON-LD extraction. |
| `extra.com` | Existing product-page method retained. On 404/block, public Unbxd exact-`uniqueId` adapter; accept price/URL only on exact ID, otherwise not listed. Keep API configuration versioned because the public storefront site key can rotate. |

## File-by-file implementation sequence for the fresh session

### Phase 0: lifecycle correctness

1. Add terminal-chain semantics to
   `libs/scrape-core/scrape_core/items.py` and both spiders.
2. Change `libs/scrape-core/scrape_core/pipelines.py` so intermediate attempt
   rows are persisted but do not terminalize `scrape_job_targets`.
3. Extend tests around `libs/shared/app_shared/jobs/targets.py` without
   weakening terminal-state protection.
4. Add regression cases: failed attempt -> successful retry, failed attempt ->
   rate-limited defer, and fully exhausted chain -> one terminal failure.
5. Implement the dry-run reconciliation task and API/admin trigger, then review
   its output before any repair execution.

### Phase 1: versioned multi-method profiles

1. Add an Alembic migration under `alembic/versions/` for adapter config,
   profile revisioning, `domain_strategy_methods`, audit FKs/fields, indexes,
   and RLS matching the workspace-owned parent pattern.
2. Add/update ORM models in:
   - `libs/shared/app_shared/models/scrape_profiles.py`
   - `libs/shared/app_shared/models/strategy.py`
   - `libs/shared/app_shared/models/observations.py`
3. Extend profile schemas, validation, repository, bulk upsert, and API routes in
   `apps/api/app/schemas/scrape_profiles.py`,
   `libs/shared/app_shared/profiles/`, and
   `apps/api/app/routers/scrape_profiles.py`.
4. Extend domain-strategy API/read models so operators can see the complete
   ordered method list, proof state, health, cooldown, and version history.
5. Backfill every current preferred method as the first `PROVEN` method row;
   never discard the old preferred values or stats.

### Phase 2: adapters, identity, and extraction

1. Add an adapter registry under `libs/scrape-core/scrape_core/adapters/`.
2. Implement Shopify product JSON, Noon catalog exact-SKU, eXtra Unbxd exact-ID,
   and exact-ID URL-repair adapters with fixture-driven unit tests.
3. Wire `AdapterKey` into both spiders; currently it is stored but unused.
4. Extend `libs/scrape-core/scrape_core/extraction/pipeline.py` so adapter
   candidates use `ExtractionMethod.PLATFORM_JSON` while retaining the entire
   current JSON-LD -> embedded JSON -> CSS -> regex fallback chain for HTML.
5. Add central final-URL/product-identity validation before extraction. Include
   root redirects, canonical ASIN/SKU/handle checks, and exact API predicates.
6. Replace whole-document OOS sniffing with profile-scoped offer/availability
   rules and explicit precedence for a valid current offer.

### Phase 3: cross-mode strategy execution

The current dispatch batches a target into one static profile mode in
`libs/shared/app_shared/jobs/batching.py`, and the HTTP spider discards terminal
`PLAYWRIGHT_PROXY` intent because it cannot execute it. Implement a durable
cross-mode handoff:

1. The strategy resolver returns an ordered candidate list, not only one
   preferred access/extraction pair.
2. When an HTTP candidate returns a fallback-triggering outcome, persist the
   intermediate attempt and enqueue the next candidate on the correct HTTP or
   browser node while keeping the job target non-terminal.
3. Carry strategy-method ID, attempt ordinal, and chain token through dispatch
   for idempotency and locking.
4. Split direct browser from proxied browser in the audit enum and browser
   context selection.
5. Allow browser PNF/block to continue to another candidate rather than the
   current browser spider's unconditional one-attempt stop.
6. Keep batch sizing by actual node mode; derive mode from the selected
   candidate rather than the target's single default profile.

### Phase 4: error classification, robots, and breakers

1. Extend `scrape_core.blocking`/exception classification for curl CONNECT,
   TLS, certificate, HTTP/2, Twisted connection loss, and Scrapy HttpError
   response status.
2. Add `NOT_LISTED`, `IDENTITY_MISMATCH`, `POLICY_BLOCKED`, and protocol/TLS
   outcome vocabulary as needed. Map policy/no-listing to skipped or a distinct
   non-operational state rather than ordinary scraper failure.
3. Update `libs/scrape-core/scrape_core/robots.py` with configured-UA fetch,
   TTL/revalidation, canonical-path evaluation, and no-retry policy behavior.
4. Restore Amazon's operator-approved robots policy through the normal audited
   backend path only after code/test review.
5. Add method-scoped breakers and canary recovery; integrate with existing
   proxy spend/cooldown controls.

### Phase 5: seed and rollout

1. Seed versioned method rows/playbooks for the eight domains in the table
   above. Do not delete any existing scrape profile, access policy, domain rule,
   playbook, or strategy stats row.
2. Canary in this order: S-Tech JSON, Afaq retry accounting, Jarir URL repair,
   Amazon browser fallback, Noon catalog API, eXtra exact-ID classification,
   Salla root-redirect guards.
3. Compare method-level request count, proxy bytes, browser seconds, exact
   identity rate, extraction rate, terminal outcome, and price agreement.
4. Expand only after acceptance gates pass; leave prior methods available for
   immediate operator rollback by reordering, not code deployment.

## Required tests and acceptance gates

### Unit/fixture tests

- Every adapter: exact match, fuzzy-only response, missing fields, malformed
  JSON, zero/null sale price, list-price handling, stock handling, currency,
  and price transform.
- Identity: same canonical product, allowed locale redirect, changed slug with
  immutable ID, root redirect, cross-product redirect, and final URL mismatch.
- Strategy resolver: each failure trigger, candidate cooldown, open circuit,
  canary, budget exhaustion, cross-mode handoff, and complete exhaustion.
- Lifecycle: intermediate failure never terminalizes; later success completes;
  final failure terminalizes once; duplicate/late messages remain idempotent.
- OOS precedence: priced buy box plus unrelated OOS text must remain a priced
  success.

### Live canary minimums

| Domain/method | Minimum gate |
|---|---|
| Amazon rendered direct | At least 30 stratified targets; >=95% fetch, 100% recovery among still-available prior-priced samples, zero identity mismatches |
| Amazon rendered proxy fallback | At least 10 direct-shell/block samples or all available if fewer; >=90% fetch without cross-product results |
| Noon catalog API | At least 50 stratified SKUs; >=98% endpoint response, 100% exact-match enforcement, zero fuzzy substitutions |
| S-Tech Shopify JSON | At least 30 targets or full active catalog if smaller; >=99% response and price agreement with HTML/prior observations |
| Afaq direct retry | At least 20 normal plus timeout-history targets; >=95% within configured timeout and exact price agreement |
| Jarir URL repair | All known 404s plus at least 20 healthy IDs; 100% immutable-ID validation |
| eXtra Unbxd | All known 404s plus at least 20 healthy exact IDs; 100% exact-ID validation |
| FQ/Alshamel redirect guard | All known root redirects plus at least 20 healthy product URLs; zero homepage/cross-product prices |

Run-level acceptance:

- Zero failed targets with a successful observation in the same job.
- Zero paid retries after a permanent robots or exact-not-listed decision.
- Zero extracted prices when product identity validation fails.
- Every request attempt identifies its strategy method and profile revision.
- Old methods remain listed, measurable, reorderable, and canary-testable.
- A 100-200 target mixed-domain canary completes with no stuck targets and
  counters equal to terminal target rows.

## Fresh-session starting checklist

1. Read this plan and the original
   `HANDOVER_WOO_SYNC_AND_MUSHTRYATI_RUN_2026-08-24.md`.
2. Inspect the dirty worktree and preserve unrelated user changes.
3. Implement Phase 0 first; its regression test must fail before the fix and
   pass afterward.
4. Design/review the migration and profile API contract before implementing
   adapters.
5. Implement one vertical slice (S-Tech Shopify JSON) through profile ->
   resolver -> scraper -> attempt audit -> terminalization before adding Noon,
   Amazon, and repair adapters.
6. Use the tested cohorts and acceptance gates above; do not mutate production
   configuration until the code, migration, and live canary evidence are
   reviewed.
