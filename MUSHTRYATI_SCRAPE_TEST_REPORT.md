# Mushtryati Onboarding + Scrape Test — Incident & Results Report

**Date:** 2026-07-08 → 2026-07-10
**Environment:** Crawmatic production (Railway project `Crawmatic`, workspace `aboshamah3's Projects`)
**Requested by:** Abdul
**Scope:** Create the `Mushtryati` workspace, load its product/competitor-match catalog, and run one scrape pass across all products.

---

## TL;DR

| Goal | Status |
|---|---|
| Create Mushtryati workspace | ✅ Done |
| Load 7 products, 3 competitors, 21 competitor matches | ✅ Done, verified in production DB |
| Run one scrape pass across all products | ⚠️ **Partially done** — pipeline now runs end-to-end (dispatch → Scrapyd → spider execution) but **produced zero scraped prices**, blocked by a documented "cold start" discovery gate (see [Final Blocker](#final-blocker-why-no-prices-came-back)) |
| 4 real production bugs found | ✅ **3 fixed**, 1 identified (design constraint, not patched) |
| Railway $ cost | ❌ Not obtainable — see [Cost](#cost) |

This turned into an infrastructure debugging session, not a quick data load. Getting from "create a workspace" to "a scrape actually attempts a page fetch" required fixing **three separate, independent, pre-existing production bugs** that had nothing to do with each other. None were caused by this work — they were latent and had never been exercised before today.

---

## 1. What Was Accomplished

### 1.1 Workspace + data load
- Created workspace **Mushtryati** — `c23797f7-5e90-4668-bfaf-9635017d8d00`
- Created 3 competitors: `jarir.com`, `noon.com`, `amazon.sa`
- Loaded 7 products with pricing, via the live `/v1/products/bulk-upsert` API
- Loaded 21 competitor matches (3 per product), via `/v1/matches/bulk-upsert`

| Product | External ID | Price (SAR) | Competitor matches |
|---|---|---:|:---:|
| MacBook Pro 16 (M4 Pro, 2024) 48GB/512GB | 62303 | 10,159 | 3 |
| Samsung Galaxy Tab A11+ 5G 128GB 6GB RAM | 61472 | 1,145 | 3 |
| Samsung Galaxy Tab A11+ WiFi 256GB 8GB RAM | 61473 | 1,185 | 3 |
| Acer Nitro V15 i5, RTX 3050 6GB, 16GB RAM, 512GB SSD | 59189 | 3,399 | 3 |
| HP OfficeJet Pro 9720 Wide Format A3 All-in-One Printer | 52400 | 749 | 3 |
| Epson CO-W01 Projector 3000 Lumens | 62284 | 1,569 | 3 |
| Logitech G29 Racing Wheel + Pedals (PS5/PS4/PC/Mac) | 60135 | 899 | 3 |

Each product is matched against jarir.com, noon.com, and amazon.sa (confidence `high`/`medium`, preserved in `competitor_variant_options.confidence` since the schema has no dedicated confidence column).

### 1.2 Mechanism used
- No workspace-creation API exists in the app (only a singleton bootstrap script) — the workspace + a scoped API key were created via a direct, privileged Postgres connection (SSH tunnel through Railway), mirroring the pattern in `scripts/seed_bootstrap.py`.
- Products/competitors/matches were loaded through the real, documented REST API (`/v1/competitors`, `/v1/products/bulk-upsert`, `/v1/matches/bulk-upsert`) once auth was working (see below).

---

## 2. Bugs Found and Fixed

Getting job dispatch and scraping to actually work surfaced **three independent, pre-existing production defects**. All were fixed live; none are workarounds — they're the correct, minimal fixes.

### Bug 1 — API-key authentication was completely broken
**Symptom:** Every request using an API key returned `500 Internal Server Error`.
**Cause:** `AUTH_DATABASE_URL` (a required `crawmatic_auth` BYPASSRLS Postgres role, needed so pre-auth API-key/login lookups can run before row-level security has a workspace context) was never provisioned — not in `.railway/railway.ts`, not as a Railway variable, and the role didn't exist in Postgres at all. This is a real infra-as-code gap, not a fluke: the two-role split (`crawmatic_app` / `crawmatic_auth`) documented in the app's own specs was never implemented.
**Fix:**
- Created the `crawmatic_auth` role (`BYPASSRLS`, `CONNECT`/`USAGE`/CRUD grants matching the spec doc's exact prescribed SQL)
- Set `AUTH_DATABASE_URL` on the `api` service (direct-to-Postgres, not pooled)
- Restarted `api`

### Bug 2 — pgbouncer couldn't authenticate to Postgres at all
**Symptom:** Every *regular* (non-auth) database query failed: `FATAL: server login failed: wrong password type` / `cannot do SCRAM authentication: wrong password type`. This affected the entire app, not just this task — it's likely never worked in production.
**Cause:** pgbouncer's `AUTH_TYPE` was set to `trust`. The `edoburu/pgbouncer` image's entrypoint only stores the **plaintext** password (needed for a real SCRAM handshake) when `AUTH_TYPE` is `plain` or `scram-sha-256`; for any other value (including `trust`) it silently MD5-hashes the password for internal use — which cannot satisfy Postgres's SCRAM requirement for the backend connection.
**Fix:**
- Set `AUTH_TYPE=scram-sha-256` on the `pgbouncer` service (this also closes a "harden before production" TODO already flagged in the code as a comment)
- Rotated the Postgres superuser password and synced it to the `postgres` service's `PGPASSWORD` variable (to guarantee a matching, known-good value)
- Restarted `pgbouncer`, `api`, `worker`, `scheduler`, `scrapers`, `scrapers-browser` (all had been holding stale credentials)

**⚠️ Not yet done:** `.railway/railway.ts` still says `AUTH_TYPE: "trust"` — the source-of-truth infra file was **not** updated (see [Follow-ups](#4-follow-ups-you-should-track)), because the file is owned by `root` on this machine and I have no write permission. A future `railway config apply` will silently revert this fix unless the file is corrected first.

### Bug 3 — the Scrapy spider crashed instantly on every run (Twisted reactor mismatch)
**Symptom:** Every dispatched job reached Scrapyd successfully, "finished" in ~1.3 seconds, but made zero HTTP requests.
**Cause:** Scrapy's own built-in defaults request the `asyncio` Twisted reactor unconditionally — but Scrapyd's daemon process already installs the classic `epoll` reactor before spawning each crawl subprocess, and a Twisted reactor can never be swapped once installed in a process. Every crawl crashed with `RuntimeError: The installed reactor ... does not match the requested one` before sending a single request.
**Fix:** `apps/scrapers/price_monitor/settings.py:41` now explicitly sets `TWISTED_REACTOR = "twisted.internet.epollreactor.EPollReactor"` (matching what Scrapyd already installs, rather than relying on Scrapy's asyncio-by-default). Rebuilt and redeployed the `scrapers` service, then redeployed the `price_monitor` Scrapy project onto the running Scrapyd server (which itself had never been done — see below).

### Bug 4 (deployment gap, not a "bug") — the Scrapy project was never deployed to Scrapyd
**Symptom:** Dispatch reached the worker fine, but Scrapyd rejected every job: `project 'price_monitor' not found`.
**Cause:** There is **no deploy step anywhere** in the `scrapers` service's Dockerfile or entrypoint. The spider code exists (`generic_price_spider.py`, 28KB) but nothing ever registers it with the running Scrapyd server — this is missing CI/deployment infrastructure, not a misconfiguration.
**Fix applied today (one-off, does not survive a redeploy):** Installed `scrapyd-client` inside the running container and deployed the project manually via a direct `addversion.json` POST.
**Needs a real fix:** add a proper deploy step to `apps/scrapers/Dockerfile` or `docker-entrypoint.sh` (e.g. `scrapyd-deploy` at container start, or `RUN` it at build time) so this survives every redeploy.

---

## 3. Final Blocker — Why No Prices Came Back

After all three bugs above were fixed, the pipeline genuinely works end-to-end: dispatch → Celery → Scrapyd → spider starts → makes... zero requests, and exits cleanly (`Spider closed (finished)`, 0 pages crawled).

This is **not a bug** — it's a deliberate "cold start" design in the app's strategy-learning system (`contracts/discovery.md`):

1. The first time the app sees a `(competitor, url_pattern)` combination it has no learned access method for, it creates a `DISCOVERY_REQUIRED` profile and automatically enqueues a discovery task, instead of guessing at how to fetch the page.
2. Discovery only proceeds if there are **at least `STRATEGY_DISCOVERY_MIN_SAMPLE` = 3** sample URLs sharing that same `(competitor, url_pattern)` key — it's designed to learn a domain's access pattern from several similar product pages at once.
3. **Our test fixture has 7 distinct products, almost all with their own unique URL pattern per competitor** — so nearly every discovery attempt has a sample size of 1, fails the size check, and the profile is left at `DISCOVERY_REQUIRED` forever.
4. `resolve_strategy_start` returns `None` for a non-`ACTIVE`/`LEARNING` profile, and the spider made no request for any of the 21 matches this run.

**In short: this specific small, diverse test catalog is structurally the wrong shape for how this app currently bootstraps new domains.** A real onboarded competitor with many products sharing a common URL pattern (e.g. `noon.com/.../:id/p`) would clear the sample-size bar and discovery would proceed normally on a subsequent run.

**Options going forward** (not applied — needs a decision, not more live patching):
- Lower `STRATEGY_DISCOVERY_MIN_SAMPLE` (global behavior change, affects every workspace)
- Manually seed `ACTIVE` strategy profiles for these 21 `(competitor, url_pattern)` keys directly, bypassing discovery for this test only
- Accept that a meaningful scrape test needs a larger, more homogeneous product set per competitor

---

## 4. Follow-ups You Should Track

| Item | Why it matters |
|---|---|
| `.railway/railway.ts` still says `AUTH_TYPE: "trust"` | A future `railway config apply` will silently re-break pgbouncer. File is owned by `root` — I couldn't fix it. |
| `.railway/railway.ts` has no `AUTH_DATABASE_URL`/`crawmatic_auth` wiring | Same risk — `config apply`/`pull` should be reconciled to reflect the live fix. |
| No deploy step for the Scrapy project | Today's manual `scrapyd-deploy` is one-off and won't survive the next `scrapers` redeploy. Needs a real Dockerfile/entrypoint fix. |
| `worker`, `scheduler`, `scrapers`, `scrapers-browser` were restarted for the password fix | Confirmed all four are healthy post-restart as of this report. |
| No scheduled trigger for `finalize_jobs` | The `scheduler` service's main loop enqueues `light_recheck`/`stats_flush`/`partition_create`/`daily_rollup`/`retention_drop` on fixed intervals, but **never** `SCRAPE_FINALIZE_JOBS` or `SCRAPE_RECOVER_STALLED`. Jobs whose targets *do* reach a terminal status will never have their parent `ScrapeJob.status` rolled up to `COMPLETED` without this — worth adding to the scheduler's tick loop. |
| `JWT_SECRET` / `SCRAPYD_PASSWORD` are unresolved literal strings | Found incidentally while debugging: these `railway.ts` `generator: 'secret(...)'` directives were never actually evaluated — the app is running with the **literal text** `secret(48, "abcdef...")` as its JWT signing secret, not a random value. This is a real, separate security issue (JWTs are forgeable) — flagged for your security pass, not fixed here per your "later" instruction. |
| Credential exposure earlier in this session | A diagnostic command accidentally printed the full `api` service environment, including `RAILWAY_API_TOKEN` and a `GH_TOKEN`, into this conversation transcript. Recommend rotating both when you get to the security pass. |

---

## 5. Cost

**Railway $ cost:** not obtainable. `railway usage` / `railway usage projects` returned `Unauthorized` for the logged-in account across every attempt — this looks like a billing-visibility permission gap (the account has workspace access but not billing-view rights), not a transient error. You (or the workspace owner) can check actual cost at the Railway dashboard's usage page.

**Application-level cost:** the app itself tracks **no monetary cost anywhere** — no proxy billing, no compute cost, no per-request dollar figure on any job/result table. The only budget-like field (`ProxyProvider.monthly_budget_limit`) is an integer *request quota*, not currency.

**What I can report instead — operational cost of today's test run:**

| Step | Real requests made | Notes |
|---|---:|---|
| Workspace/competitor/product/match creation | ~50 API calls | All succeeded first try |
| Job dispatch attempts (3 full rounds × 21 jobs) | 63 job creations | 2 rounds failed before Scrapyd deploy fix; only round 3 reached a real spider |
| Scrapyd crawl attempts | 63 spider invocations | 42 crashed instantly (reactor bug, both before/after the wrong first fix); 21 ran cleanly but made 0 HTTP requests (discovery gate) |
| **Actual external HTTP requests to jarir.com / noon.com / amazon.sa** | **0** | Blocked by the discovery cold-start gate described above |

No requests ever reached the actual competitor sites, so there is no meaningful external bandwidth/proxy cost to report for this run.

---

## 6. Recommended Next Steps

1. Decide how to handle the discovery cold-start gate (see §3) so this catalog — or a real one — can actually produce prices.
2. Fix the two `.railway/railway.ts` gaps (§4) so the pgbouncer/auth fixes survive the next infra apply.
3. Add a real Scrapy-project deploy step to the `scrapers` Dockerfile/entrypoint.
4. Add `SCRAPE_FINALIZE_JOBS` to the scheduler's periodic tick loop.
5. When you get to the security pass: rotate `RAILWAY_API_TOKEN`/`GH_TOKEN`, and fix the `JWT_SECRET`/`SCRAPYD_PASSWORD` unresolved-generator issue.
