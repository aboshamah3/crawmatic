# PLAN — Continuous Amazon.sa + Noon Pricing at Low Cost

**Written 2026-08-01.** Goal: get prices from our two biggest competitors, amazon.sa and
noon.com, **continuously and unattended**, without a big monthly bill. This plan first
states exactly what is wrong (all of it measured, not guessed), then the fix steps in
order.

Evidence base: `matching/COMPETITOR_SCRAPE_PROFILES.md` (§2b),
`COST_PER_100_PRODUCTS_2026-07-30.md`, `PRICE_TEST_100_COMPLETE_2026-07-30.md`,
plus live Jina/noon probes run today (2026-08-01, results below).

---

## 1. What is ACTUALLY wrong — the precise diagnosis

The two sites have **completely different problems**. Amazon is a solved scrape with a
cost + reliability problem. Noon is a genuine access blockade.

### Amazon.sa — WORKS, but expensive and can't run unattended

The full pipeline (Playwright browser + DataImpulse residential proxy + CSS extraction,
robots set to `IGNORE_AFTER_APPROVAL` per Abdul 2026-07-27) delivers real prices —
53 verified captures with correct part numbers on 2026-07-30. Three remaining defects:

| # | Problem | Measured impact |
|---|---|---|
| A1 | **DEFERRED deadlock** — deferred targets are never re-dispatched, so any run bigger than ~20 products wedges permanently and needs hand-driving | The 36% Amazon "coverage" in the 100-product test was purely this; blocks *continuous* operation for ALL sites, not just Amazon |
| A2 | **6.15 MB per page through the paid proxy** — `abort_unsafe_request` (`libs/scrape-core/scrape_core/browser/ssrf.py`) never aborts, so Chromium downloads all images/CSS/fonts. HTML alone is ~1.3 MB | Amazon is **77% of the whole pricing bill**: $0.59 of the $0.77 per 100 products; daily full catalog ≈ $112/mo proxy |
| A3 | **Browser capacity: one process** — `scrapers-browser` runs `max_proc = 1`, `BROWSER_CONCURRENT_REQUESTS = 2`, ~11.4 s/page. Amazon is 606 of 2,380 catalog links | Even with A1 fixed, a full Amazon sweep takes ~2 h through the single browser; fine for daily, but it is the throughput ceiling |
| A4 | **Marketplace-seller price poisoning** — e.g. GI-490 black/yellow scraped at exactly 219 SAR on Amazon while every other site sits at 35–65 (inflated seller or hidden multipack) | Correct scrapes, wrong business data; an automatic repricing rule would be poisoned. Needs an outlier guard, not a scraper fix |
| A5 | Idle-fleet baseline — 48 idle Celery procs | $40/mo Railway baseline of which ~$31 is waste (not Amazon-specific, but part of "not for a big cost") |

### Noon.com — ✅ SOLVED 2026-08-02 (this section describes the *former* state)

> **Superseded.** Everything below was true on 2026-08-01 and is kept as the record of
> what was tried. On 2026-08-02 a wider matrix (8 Arab-country proxy exits × 3
> storefronts × 2 transports) found noon **open** on plain HTTP through the same
> DataImpulse account, and a 100-product run returned 100/100 pages. See Phase 3.
> The row below claiming DataImpulse ranges are "burned" no longer holds.

Everything tried and measured (2026-07-27 + 2026-07-30 tests):

| Route | Result |
|---|---|
| Direct HTTP (Railway or this server) | TLS dropped / timeout — noon blocks datacenter IPs |
| DataImpulse residential proxy — SA, AE, EG, US, no-country | CONNECT ok, then **tarpit** (silence until timeout). Noon blocks DataImpulse's ranges specifically; the proxy itself is proven healthy |
| Real Chromium ± proxy ± HTTP/2 | `ERR_HTTP2_PROTOCOL_ERROR` or timeout |
| **Jina reader** (`r.jina.ai/<url>`) | **Page loads, but the price does NOT render reliably.** Re-tested live 2026-08-01 twice (cached + `x-no-cache`): product title/specs present, price absent, "Unable to load cart" — noon's offer API refuses Jina's non-KSA render nodes. The single 62.00 SAR success on 07-27 was luck, not a method |
| Noon internal catalog API (`_svc/catalog/...`) via Jina | Returns the SPA storefront shell, not JSON — dead end (2026-08-01) |

**Conclusion:** noon is not fixable by any config change on our side. It needs either a
**proxy provider noon doesn't block** (DataImpulse's ranges are burned for noon — but
noon can't block all residential/mobile ISP ranges, that would block real customers) or
a **rendering service with true KSA egress**. Also note: noon prices only render with a
KSA-located client, so whatever route we pick must egress from Saudi Arabia.

---

## 2. The fix plan, in order

### Phase 1 — Make ANY continuous run possible (prerequisite for everything)

> Fix A1: the DEFERRED deadlock. Without this, "continuously" is impossible for every
> site, and every cost number below stays theoretical.

- `dispatch_job` already selects `PENDING` **and** `DEFERRED`; nothing ever re-enqueues.
  Add a periodic re-enqueue tick in `scheduler_app.py`.
- **Give the Redis guard `dispatched:{job_id}:{batch_index}` a TTL** — otherwise the
  re-enqueue works exactly once and then silently stops (known trap, documented 07-30).
- Acceptance test: the 100-product run completes **unattended**, no hand-driving,
  Amazon coverage rises from 36% to ≈ the 96-link ceiling.

Effort: small, one service. Risk: low. **This is the single highest-value change.**

### Phase 2 — Make Amazon cheap (A2, one deploy, halves the bill)

- In `abort_unsafe_request`, abort request types `image`, `media`, `font`,
  `stylesheet` for browser fetches (a few lines, already scoped 07-30).
- Expected: 6.15 MB → ~1.4 MB per Amazon page; $0.59 → ~$0.14 per 100 products;
  daily-catalog proxy cost $112/mo → ~$27/mo.
- Verify the same way the problem was found: Railway `NETWORK_RX_GB` for
  `scrapers-browser` ÷ `PLAYWRIGHT_PROXY` attempt count. Target ≤ 1.5 MB/page, and
  confirm prices still extract (CSS selectors don't need CSS files to work, but verify).
- Keep Amazon's existing 10 rpm / conc-2 rule — it's working; don't touch it.
- Capacity (A3): after asset blocking is verified, raise the browser lane modestly
  (e.g. `BROWSER_CONCURRENT_REQUESTS = 3–4` within the same single process) only if the
  ~2 h Amazon sweep is too slow for the chosen cadence. Do it after, not with, the asset
  change — one variable at a time.
- Data quality (A4): add a per-product outlier rule before any repricing consumes Amazon
  numbers — e.g. flag an Amazon price > 2× the median of the other sites for that
  product as "suspect (marketplace seller/multipack)" instead of feeding it to pricing.

### Phase 3 — Noon: ✅ SOLVED 2026-08-02, ready to seed

**The decision tree below (stages N1–N4) is obsolete — do not spend the trial budget.**
Noon is no longer blocked for DataImpulse. Two tests on 2026-08-02 settled it:

- `matching/NOON_PROXY_MATRIX_2026-08-02.md` — 54 fetches across 8 Arab-country proxy
  exits × 3 storefronts × 2 transports. The July tarpit did not reproduce once.
- `matching/NOON_100_PRICE_TEST_2026-08-02.md` — **100 real catalog products,
  100/100 fetched, 0 failures, 89 real prices + 11 verified out-of-stock.**

#### The noon profile to seed

| Field | Value |
|---|---|
| Access | `PROXY_HTTP` — existing DataImpulse, username `login__cr.sa`, rotating sessions; **retry ×2 on 403** |
| Transport | Plain HTTP/1.1, Chrome UA + standard navigation headers, compression on. **No browser, no Jina, no new provider.** (curl_cffi Chrome-TLS impersonation also verified — documented fallback if noon tightens fingerprinting) |
| Extract | `JSON_LD` (`offers.price`) — worked on **100/100** pages; fallback `REGEX(__NEXT_DATA__)` never needed |
| Rate | 10 rpm / conc-1 (same style as the S-Tech and Amazon rules) |
| Failure mode | Instant ~500-byte HTTP 403 when a rotating exit lands on a blocked IP. **7 of 100 products needed a retry; all 7 then succeeded.** Nothing timed out |

#### Measured cost — noon is now the *cheapest* site in the fleet

| | Measured |
|---|---:|
| Wire bytes per product | **78.1 KB** (7.63 MB / 100 products) — HTML is ~0.5 MB but gzip is what the proxy bills |
| Latency | **1.29 s/page** median |
| **Proxy cost per product** | **$0.000078** |
| Proxy cost per 100 products | **$0.0078** |
| Compute per product (July figure, not re-measured) | ~$0.0002 |
| **Total per product** | **≈ $0.00028** |

For scale: the Amazon browser path costs **6.15 MB/page — ~79× more per page than
noon**. Full noon coverage of the catalog once ≈ **$0.60**; daily ≈ **$18/mo**, of which
only ~$2 is proxy — compute dominates, not bandwidth.

#### Two rules that must survive into production

1. **Out-of-stock ≠ price 0.** Noon serves `"availability": "OutOfStock"` with no offer
   price; 11 of the 100 came back that way and are correct data. Store them as
   out-of-stock, never as 0, or any repricing rule is poisoned.
2. **Re-verify before trusting it.** Noon's edge flipped once already (blocked in July,
   open in August). Run a 10-URL spot check right before seeding, and rely on the
   Phase-4 prices-per-site health signal to catch a re-block the day it happens.

Bonus finding: `uae-en` and `egypt-en` storefronts work identically via `__cr.ae` /
`__cr.eg` with the same product IDs (region-portable URLs), returning AED/EGP — if
multi-region pricing is ever wanted, the method is already proven.

<details>
<summary>Superseded — the original N1–N4 decision tree (kept for the record)</summary>

1. **Stage N1** — different residential/mobile proxy provider with a real KSA pool;
   trial cost ~$5–15. *(Its outcome was achieved with the provider we already pay.)*
2. **Stage N2** — same new provider + the browser path if plain HTTP gets a JS
   challenge. *(Not needed; plain HTTP returns the full page.)*
3. **Stage N3** — Jina with KSA-targeting attempts. *(Not needed.)*
4. **Stage N4** — accept the gap visibly. *(Not needed.)*

</details>

### Phase 4 — Turn it into a continuous, monitored schedule

- Baseline cleanup (A3): cap worker `--concurrency=4` → Railway baseline $40 → ~$9/mo.
- Cadence: start **weekly** full-catalog (2,380 links) to validate stability, then move
  to **daily** once two consecutive unattended runs complete clean.
- Wall-clock realism: rate rules make refreshes slow by design — S-Tech's 609 links
  alone need ~1 h at 10 rpm; Amazon's 96+ links similar order. A daily run fits easily;
  just don't expect sub-hour refreshes.
- Add two cheap health signals per run: (1) prices-extracted per site vs link count
  (catches a selector break the day it happens), (2) `scrapers-browser` MB per fetch
  (catches asset-blocking regressions before the invoice does).

---

## 3. What it costs when done

| | Today (broken) | After Phases 1–4 |
|---|---:|---:|
| Per 100 products | $0.77 (and runs wedge) | **≈ $0.32** |
| Daily full catalog, all-in monthly | ≈ $167 theoretical | **≈ $50/mo** ($9 baseline + $15 compute + ~$27 Amazon proxy + noon **$0.24 measured**) |
| Weekly cadence instead | — | **≈ $13–15/mo** |

Amazon after Phase 2 is ~$0.14 per 100 products. **Noon is measured, not estimated:
$0.0078 per 100 products of proxy — effectively free, and cheaper than every other
site.** It is no longer a cost variable in this plan.

## 4. Decisions needed from Abdul

1. Go-ahead for the Phase 1 + Phase 2 deploys (both scoped, low risk).
2. ~~$15 trial budget for an alternative KSA proxy provider~~ — **no longer needed**,
   noon works on the DataImpulse account we already pay for (Phase 3, solved 2026-08-02).
3. Cadence target: daily or weekly once stable ($50/mo vs ~$14/mo).
4. Go-ahead for the one production write that seeds the noon profile into
   `domain_access_rules` (Phase 3) — the method is proven, the row is not yet created.

## 5. Execution status (updated 2026-08-02 evening — all phases EXECUTED)

| Phase | State |
|---|---|
| **Phase 1 — DEFERRED deadlock** | ✅ Deployed (`6a863f8`): `redispatch_pending_jobs` sweep on the 60 s maintenance tick + TTL on the dispatch guard (`SCRAPYD_DISPATCH_GUARD_TTL_SECONDS=900`, sentinel 120 s). Verified: Cohort B dispatched + finalized unattended |
| **Phase 2 — Amazon asset blocking** | ✅ Deployed (`a76f709`): **0.91 MB/page measured** (was 6.15; target ≤1.5), prices still extract |
| **Phase 4a — worker cap** | ✅ Deployed (`edd9cb5`): `CELERY_WORKER_CONCURRENCY=4`; worker 4.29 GB → 0.42 GB (~$27/mo saved) |
| **Phase 3 — noon seed** | ✅ Seeded (rule conc-1, rotating policy retry ×2, profile ACTIVE PROXY_HTTP+JSON_LD) — but **noon re-blocked DataImpulse mid-day 2026-08-02** (~14:36Z, tarpit on every transport incl. the morning's working urllib route). Method perishable; profile costs nothing while blocked |
| **Bonus fixes found by the Cohort B run** | `8e0b807` match_ids list collapsed to ONE target per batch (the real July throughput ceiling); `98b2b5d` rate-ceiling denials terminal-failed instead of deferring; `ba393e9`/`012c986` Scrapyd eggs baked into images (no more post-deploy re-registration) |

**Cohort B result** (`matching/PRICE_TEST_COHORT_B_2026-08-02.md`): 100/100 products
priced, 239/449 links (53%), **unattended end-to-end**; 9 of 13 sites at 92–100%.
Measured all-in marginal ≈ **$0.17 per 100 products**.

## 5b. State after the 2026-08-03 amazon/S-Tech validation

Abdul held the push because those two sites were unvalidated. Full detail in
`matching/PRICE_TEST_COHORT_B_2026-08-02.md` §E. Headlines:

| | Result |
|---|---|
| **S-Tech** | ✅ **96/96 = 100%** (was 18%). Needed all three: its own proxy-first policy (the old one had no provider), `domain_strategy_profiles` set DISABLED, and the retry-path defer fix |
| **Amazon** | ✅ extraction solved — **56/57 = 98% on fresh exits**, zero PRICE_NOT_FOUND. ⚠️ access unfinished: it burned its proxy exits mid-run and then failed 37/37 with CAPTCHAs |
| **Noon** | unchanged — still re-blocked since 2026-08-02 |

Five more code defects were found and fixed (retry-path ceiling failure; bot
interstitial served as HTTP 200; unbounded defer cycling; every non-2xx logged
UNKNOWN_ERROR; terminal targets re-openable). 27 commits pushed; 1,861 tests green.

**Two plan assumptions are now void:**

- **§A3's single-browser ceiling.** Amazon never needed the browser — the price is in
  server-rendered HTML, plain HTTP scores 28/30 at **240 KB/page vs ~910 KB**. Its
  profile is now HTTP mode, and **no `BROWSER` profiles remain**, so `scrapers-browser`
  is idle and reclaimable.
- **§3's cost table.** It assumed Amazon dominates via browser bytes. Re-measure before
  quoting a cadence price; the real number is below the $33/mo in §5.

**The new gate on Phase 4 (cadence) is proxy exit burn, not cost.** `assign_proxy` only
varies the session key by `attempt_number`, and the spider seeded it per *domain*, so a
whole crawl funnelled through ~3 exit IPs; Amazon CAPTCHAs after ~150 such fetches and
recovers only with time. Fixed to seed per match (rotation is now genuinely per
request), but **unproven at sweep scale** — a full 606-link Amazon sweep must run clean
before daily cadence is credible.

### Next actions

1. **Cold re-run Amazon's 37 remaining links** after the exits rest — the last
   validation step.
2. **Clean up 153 stale jobs.** They make the Phase-1 sweep enqueue ~220k pointless
   tasks/day (SQL in the `scrape-deferred-deadlock` memory note).
3. **Investigate the strategy optimizer**: it kept S-Tech on a preference carrying 996
   recorded failures; rediscovery should have marked it DEGRADED after ~3. Any site
   whose access method breaks will keep being retried the broken way.
4. Then Phase 4b: two consecutive clean unattended runs before scheduling.

---

# 6. EXECUTION RUNBOOK — for the next session

**Written 2026-08-02 as a handoff.** Run this in a fresh session. Fable orchestrates and
supervises; Opus/Sonnet subagents do the heavy lifting (code edits, DB queries, log
reading, metric pulls). Fable makes the deploy/seed decisions and owns the final report.

Three deliverables, in order: **(1) execute the plan as written, (2) run a second
100-product all-site test, (3) write a short comparison report on completion rate and
real cost (Railway + proxy).**

## 6.1 Step 1 — Execute Phases 1, 2, 4a, and the noon seed

Do them in this order; each is independently verifiable and independently revertible.

### Phase 1 — the DEFERRED deadlock (**gate: nothing else matters until this passes**)

| | |
|---|---|
| Files | `apps/scheduler/app/scheduler/scheduler_app.py`, `libs/shared/app_shared/scrapyd/client.py` |
| Change A | Add a periodic re-enqueue tick to the `main()` loop (same shape as the existing `_enqueue_finalize_jobs` / `_enqueue_recover_stalled` ticks): for every job still holding `PENDING`/`DEFERRED` targets, re-enqueue `SCRAPE_DISPATCH_JOB` on the `scrape_dispatch` queue. `dispatch_job` **already selects both PENDING and DEFERRED** (`apps/workers/app/workers/tasks_jobs.py:240`) — nothing calls it a second time. That is the whole bug. |
| Change B | **Give the Redis guard a TTL.** `client.py:125` does `SET key <sentinel> NX` and `:154` overwrites with the real jobid — neither sets an expiry, so a re-dispatch of the same `batch_index` silently returns the old jobid and never re-POSTs. Without this, the re-enqueue works exactly once and then quietly stops. This trap already cost the July run its last ~90 links. |
| Acceptance | A 100-product run completes **unattended, no hand-driving**. Concretely: 0 jobs stuck `RUNNING` with `DEFERRED` targets after the run, and 0 jobs left `PENDING` with `started_at IS NULL`. |
| Revert | Redeploy previous scheduler image. |

### Phase 2 — Amazon asset blocking

| | |
|---|---|
| File | `libs/scrape-core/scrape_core/browser/ssrf.py` — `abort_unsafe_request` (line ~116) |
| Change | It currently returns `False` for every non-navigation request, so Chromium downloads all images/CSS/fonts. Abort resource types `image`, `media`, `font`, `stylesheet` for browser fetches. Keep the existing SSRF logic on navigation requests exactly as-is. |
| Acceptance | Railway `NETWORK_RX_GB` for `scrapers-browser` ÷ `PLAYWRIGHT_PROXY` attempt count ≤ **1.5 MB/page** (was 6.15). **And prices still extract** — CSS selectors don't need CSS files, but verify on real Amazon captures before trusting it. |
| Revert | One-line revert; low risk. |

### Phase 4a — worker concurrency cap

| | |
|---|---|
| File | `apps/workers/Dockerfile:30` — bare `CMD ["celery", "-A", "app.workers.celery_app", "worker", "--loglevel=info"]` |
| Change | Add `--concurrency=4`. Optionally add a `CELERY_WORKER_CONCURRENCY` knob to `libs/shared/app_shared/config.py` so it is tunable without a rebuild. |
| Acceptance | `ps aux \| grep -c celery` inside the worker → **5** (was 51); container `/sys/fs/cgroup/memory.current` → ~0.5 GB (was 3.6); worker `MEMORY_USAGE_GB` in the metrics API → ~0.5 after 24 h (was 3.85). |
| Watch for | Under-provisioning shows up as growing `scrape_dispatch` queue depth in Redis, **not** as errors. If depth grows during the run, raise to 6–8. |

### Phase 3 — seed noon (production write)

Spot-check 10 noon URLs first (noon's edge flipped once already), then seed
`domain_access_rules` for noon.com: `PROXY_HTTP`, DataImpulse `__cr.sa`, **retry ×2 on
403**, extraction `JSON_LD → REGEX(__NEXT_DATA__)`, rate 10 rpm / conc-1.

## 6.2 Step 2 — The second 100-product test (all 13 sites)

**Cohort B is already selected** and stored at `matching/COHORT_B_100_PRODUCTS.txt`
(product_id|link_count, pulled from production 2026-08-02):

- **100 products, 449 ACTIVE links** — 49 products with 5 links, 51 with 4.
- These are the *next tier down* from the July cohort: the July 100 were every product
  with **≥6** links; Cohort B is the best of what remains.

> ⚠️ **The two cohorts are not the same shape — compare rates, never absolutes.**
> July: 100 products / **714 links** (6–10 each). Cohort B: 100 products / **449 links**
> (4–5 each). A lower total price count is expected and is *not* a regression. The
> meaningful comparisons are **% of links priced per site** and **% of products with
> ≥1 price**.

Run it through the production pipeline (not a side script) — the point is to prove the
pipeline runs unattended, which is exactly what the July run could not do.

## 6.3 The baseline to compare against — 2026-07-30, 100 products / 714 links

Full detail in `PRICE_TEST_100_COMPLETE_2026-07-30.md`. Headline numbers:

| | July baseline |
|---|---:|
| Products with ≥1 competitor price | 100 / 100 (100%) |
| Links priced | **484 / 714 (68%)** |
| Distinct prices captured | 510 |
| Sites returning prices | 11 of 13 |
| Wall-clock | ~85 min |
| Marginal cost of the run | ≈ **$0.12** |
| Throughput | ~9.4 links/min |
| **Run completed unattended?** | ❌ **No — hand-driven through the deadlock** |

Per-site price rate (the row that matters most for comparison):

| Site | Links | Priced | Rate |
|---|---:|---:|---:|
| eXtra | 33 | 33 | 100% |
| آفاق الحاسوب | 4 | 4 | 100% |
| Amwaj EST | 52 | 51 | 98% |
| Jarir | 82 | 77 | 94% |
| رواد الأحبار | 58 | 54 | 93% |
| Rawand | 44 | 40 | 91% |
| FQ Toners | 71 | 61 | 86% |
| الشامل | 22 | 18 | 82% |
| PC Palace | 82 | 65 | 79% |
| S-Tech Ink | 98 | 46 | **47%** |
| Amazon | 96 | 35 | **36%** |
| **Noon** | 72 | **0** | **0%** |
| احبار HD | 0 | — | — |

The three laggards and what changed since:

- **Noon 0%** — ✅ **solved 2026-08-02**: 100/100 pages, 89 prices + 11 verified
  out-of-stock, 78.1 KB/page, 1.29 s/page (`matching/NOON_100_PRICE_TEST_2026-08-02.md`).
  **Expect noon near 100% in cohort B.** If it is not, the seed is wrong, not the route.
- **Amazon 36%** — capacity, not breakage. It produced 53 real prices with correct part
  numbers; it simply never worked through 96 links at ~11.4 s/page through one browser
  process. Phase 1 should raise this substantially; if it plateaus well under 90%, the
  single-browser ceiling (`max_proc = 1`, `BROWSER_CONCURRENT_REQUESTS = 2`) is the cause.
- **S-Tech 47%** — apex-redirect bug fixed (281 URLs rewritten to `www.`), but a second
  cause remains: URLs returning 200 from the office return non-200 from Railway, i.e.
  S-Tech rate-limiting the Railway IP. **Still unproven either way** — cohort B is the
  test. Memory note `stech-rate-limit-rule` says 10 rpm / conc-1 fixed 47% → 100% in
  isolation; confirm that holds inside a full run.

Also carry forward: **the GI-490 marketplace-price poisoning** (Amazon at 219 SAR while
every other site sits at 35–65). Flag any Amazon price > 2× the median of other sites as
suspect rather than feeding it to pricing.

## 6.4 Step 3 — The report

Write `matching/PRICE_TEST_COHORT_B_<date>.md`. **Careful but summarized** — the July
report is 400 lines; this one should be ~1 page plus an appendix. Required content:

**A. Completion rate**
- Links priced / links targeted, overall and **per site**, side by side with the July
  rates in §6.3.
- Products with ≥1 price.
- **Did the run complete unattended?** — the single most important line in the report.
  Name any manual intervention that was needed.
- Per-site deltas with a one-line cause for any site that moved more than ~10 points.

**B. Cost — measure, don't estimate**
- **Railway:** metrics GraphQL at `https://backboard.railway.com/graphql/v2`, using
  `user.accessToken` from `~/.railway/config.json` (note: `user.token` is null — use
  `accessToken`) and a **non-default User-Agent** (Cloudflare returns error 1010
  otherwise). Query `usage(projectId:"76296e99-f8d6-4001-b6ac-10b9d2e20ea1",
  measurements:[CPU_USAGE,MEMORY_USAGE_GB,NETWORK_RX_GB,NETWORK_TX_GB],
  startDate, endDate, groupBy:[SERVICE_ID])`. **Units are per-minute integrals** —
  divide by the window's minutes. Service IDs: worker `cbfcc0c3`, scrapers `2658aa25`,
  scrapers-browser `e0115ef8`, api `5cdd7fc5`, postgres `2cbe7c21`, redis `82783a46`,
  scheduler `7b9f7c1b`, pgbouncer `6fb3959d`. Report the **run window minus idle
  baseline** = marginal cost, vs July's $0.12.
- **Proxy:** `scrapers-browser` `NETWORK_RX_GB` ÷ `PLAYWRIGHT_PROXY` attempt count =
  MB/page. Compare against 6.15 MB (before Phase 2) and the ≤1.5 MB target. Price at
  DataImpulse ~$1/GB.
- **Per-product and per-link cost**, then project: daily full catalog, monthly.
- Confirm or correct the plan's §3 table with the measured numbers.

**C. Verdict**
- Is the system ready for a scheduled cadence (Phase 4b)? The bar is **two consecutive
  unattended runs completing clean** — cohort B is the first of those two.

## 6.5 Orchestration notes

- **Fable orchestrates**; Opus/Sonnet subagents do the heavy lifting. Good subagent
  splits: one per code change (Phase 1 / Phase 2 / Phase 4a) since they touch disjoint
  files; one for DB/log forensics during the run; one for the metrics pull.
- **Do not parallelize the deploys** — one variable at a time, each verified against its
  own acceptance criterion before the next. That rule is why the July run's causes were
  identifiable at all.
- **Prod-write approval:** seeding noon, the worker redeploy, and any unwedging writes
  all touch production. Agree the approval mode with Abdul at the start of the session
  rather than stopping every few minutes mid-run.
- Reference material: `matching/NOON_PROXY_MATRIX_2026-08-02.md` (route matrix),
  `matching/NOON_100_PRICE_TEST_2026-08-02.md` (noon 100 + cost method),
  `PRICE_TEST_100_COMPLETE_2026-07-30.md` (July baseline + full price appendix),
  `matching/COMPETITOR_SCRAPE_PROFILES.md` (per-site methods).
