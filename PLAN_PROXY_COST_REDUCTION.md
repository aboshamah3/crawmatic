# Proxy Cost Reduction Plan — quality-neutral by construction

**Date:** 2026-08-11 · **Status: DEPLOYED 2026-08-12** (Fixes 1, 2a, 4, 4b) —
merge commit `f83c0ba` on branch `proxy-cost-reduction`, 2,050 unit tests green.
Fix 2b and Fix 3 rejected by user; not implemented.

**Deploy record (2026-08-12):** required merging the divergent prod line first —
production was running `saas-phase2` (25608c0: scrapy 2.16 reactor, register_egg,
extra.com extraction) while OOS detection / block detection / `dont_retry` /
retry-defer lived only on the scraper branch and had been silently reverted by the
Aug-11 deploy. The merge restores both. Sequence executed: migrate (→
`b7d02a41c9e3`) → seed playbook (13 domains + global amazon profile) → worker →
api → scheduler → scrapers → **egg re-registered** → scrapers-browser.
Verified: all services SUCCESS, deployed spider 850 lines with OOS+dedup+blocking,
scrapyd lists the spider, and a 3-link fqtoners smoke job returned 3/3 COMPLETED
(JSON_LD prices, IN_STOCK, 1 DIRECT_HTTP attempt each, no proxy).
`SCRAPE_URL_DEDUP` deliberately left **unset (inert)** pending the stech A/B.

**Unplanned win:** Fix 4b (discovery early-exit) hit the live extra.com discovery
leak found 2026-08-12 (~$325/mo). Pre-deploy extra.com burned ~3,165 proxied
req/hr; post-deploy ~279 req/hr. The root cause (per-product `url_pattern` →
permanent "template_changed" rediscovery, 186 profiles) is UNFIXED — only the
proxied leg of each run was removed.

**Remaining:** stech A/B → flip `SCRAPE_URL_DEDUP=1`; separately fix the
extra.com url_pattern root cause + a per-domain daily discovery cap.
**Baseline (measured, DataImpulse billing API + prod DB, Aug 10 run):**
scrape run ≈ $1.96 (amazon 1,379 MiB / noon 335 / stech 291), local matching ≈ $1.45
(fqtoners alone 23,450 req / 799 MiB). Duplicate fetches measured: amazon 2,716
attempts for 1,097 distinct URLs, stech 1,826 for 766 — same URL fetched up to 6×
successfully in ONE job.

**Prime directive:** every change must produce byte-identical or better data. Where a
change could alter observed data even theoretically, it is gated or dropped.

**Branch warning:** the working tree is on `saas-phase2`; the scraper engine running in
prod is `scraper/browser-realistic-request-headers` (has `dont_retry`, block detection,
retry-defer, OOS sniffing — all absent from `saas-phase2`). Scraper-side work (Fixes 1,
2) MUST be implemented on the prod scraper branch. Fix 3 is a local script. Fix 4
touches shared libs + API and needs a merge decision per branch.

---

## Fix 1 — Fetch each distinct URL once per job, fan results out per match

**Why safe:** every downstream artifact (price_observations, request_attempts,
match_current_prices, target status, job counters, webhooks, per-variant
PRICE_ANALYSIS_RECOMPUTE, /jobs/{id}/results) is keyed by `match_id` and is preserved
1:1. Only the number of HTTP fetches changes. Extraction is page-level
(`variant_strategy` is stored but consumed nowhere in scrape-core), so same page ⇒ same
price for all sibling matches, by construction.

**Where (prod scraper branch paths):**
- `libs/scrape-core/scrape_core/targets.py::load_targets` — after building SpiderTargets,
  group by **(workspace_id, competitor_id, normalized_competitor_url, resolved
  scrape_profile_id, access_policy_id, mode)**. First member = fetcher; the rest ride as
  `sibling_targets` on the fetcher. Gate: only group when the resolved profile's
  `variant_strategy == PAGE_SINGLE_PRICE` (today: always) — forward-guard for
  variant-aware extraction.
- Admission (`dispatch_admission`): acquire the match lock for the fetcher AND each
  sibling; a sibling whose lock is held is dropped from the group and emitted as its own
  fetch (never skipped silently) — preserves current LOCKED_ALREADY_RUNNING semantics.
- `generic_price_spider.parse` / `errback` (+ browser twin): build one ScrapeResult per
  sibling from the single response — per-match `match_id`, lock key/token, attempt
  kwargs. Success, PRICE_NOT_FOUND/OOS, and transport failure all fan out, so every
  sibling target terminalizes and jobs finalize exactly as today.
- `pipelines._flush_batch`: unchanged — it already iterates per-item and writes per match.
- Retry ladder: retries re-dispatch the fetcher with siblings intact (attempt_number
  advances on the fetcher; sibling RequestAttempt rows mirror the outcome per attempt,
  same as today's per-match rows).

**Explicitly NOT doing:** dedupe at target creation (drops per-match observations,
breaks job accounting), cross-job caching of pages (would serve stale prices — quality).

**Verification:** unit tests on grouping key + fan-out; staging job over a
variant-heavy competitor (stech) asserting: distinct-URL fetch count == distinct URLs,
observation count == match count, identical prices vs a pre-change control run, job
finalizes, per-variant recomputes fire for every sibling variant.
**Measured expectation:** amazon −60% fetches, stech −58% ⇒ ≈ $0.8–1.0 saved per full run.

---

## Fix 2 — Stop paying repeatedly for deterministic failures (REVISED after research)

**Research correction:** parse failures (`PRICE_NOT_FOUND`, `INVALID_PRICE_FORMAT`) are
already never retried in-run — `parse` emits and returns; only the errback (transport,
non-2xx, blocked-page) re-dispatches. Blocked interstitials are detected pre-parse
(`blocking.py`) and retried with a fresh proxy exit — that path is quality-critical and
stays untouched. The measured 1,112 PRICE_NOT_FOUND rows are ~all attempt-1 PROXY_HTTP,
repeated **across jobs**: 199 matches re-downloaded by 3–7 jobs since Aug 9.

Two sub-fixes:

**2a. Phantom browser-fallback attempt (pure waste, zero quality value).** The HTTP
spider's terminal ladder step is `PLAYWRIGHT_PROXY`, but `dispatch_admission` dispatches
it as a plain HTTP request — a 4th paid download identical to attempt 3, with no browser.
Fix on prod branch: when the plan's method is `PLAYWRIGHT_PROXY` and the spider is HTTP,
skip the attempt (terminalize with the prior error). No data ever came from this step
that attempt 3 didn't already produce.

**2b. DROPPED (user decision 2026-08-11): full cadence stays.** Layout-gap matches
keep being fetched by every job; the ~$0.2/run stays as the price of zero staleness
risk. Only 2a ships from this fix.

---

## Fix 3 — SKIPPED (user decision 2026-08-11): matcher `fetch.py` stays proxy-first

Kept for reference; not being implemented.

### Original proposal — `fetch.py` (matcher) direct-first with automatic proxy fallback

**File:** `.claude/skills/match-competitors/scripts/fetch.py` (local script; biggest
single saving, ~$1.3/day of matching activity).

- Host policy table in-script: `PROXY_FIRST_HOSTS = {www.amazon.sa, amazon.sa,
  www.noon.com, noon.com, www.google.com, www.bing.com}` (bot-walled; evidence:
  TLS-fingerprint blocks). Everything else: direct first.
- Fallback triggers (any ⇒ automatic refetch via proxy, so worst case = today's
  behavior): curl failure; HTTP ≥ 400 (curl `-w %{http_code}`); body < 2 KB; existing
  bot-wall regex hit (`captcha|are you a robot|just a moment|access denied`).
- Output marks which path served the page (`VIA: direct|proxy`) so match evidence stays
  auditable.
- Quality evidence: prod Railway scrapers fetch fqtoners/pcpalace/jarir/afaq/extra
  direct with validated prices — same pages, same egress class.
- Update `matching/COMPETITOR_SCRAPE_PROFILES.md` accordingly.

---

## Fix 4 — Competitor profiling: sane defaults for NEW competitors, one profile per
competitor across ALL workspaces

**Facts from research:** extraction profiles (`scrape_profiles`), access policies, and
proxy providers already support global rows (`workspace_id NULL`, RLS-readable by all,
assignable to any workspace's competitor) — prod's `global_default` policy is exactly
that. The per-workspace duplication problem is the **learned strategy profile**
(`domain_strategy_profiles`): strictly workspace-scoped (NOT NULL, composite FK,
RLS-owned), auto-created per (workspace, competitor, url_pattern), and each one runs its
own discovery ladder — up to 10 **proxied** fetches per method leg — even for a domain
another workspace already learned. Making that table global would break FK/RLS/repo
invariants; we don't touch it.

**Design: global domain playbook (seed-through, not shared-mutable):**
1. New global table `domain_playbooks` (no workspace_id): `domain` (unique),
   `preferred_access_method`, `scrape_profile_name` (FK-by-name to a **global**
   `scrape_profiles` row), `access_policy_name`, `notes`, `updated_at`.
2. Seed it from current production knowledge: amazon.sa (PROXY_HTTP + amazon profile),
   noon.com (PROXY_HTTP), stech.ink (PROXY_HTTP per rate-limit rule), all direct-HTTP
   sites (DIRECT_HTTP + their extraction profiles, converted to global rows named by
   domain, e.g. `profile:jarir.com`).
3. On competitor creation (`POST /v1/competitors`) and on first-target auto-seed
   (`strategy/resolution.py`): if the competitor's domain has a playbook entry, (a) set
   `competitor.default_scrape_profile_id` to the global profile when the caller didn't
   pass one, and (b) create the `domain_strategy_profile` pre-seeded
   ACTIVE from the playbook — **skipping the discovery probe entirely**. Unknown domain
   ⇒ current behavior (discovery), plus Fix-4b below.
4. NO publish-back (user decision 2026-08-11): the playbook is a curated catalog of
   well-known competitors, seeded once from today's production knowledge and edited by
   operators. Workspaces consume it fully automatically (new tenant adds amazon.sa ⇒
   gets the amazon profile + PROXY_HTTP with zero discovery cost) but never write to
   it. Unknown domains fall through to per-workspace discovery as today (with the 4b
   early-exit); per-workspace rediscovery can still diverge locally if a site treats
   tenants differently (quality safety valve).

**4b. Discovery ladder early-exit (new competitors, unknown domains):** in
`_probe_sample`, if `DIRECT_HTTP` qualifies on the **entire** sample, skip the
`DIRECT_HTTP_RETRY` and `PROXY_HTTP` legs — nothing can beat a full-sample qualifier
under the existing winner rule (most qualifying URLs, cheapest on tie), so the outcome
is provably identical, minus up to 20 paid fetches per new profile group.

**Also folded in (bugs surfaced by research, cheap while we're there):**
- Dispatch-mode routing reads mode only from the per-match profile override, ignoring
  competitor/global defaults (`tasks_jobs._resolve_domains_and_modes`) — resolve through
  the real chain so a future BROWSER default routes correctly.
- Access-policy Redis cache has no invalidator (30 s TTL only) — add one alongside the
  profile-cache invalidator; global-row edits must invalidate across workspaces.

---

## Open decisions (need user)

- **D1 (Fix 2b):** enable weekly-backoff for layout-gap matches (recommended; bounded
  ≤7-day staleness on pages that yield nothing today), or keep full cadence and accept
  the ~$0.2/run?
- **D2 (Fix 4):** playbook publish-back automatic for access method only (recommended),
  fully automatic (access + extraction profile), or fully manual curation?
- **D3 (rollout order):** proposal: Fix 3 immediately (local script, no deploy) → Fix 1
  + 2a on prod scraper branch behind a settings flag (`SCRAPE_URL_DEDUP=1`), one staging
  A/B run vs control before enabling in prod → Fix 2b (if approved) → Fix 4 (schema +
  API, biggest surface).

## Projected effect (against the $3.41 measured day)

| Item | Saving | Quality delta |
|---|---:|---|
| Fix 1 URL dedup | ~$0.9/run | none (same pages, fanned out) |
| Fix 2a phantom browser attempt | ~$0.05–0.1/run | none (was a duplicate no-op) |
| Fix 2b gap backoff (if approved) | ~$0.2/run | ≤7-day staleness on 199 dead pages |
| Fix 3 matcher direct-first | ~$1.3/matching-day | none (auto proxy fallback) |
| Fix 4 playbook + early-exit | ~$0.02–0.2/new competitor group | none (provably same winner) |
| **Total** | **~65–75% of proxy spend** | |
