# Feature Specification: Browser Scraping Service

**Feature Branch**: `014-browser-scraping-service`

**Created**: 2026-07-05

**Status**: Draft

**Input**: User description: "SPEC-14 — Browser Scraping Service (internal JS fallback). A second, separately-deployed Scrapyd service running a scrapy-playwright browser spider (generic_browser_price_spider) that renders JavaScript product pages the HTTP spider cannot extract, waits for a configured selector, optionally performs variant-selection interaction, optionally routes through the internal proxy pool (PLAYWRIGHT_PROXY), and persists observations/current-price/attempts under the same reactor-safety, SSRF, robots, batching, and price_analysis-handoff rules as the HTTP spider. Browser concurrency is kept low. Only browser-mode matches are routed to the browser service."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A JavaScript-rendered product page yields a price (Priority: P1)

Some competitor product pages render their price client-side (the price is not in the initial
HTML the HTTP spider receives), so the existing HTTP spider extracts nothing usable. An operator
marks the relevant scrape profile as **browser mode**. From then on, when that match is scraped,
a real browser loads the page, executes its JavaScript, waits until the configured price element
is present, and only then extraction runs — producing the same kind of price observation the HTTP
spider produces for static pages. The operator sees a current price for a page that previously
returned nothing.

**Why this priority**: This is the entire reason the feature exists — turning a JS-only page into
a persisted price. Without it there is no browser scraping. It is the standalone MVP: given a
browser-mode profile and a JS fixture page, one observation/current-price/attempt is written.

**Independent Test**: Deploy the browser spider to a browser Scrapyd node, schedule it directly
against a browser-mode match whose fixture page renders its price only after JS runs (with a
`wait_for_selector` set), and confirm exactly one price observation, one current-price update, and
one request attempt are persisted, and a `price_analysis` task is emitted for the affected variant.

**Acceptance Scenarios**:

1. **Given** a browser-mode scrape profile with a `price_selector` and a `wait_for_selector`, and a
   fixture page that injects the price via JavaScript after load, **When** the browser spider
   scrapes the match, **Then** the browser waits for the selector, extraction reads the rendered
   price, and one observation + one current-price + one request attempt are persisted with a valid
   monetary value.
2. **Given** the same profile, **When** the configured `wait_for_selector` never appears within the
   profile's `browser_timeout_ms`, **Then** the attempt terminates as a failure with an appropriate
   error code (a timeout, not a hang), one failed request attempt is recorded, and no bogus
   observation is written.
3. **Given** a successful browser scrape, **When** persistence completes, **Then** the spider emits
   exactly one `price_analysis` task for the affected variant and computes no alert, variant state,
   or webhook itself (handoff parity with the HTTP spider).
4. **Given** a browser-mode match, **When** the page renders its price directly in server HTML (no
   JS needed), **Then** the browser spider still extracts and persists it correctly (browser mode is
   a superset of what static extraction can read).

---

### User Story 2 - Only browser-mode work reaches the separate browser service (Priority: P1)

The browser service is a **separate deployment** from the HTTP scraping service — its own image
(with a real browser installed), its own Scrapyd node pool, and a deliberately low concurrency so
expensive browser sessions do not overwhelm the host. When a job is dispatched, targets whose
resolved scrape mode is **browser** are grouped and sent to the browser service running the browser
project/spider; targets whose mode is **HTTP** continue to go to the HTTP service running the HTTP
spider. A browser target is never scheduled onto an HTTP node, and an HTTP target is never scheduled
onto a browser node.

**Why this priority**: Correct routing is what makes US1 reachable in production. Sending a browser
target to the HTTP service (no browser) or an HTTP target to the scarce browser service would either
fail to render or waste the constrained browser capacity. Together with US1 this is the MVP:
browser-mode matches actually get browser-scraped, end to end, through dispatch.

**Independent Test**: Dispatch a job whose targets mix HTTP-mode and browser-mode matches across the
same and different domains, and confirm browser-mode batches are scheduled to the browser node pool
running the browser project + browser spider, HTTP-mode batches to the HTTP node pool running the
HTTP spider, no batch mixes modes, and a retried dispatch routes each batch to the same node and
spider as the first (no double-run).

**Acceptance Scenarios**:

1. **Given** a job with both HTTP-mode and browser-mode targets, **When** it is dispatched, **Then**
   browser-mode targets are batched separately from HTTP-mode targets (a batch carries exactly one
   mode) and each browser batch is scheduled to the browser node pool with the browser project and
   `generic_browser_price_spider`, while HTTP batches go to the HTTP node pool with the HTTP spider.
2. **Given** a browser-mode batch, **When** the dispatch task schedules it, **Then** it authenticates
   to the browser Scrapyd node (basic auth) exactly as it does for the HTTP node, and the same
   idempotency guard and in-flight match locks apply, so a retried dispatch does not run the batch
   twice.
3. **Given** a domain whose targets are all HTTP mode, **When** the job is dispatched, **Then** no
   batch is sent to the browser node pool for that job.
4. **Given** the browser service is deployed, **When** it starts, **Then** it runs as its own Scrapyd
   process with the browser project baked into its image, a browser runtime installed, and a bounded
   number of concurrent browser jobs/sessions.

---

### User Story 3 - Variant selection before reading the price (Priority: P2)

Some product pages show one product with selectable variants (size, colour, pack), and the price
only reflects the intended variant after the shopper selects it in the page. When a browser-mode
profile carries a **variant-selection configuration**, the browser performs the configured
interaction (e.g. selecting the option that corresponds to the match's variant) and waits for the
page to settle before extraction, so the persisted price is the price of the correct variant rather
than a default.

**Why this priority**: Extends US1 to variant-level correctness on interactive pages — valuable but
not required to prove browser scraping works. A browser-mode page without variant interaction (US1)
is already useful.

**Independent Test**: Point the browser spider at a fixture whose displayed price changes only after
a variant option is selected, with a `variant_selector_config` on the profile, and confirm the
persisted observation is the post-selection price, while the same page scraped without the config
yields the default price.

**Acceptance Scenarios**:

1. **Given** a browser-mode profile with a `variant_selector_config` and a page whose price updates
   after selecting a variant, **When** the spider scrapes it, **Then** the spider performs the
   configured selection, waits for the page to update, and extracts the post-selection price.
2. **Given** a browser-mode profile with **no** `variant_selector_config`, **When** the spider
   scrapes an interactive page, **Then** it skips variant interaction and extracts the page's default
   price (no interaction is attempted).
3. **Given** a `variant_selector_config` whose target element cannot be found or interacted with,
   **When** the spider scrapes, **Then** the attempt fails cleanly with an error code (no hang, no
   partially-interacted mystery state persisted as a valid price).

---

### User Story 4 - Bounded, guardrail-parity, optionally-proxied browsing (Priority: P2)

The browser path must obey every safety and scale rule the HTTP path obeys, plus keep its own
resource use bounded. Browser sessions are expensive, so concurrency is capped low. Every URL the
browser navigates to — and every redirect it follows — is re-validated against the SSRF rules using
the resolved IP at connection time; robots policy is resolved per domain; database writes happen off
the reactor thread in batches, never one commit per item; and when the resolved access method is
**PLAYWRIGHT_PROXY**, the browser routes through the internal proxy pool exactly as the HTTP path
routes proxied requests, logging the attempt with that access method.

**Why this priority**: Hardening and correctness-at-scale for the browser path. US1–US3 prove the
capability; this story ensures it is safe, isolated, and honest about its transport. It reuses the
existing SSRF, robots, proxy-assignment, rate-limit, lock, and persistence machinery rather than
re-implementing them.

**Independent Test**: Run the browser spider against a set of targets and confirm: concurrent browser
sessions never exceed the configured low cap; a request whose (resolved) host is private/internal —
including one that 302-redirects to an internal address — is refused before any page body is read and
recorded as blocked; when the access policy assigns a proxy, the browser context uses it and the
request attempt records `PLAYWRIGHT_PROXY`; and persistence for N targets performs far fewer than N
commits, off the reactor thread.

**Acceptance Scenarios**:

1. **Given** the browser node's concurrency configuration, **When** many browser-mode targets are
   scheduled, **Then** the number of simultaneously-open browser sessions/contexts stays within the
   configured low bound (no unbounded browser fan-out).
2. **Given** a browser-mode target whose URL resolves to a private/loopback/link-local/internal
   address, or which redirects to one, **When** the spider attempts it, **Then** the navigation is
   refused before the page body is processed and a blocked attempt is recorded (SSRF parity with the
   HTTP spider, re-validated on every hop against the resolved IP).
3. **Given** a resolved access policy that assigns `PLAYWRIGHT_PROXY`, **When** the browser scrapes,
   **Then** the browser session routes through the assigned internal proxy and the persisted request
   attempt records `access_method = PLAYWRIGHT_PROXY` with the proxy metadata; the proxy password is
   never logged.
4. **Given** a batch of N browser-mode targets, **When** they are persisted, **Then** observations /
   current-price updates / request attempts are written in batched commits (flush every N items or T
   seconds), never one commit per item, and every DB call runs off the Twisted reactor thread.
5. **Given** an in-flight match lock is already held for a target (a concurrent run), **When** the
   browser spider processes it, **Then** it respects the same lock/rate-limit behavior as the HTTP
   spider (no duplicate concurrent scrape of the same match).

---

### Edge Cases

- **Selector never appears**: `wait_for_selector` not present within `browser_timeout_ms` → the
  attempt is a bounded timeout failure with an error code, not an indefinite hang; a failed request
  attempt is recorded and no observation is written (US1 AS-2).
- **No `wait_for_selector` configured**: the browser proceeds after the page's normal load/network
  settle without waiting on a specific element; extraction runs against the rendered DOM.
- **`browser_timeout_ms` unset**: a safe default browser timeout bounds every navigation/wait so a
  browser job can never hang a (scarce) browser slot indefinitely.
- **Variant target missing/uninteractable**: variant selection fails cleanly with an error code; no
  partially-interacted state is persisted as a valid price (US3 AS-3).
- **Browser crash / page error mid-scrape**: treated as a failed attempt with an error code and the
  browser session/context is released so the slot is reclaimed (no leaked browser processes).
- **Redirect to internal host**: re-validated on every hop; a public URL that 302s to an internal
  address is refused before the body is read (US4 AS-2).
- **Proxy assigned but browser cannot use it**: recorded as a proxy failure attempt, not a silent
  direct-browser fetch that misrepresents the transport.
- **HTTP target accidentally on a browser node / browser target on an HTTP node**: routing guarantees
  mode-appropriate node+spider selection so this does not occur; an HTTP-only node has no browser and
  a browser target never lands there (US2 AS-1/AS-3).
- **Mixed-mode job**: a single job containing both HTTP and browser targets dispatches each mode to
  its own service; no batch ever mixes modes.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a browser scrape spider, `generic_browser_price_spider`, that
  loads a competitor product page in a real browser (scrapy-playwright), executes the page's
  JavaScript, and extracts price/currency/stock/title using the profile's configured extraction
  strategy against the **rendered** DOM.
- **FR-002**: The browser spider MUST accept the same dispatch arguments as the HTTP spider
  (`workspace_id`, `scrape_job_id`, `match_ids`, `mode`) and load the matched
  `competitor_product_matches` scoped to `workspace_id` (a match outside the workspace is simply
  absent — no cross-workspace read).
- **FR-003**: Before extraction, when the resolved profile sets `wait_for_selector`, the browser MUST
  wait until that selector is present (or a change/network-settle condition is met) before reading the
  DOM, bounded by the profile's `browser_timeout_ms` (or a safe default when unset); if the wait times
  out, the attempt MUST fail with a bounded-timeout error code rather than hanging.
- **FR-004**: When the resolved profile carries a `variant_selector_config`, the browser spider MUST
  perform the configured variant-selection interaction and wait for the page to settle before
  extraction, so the persisted price reflects the selected variant; when no `variant_selector_config`
  is present, the spider MUST NOT attempt any variant interaction.
- **FR-005**: A variant-selection interaction that cannot complete (element missing/uninteractable)
  MUST fail the attempt cleanly with an error code; no partially-interacted page state may be persisted
  as a valid observation.
- **FR-006**: The browser spider MUST reuse the existing persistence path (observations, current-price
  updates, request attempts) and item/extraction/validation machinery from the HTTP spider rather than
  re-implementing them; it MUST stop at persistence and emit exactly one `price_analysis` Celery task
  per affected variant (deduplicated per variant per job). It MUST NOT compute alerts, variant price
  state, alert events, or webhooks itself.
- **FR-007**: The browser spider MUST obey reactor safety: no synchronous DB commit or blocking Redis
  round-trip on the Twisted reactor thread; every DB/Redis blocking call runs off-reactor (thread
  offload / async driver), with a small per-process DB pool through PgBouncer — identical to the HTTP
  spider.
- **FR-008**: The browser spider MUST enforce the same SSRF safety as the HTTP spider: scheme
  allowlist plus private/loopback/link-local/internal deny rules evaluated against the **resolved IP at
  connection time**, re-validated on **every redirect hop**, refusing the navigation before the page
  body is processed and recording a blocked attempt.
- **FR-009**: The browser spider MUST resolve and honor per-domain robots policy via the same custom
  middleware mechanism as the HTTP spider (not Scrapy's process-global `ROBOTSTXT_OBEY`), resolving the
  policy per request from the cached domain configuration.
- **FR-010**: The browser spider MUST write observations / current-price updates / request attempts in
  batches (flush every N items or T seconds), never one commit per item.
- **FR-011**: When the resolved access policy assigns `PLAYWRIGHT_PROXY`, the browser session MUST
  route through the assigned internal proxy pool, and the persisted request attempt MUST record
  `access_method = PLAYWRIGHT_PROXY` with the proxy metadata (provider/country) reusing the SPEC-10
  proxy-assignment and attempt-logging path; the proxy password MUST never be logged. When the resolved
  policy does not assign a proxy, the browser fetches directly.
- **FR-012**: The browser spider MUST respect the same in-flight match locks and distributed
  domain rate limiting as the HTTP spider (non-blocking on the reactor), so a match already being
  scraped is not concurrently re-scraped and per-domain limits are honored.
- **FR-013**: The browser scraping service MUST be deployed as a **separate** Scrapyd service from the
  HTTP scraping service: its own image with a browser runtime installed at build time, the browser
  Scrapy project baked in at build time (no runtime code-upload dependency), and Scrapyd basic auth
  enabled; the dispatch worker MUST authenticate every `schedule.json` call to it.
- **FR-014**: The browser service MUST bound its browser concurrency to a deliberately low level
  (at most a small number of concurrent browser jobs/sessions/contexts) so browser resource use stays
  controlled, independent of the HTTP service's concurrency.
- **FR-015**: Dispatch MUST route **browser-mode** batches to the browser node pool running the
  browser project + `generic_browser_price_spider`, and **HTTP-mode** batches to the HTTP node pool
  running the HTTP spider; a batch MUST carry exactly one mode, and no browser target may be scheduled
  onto an HTTP node nor an HTTP target onto a browser node.
- **FR-016**: Browser-mode dispatch MUST reuse the existing deterministic node selection, idempotent
  dispatch guard (`dispatched:{scrape_job_id}:{batch_index}`), and re-dispatch/finalization logic
  unchanged, so a retried or re-dispatched browser batch never double-runs and always targets the same
  browser node and spider.
- **FR-017**: The browser scraping path MUST NOT introduce any new persistent schema: it consumes the
  existing `scrape_profiles.mode` / `wait_for_selector` / `browser_timeout_ms` /
  `variant_selector_config` fields and the existing observation/current-price/request-attempt tables;
  no migration is part of this feature.
- **FR-018**: Every browser navigation MUST be bounded by a timeout (page load and each wait), so a
  single stuck page can never occupy a scarce browser slot indefinitely; a timed-out navigation is a
  recorded failed attempt, and its browser session/context is released.
- **FR-019**: All browser scraping MUST remain confined to public product pages under the same
  legal/compliance guardrails as the HTTP spider; the browser path adds JS rendering only, not any
  anti-bot evasion, stealth, or authenticated-session capability.

### Key Entities *(include if feature involves data)*

- **Scrape Profile (browser fields)** *(existing, SPEC-06)*: The workspace/global configuration that
  drives browser scraping. Relevant fields — `mode` (HTTP vs BROWSER), `wait_for_selector`,
  `browser_timeout_ms`, `variant_selector_config`, and the same extraction selectors used by the HTTP
  spider. No new fields are added.
- **Scrape Job / Scrape Job Target** *(existing)*: The unit of dispatched work; a target's resolved
  mode decides whether it batches to the HTTP or browser service. The browser spider updates target
  state exactly as the HTTP spider does.
- **Price Observation / Match Current Price / Request Attempt** *(existing)*: The persisted outputs of
  a browser scrape — identical shape to the HTTP spider's outputs; the request attempt records
  `PLAYWRIGHT_PROXY` (or direct-browser) as its access method.
- **Access Policy / Proxy Provider** *(existing, SPEC-10)*: Governs whether a browser session is
  proxied (`PLAYWRIGHT_PROXY`) and through which internal proxy.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A JavaScript-rendered product page whose price is absent from static HTML produces a
  correct persisted price observation and current-price update when scraped in browser mode, where the
  HTTP spider produced none.
- **SC-002**: 100% of browser-mode targets in a dispatched job are scheduled to the browser service
  (browser project + browser spider) and 0% to the HTTP service; conversely 0% of HTTP-mode targets are
  scheduled to the browser service.
- **SC-003**: A browser scrape that requires selecting a variant persists the selected variant's price,
  distinct from the page's default price, when a variant-selection configuration is present.
- **SC-004**: The browser service never exceeds its configured low concurrency of simultaneous browser
  sessions under load, and no page can occupy a browser slot beyond the configured timeout.
- **SC-005**: A browser navigation to (or redirecting to) a private/internal address is refused before
  the page body is read, recorded as blocked — 0% internal-address fetches complete.
- **SC-006**: When a proxy is assigned, the browser routes through it and the request attempt records
  `PLAYWRIGHT_PROXY`; the proxy password appears in no log.
- **SC-007**: Persisting N browser results performs far fewer than N database commits and executes no
  DB call on the reactor thread (batched, off-reactor persistence parity with the HTTP spider).
- **SC-008**: A retried/re-dispatched browser batch runs exactly once on exactly one browser node
  (no duplicate browser runs).

## Assumptions

- **Reuse over rebuild**: The persistence pipeline, extraction/validation, SSRF resolver + middleware,
  robots middleware, request-attempt logging, proxy assignment (SPEC-10), in-flight match locks +
  rate limiting (SPEC-11), job/dispatch orchestration + idempotent dispatch + node selection (SPEC-08),
  and the `price_analysis` handoff (SPEC-09) already exist and are reused as-is. The new surface is the
  browser spider itself, its use of scrapy-playwright (wait/variant interaction, proxied browser
  context), and the browser-mode dispatch routing to the browser project/spider.
- **No in-process HTTP→browser escalation**: Because the HTTP and browser services are separate
  processes (the HTTP node has no browser), the access-method ladder's Playwright step is realized as
  **browser-mode routing at dispatch time**, not as an in-run switch from HTTP to browser inside one
  spider process. A domain the strategy optimizer (SPEC-12) learns needs a browser is scraped in browser
  mode on subsequent jobs; a single running spider does not spin up a browser mid-run. Automatic
  cross-job escalation beyond the existing mode/strategy signals is out of scope.
- **Browser routing key is the resolved scrape mode**: A target routes to the browser service when its
  resolved scrape profile mode is BROWSER. `variant_selector_config` and JS-only pages are expressed by
  setting the profile to BROWSER mode; the resolver already yields `mode` per target.
- **`variant_selector_config` shape**: Its concrete JSON structure (which element to select and to
  what value, keyed off the match's variant) is defined in the plan; this spec requires only the
  behavior — select the configured variant, wait, then extract — consuming the existing JSONB field.
- **Concurrency bounds live in service config**: The low browser concurrency is expressed by the
  browser Scrapyd node's `max_proc` and the browser project's `CONCURRENT_REQUESTS` /
  `PLAYWRIGHT_MAX_CONTEXTS` settings (already scaffolded); this feature owns their correctness.
- **Existing browser data-model fields**: `scrape_profiles.mode`, `wait_for_selector`,
  `browser_timeout_ms`, and `variant_selector_config` already exist (SPEC-06); no migration is needed.
- **Deployment scaffold exists**: The `scrapers-browser` image (Dockerfile installs Chromium at build),
  `scrapyd.conf` (basic auth, `max_proc=1`), entrypoint, and `SCRAPYD_BROWSER_URLS` config already exist
  (SPEC-01); this feature adds the spider and completes the dispatch routing to it.
- **Backend only**: No frontend; the browser capability is exercised through jobs/dispatch, and its
  behavior is verified against JS-rendered fixture pages.
