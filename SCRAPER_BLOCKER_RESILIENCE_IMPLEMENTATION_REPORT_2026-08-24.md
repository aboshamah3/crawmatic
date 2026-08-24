# Scraper Blocker Resilience — Implementation and Production Rollout Report

**Date:** 2026-08-24  
**Project:** Crawmatic  
**Production environment:** Railway project `Crawmatic`, environment `production`  
**Implementation status:** Code complete; migration, seed, reconciliation, approved Amazon policy, and worker deployed  
**Next-run status:** Do not start the mixed-domain canary until the remaining application services are deployed and both Scrapyd projects are re-registered.

## Report bundle and original plan

This report is Part I of the implementation record. The complete, unmodified original plan is included as Part II through the adjacent source artifact:

- [Original plan — `PLAN_SCRAPER_BLOCKER_RESILIENCE_2026-08-24.md`](./PLAN_SCRAPER_BLOCKER_RESILIENCE_2026-08-24.md)
- Original-plan length: 528 lines
- Original-plan SHA-256: `5892675c601550f469a3c016db7bd4c7141c8ea7873a9a8a3e47b7513c67ee1c`

The original plan remains the requirements and acceptance-gate source of truth. This report records the implementation, validation, and production actions performed against it.

## Executive summary

The plan was implemented with separate lifecycle, versioned-profile, and adapter workstreams and then integrated into one generic platform design. Competitor profiling was retained and expanded: known domains can use seeded playbooks, while unknown domains and future customers can materialize and operate versioned methods without product-, workspace-, or merchant-specific code.

The implementation corrects premature target failure, makes fallback chains durable across HTTP and browser workers, records the exact method/profile revision used for every attempt, adds method-scoped breakers and canaries, introduces generic structured-data adapters, validates product identity before accepting prices, classifies permanent outcomes separately from operational failures, and repairs robots handling.

Production rollout steps completed during this work:

1. Applied Alembic migration `6e4a9c8f2d10`.
2. Ran and verified the idempotent domain-playbook seed.
3. Dry-ran, reviewed, and applied historical false-failure reconciliation.
4. Restored the previously approved Amazon robots policy through the authenticated API.

## Design principles preserved

- Competitor profiling remains a first-class platform capability.
- No behavior is hard-coded to Mushtryati, a particular catalog, or a fixed set of customers.
- Domain behavior is data-driven through playbooks, versioned profiles, and ordered strategy methods.
- Unknown competitors retain a generic discovery/materialization path.
- Exact identity is required for API results, URL repairs, redirects, and extracted offers.
- Historic methods are retained for rollback, measurement, quarantine, and canary recovery.
- Permanent outcomes do not poison operational health or consume paid retries.

## Implemented work

### 1. Target lifecycle and durable strategy chains

- Added explicit `chain_complete`/terminal semantics to scraper results.
- Intermediate attempt failures are persisted without prematurely marking the target failed.
- A later successful attempt can complete the same target.
- Only successful results, permanent outcomes, or an exhausted strategy chain terminalize a target.
- Added durable cross-mode handoff state for HTTP-to-browser and browser-to-other-method transitions.
- Dispatch changes a deferred target back to an in-flight state, clears stale error state, and records dispatch time, while preserving stall recovery.
- `NOT_LISTED` and `POLICY_BLOCKED` become skipped/non-operational terminal outcomes.
- Added the reusable, dry-run-first reconciliation service and Celery/admin triggers.

Primary files:

- `libs/scrape-core/scrape_core/pipelines.py`
- `libs/scrape-core/scrape_core/items.py`
- `libs/scrape-core/scrape_core/result_builder.py`
- `libs/shared/app_shared/jobs/reconciliation.py`
- `apps/workers/app/workers/tasks_jobs.py`
- `apps/api/app/routers/admin.py`

### 2. Versioned profiles and ordered strategy methods

- Added immutable scrape-profile revisions alongside the mutable current profile row.
- Added reusable adapter configuration to profiles.
- Added `domain_strategy_methods`, including priority, mode, adapter, profile/version references, outcome-conditioned entry/fallback rules, health state, cooldown, canary configuration, and supersession/retirement data.
- Preserved the legacy preferred-method fields for compatibility and rollback.
- Added exact method/profile/version/adapter/final-URL/identity fields to request-attempt audit data.
- Changed strategy statistics to key health by concrete method row while retaining legacy statistics.
- Added cursor-paginated profile-version reads and expanded strategy API responses.
- Discovery now materializes or reuses a versioned generic method for unknown domains instead of requiring a curated customer/domain entry.

Primary files:

- `alembic/versions/6e4a9c8f2d10_versioned_strategy_methods.py`
- `libs/shared/app_shared/models/strategy.py`
- `libs/shared/app_shared/models/scrape_profiles.py`
- `libs/shared/app_shared/strategy/methods.py`
- `libs/shared/app_shared/strategy/resolution.py`
- `apps/api/app/routers/scrape_profiles.py`
- `apps/api/app/routers/strategy.py`

### 3. Generic adapters and identity safety

Added a registry and reusable adapters for:

- normal HTML extraction;
- rendered Playwright HTML;
- Shopify product JSON with exact handle/variant selection and minor-unit conversion;
- public catalog JSON with exact identifiers only;
- exact-ID URL repair with bounded re-entry into the method chain;
- final-URL and product-identity validation.

The HTTP and browser spiders now resolve adapters from data rather than domain-specific branches. Root redirects, cross-product redirects, and exact API misses cannot yield a price. Whole-document out-of-stock inference was removed; a valid scoped offer wins over unrelated page text.

Primary files:

- `libs/scrape-core/scrape_core/adapters/`
- `apps/scrapers/price_monitor/spiders/generic_price_spider.py`
- `apps/scrapers-browser/price_monitor_browser/spiders/generic_browser_price_spider.py`
- `libs/scrape-core/scrape_core/validation.py`

### 4. Error classification, robots handling, and breakers

- Added permanent and identity outcome vocabulary: `NOT_LISTED`, `IDENTITY_MISMATCH`, and `POLICY_BLOCKED`.
- Added connection, TLS, certificate, HTTP/2/protocol, Twisted connection-loss, curl, Playwright, and HTTP-status mappings.
- Robots fetching now uses the configured scraper user agent, caches by origin and user agent with TTL, evaluates canonical paths, and emits a no-retry policy outcome on denial.
- Added method-scoped failure accounting, configurable circuit thresholds, cooldown, quarantine, scheduled canary selection, and canary-success recovery.
- Permanent policy/not-listed outcomes do not count against method health.

Primary files:

- `libs/scrape-core/scrape_core/errors.py`
- `libs/scrape-core/scrape_core/robots.py`
- `libs/shared/app_shared/strategy/flush.py`
- `libs/shared/app_shared/strategy/stats_buffer.py`
- `libs/shared/app_shared/config.py`

### 5. Data-driven initial playbooks

The idempotent seed creates reusable profiles and ordered method templates for:

- `amazon.sa`
- `noon.com`
- `stech.ink`
- `afaqalhasoob.com`
- `fqtoners.com`
- `alshamel.sa`
- `jarir.com`
- `extra.com`

It retains historic method rows at rollback priorities and marks the new seeded method generation with `method_version = 20260824`. Existing unrelated profiles, policies, rules, playbooks, and statistics are not deleted.

Primary file:

- `scripts/seed_domain_playbooks.sql`

## Validation performed before production

### Automated tests

- Full unit suite: `2704 passed, 1 skipped`.
- Five subsequent focused regression runs covering adapters/spiders, discovery/strategy, lifecycle/dispatch, permanent outcomes/circuits, and admin/reconciliation all passed.
- Final cancellation-preservation regression set: `7 passed`.
- `python -m compileall`: passed.
- `git diff --check`: passed.

The final reconciliation safety fix preserves an explicitly cancelled parent job while still repairing its target classifications and counters. This behavior was discovered from the production dry-run before any reconciliation writes occurred.

### Disposable PostgreSQL validation

- Applied every migration through `6e4a9c8f2d10` to a disposable PostgreSQL database.
- Ran the playbook seed twice.
- Verified the second run was idempotent and did not produce unnecessary profile-version increments.
- Verified seven resilience profiles, eight populated playbooks, and their revision rows.
- Dropped the disposable validation database and temporary local role afterward.

### Live endpoint validation

The checked-in eXtra storefront fixture exposed the current public Unbxd configuration. Its exact-ID endpoint was verified live with an exact `uniqueId`, price, currency, availability, and product URL before being stored as a versioned data-driven profile.

## Production rollout record

### Step 1 — migration

- Previous production head: `88d894a2e23b`.
- Migration deployment: `308df58b-13cc-4599-b91b-0e1df90e2c1e`.
- Deployment result: `SUCCESS`.
- Verified production head: `6e4a9c8f2d10`.
- Verified tables: `domain_strategy_methods`, `scrape_profile_revisions`.

### Step 2 — playbook seed

The production seed completed in one transaction with `ON_ERROR_STOP` enabled.

Verified production results:

- Global resilience profiles: 7.
- Seeded playbooks with non-empty method templates: 8.
- Seeded method rows at version `20260824`: 50.
- Per-domain template counts: Afaq 3, Alshamel 1, Amazon 3, eXtra 2, FQ Toners 1, Jarir 3, Noon 4, S-Tech 3.

### Step 3 — historical false-failure reconciliation

The production dry-run found 352 exact match IDs across 19 historical jobs. Each candidate satisfied the strict predicate:

- target status was `FAILED`; and
- the same workspace/job/match had a successful price observation.

Review controls used:

- Full ordered match-ID lists were returned by the auditable task.
- Per-job SHA-256 hashes were calculated for the exact ordered ID sets.
- A second dry-run reproduced all 19 counts and hashes exactly.
- Apply re-ran the preview, locked the selected job/targets, and required the same exact IDs before committing.
- Audit requester: `codex:resilience-rollout-2026-08-24`.

Results:

- Repaired targets: 352.
- Affected jobs: 19.
- Remaining failed targets with a same-job successful observation: 0.
- One job moved from `PARTIAL_FAILED` to `COMPLETED`.
- Two explicitly cancelled jobs retained `CANCELLED` while their target classifications and counters were repaired.

Final worker deployment containing the reconciliation and cancellation safety behavior:

- Deployment: `c66acc68-f578-420d-9eae-ba0e0c74fb95`.
- Result: `SUCCESS`.

### Step 4 — approved Amazon robots policy

The policy was restored through authenticated backend APIs, not direct SQL:

1. The service-authenticated admin endpoint minted a short-lived API key scoped only to `competitors:read` and `competitors:write`.
2. The tenant competitor API verified the exact competitor ID and current `RESPECT` policy.
3. `PATCH /v1/competitors/{competitor_id}` changed only `robots_policy` to `IGNORE_AFTER_APPROVAL`.
4. A tenant API read verified the changed value.
5. The temporary key was immediately revoked through the admin API and its revoked state was verified in PostgreSQL.

Changed production record:

- Workspace: `01a020de-871c-7760-8273-59f3b67d9c18`.
- Competitor: `01a020e7-cef6-7606-a1c1-9efb37d69db0`.
- Domain: `amazon.sa`.
- Before: `RESPECT`.
- After: `IGNORE_AFTER_APPROVAL`.
- PATCH status: HTTP 200.

The other production Amazon competitor already used `IGNORE_AFTER_APPROVAL` and was left unchanged. Both production Amazon records now use the previously approved policy. No other domain, competitor, workspace, legal-status value, rate limit, proxy control, or strategy setting was changed.

Temporary credential cleanup:

- Temporary key ID: `01a034c2-c974-78c2-b77f-15d947a630d1`.
- Revoke status: HTTP 204.
- Database status: `revoked`, with `revoked_at` set.

## Current production state and remaining rollout

The database migration, seed, historical repair, robots-policy restoration, and updated worker are live. At the end of this report, Railway reports PostgreSQL, Redis, PgBouncer, API, scheduler, worker, HTTP scrapers, and browser scrapers healthy.

The following is deliberately not claimed as complete:

- The API, scheduler, HTTP scraper, and browser scraper have not yet been redeployed from this implementation workspace.
- The currently live API image predates the new migration file, although it remains operational against the additive schema and reports database head `6e4a9c8f2d10`.
- The HTTP and browser Scrapyd projects must be re-registered after their service deployments; otherwise new spider code will not be available to dispatched jobs.
- The 100–200 target mixed-domain canary and the plan's domain-specific acceptance matrix have not yet been run against the fully deployed release.

Therefore, the next production scrape run should wait for this sequence:

1. Deploy scheduler, HTTP scrapers, browser scrapers, and API from the reviewed implementation.
2. Re-register both Scrapyd projects after their service deployments.
3. Verify `/version`, service readiness, and migration-head agreement.
4. Run the plan's 100–200 target mixed-domain canary.
5. Confirm zero stuck targets, exact target/job counter agreement, zero false-failed successes, zero paid retries after permanent outcomes, and complete method/profile revision audit fields.

## Acceptance evidence summary

| Requirement | Evidence/status |
|---|---|
| Generic across customers/products/competitors | Versioned playbooks/methods plus unknown-domain materialization; no customer-specific runtime branching |
| Preserve competitor profiling | Existing profiles/methods retained; revisions and method audit added |
| No target failed when later attempt succeeds | Lifecycle implementation plus historical reconciliation; production residual count is zero |
| Permanent outcomes avoid paid retry | Outcome classification and ordered fallback tests passed |
| Exact product identity | Shared identity adapter and redirect/API exact-ID guards tested |
| Method health isolated | Method-row statistics, breakers, cooldowns, and canaries implemented/tested |
| Robots behavior corrected | Configured-UA/TTL/canonical-path implementation tested; approved Amazon policy restored via API |
| Reproducible attempts | Method/profile/version/adapter/final-URL/identity audit fields migrated and wired |
| Initial eight domains seeded | Verified eight populated production playbooks and 50 versioned method rows |
| Production canary acceptance | Pending coordinated remaining-service deployment and Scrapyd registration |

## Files added or materially changed for this plan

Key additions include:

- `alembic/versions/6e4a9c8f2d10_versioned_strategy_methods.py`
- `libs/scrape-core/scrape_core/adapters/`
- `libs/shared/app_shared/jobs/reconciliation.py`
- `libs/shared/app_shared/profiles/revisioning.py`
- `libs/shared/app_shared/strategy/methods.py`
- `tests/fixtures/adapters/`
- `tests/unit/test_admin_target_reconciliation.py`
- `tests/unit/test_discovery_versioned_method.py`
- `tests/unit/test_method_circuit_persistence.py`
- `tests/unit/test_playbook_method_materialization.py`
- `tests/unit/test_scrape_adapters.py`
- `tests/unit/test_strategy_method_resolution.py`
- `tests/unit/test_target_reconciliation.py`
- `tests/unit/test_target_reconciliation_task.py`
- `tests/unit/test_target_terminal_guard.py`
- `tests/unit/test_versioned_strategy_methods.py`

Existing implementation files across the API, workers, shared models/repositories, scraper core, both spiders, configuration, error vocabulary, robots middleware, strategy flushing/resolution, seed SQL, and regression tests were updated without removing unrelated user work from the dirty workspace.

## Final conclusion

The plan's implementation is complete in the workspace, and production data preparation steps 1–4 are complete and verified. The platform retains competitor profiling while making strategy behavior reusable and safe for unrelated future customers and competitors. Production should not begin the next scrape run until the remaining application services are deployed together, both Scrapyd projects are registered, and the mixed-domain canary passes the original plan's acceptance gates.

---

## Appendix A — Original plan (verbatim)

The complete original plan is reproduced below without modification.

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
