# Production readiness re-score — 2026-09

Re-scores `CORE_AND_SAAS_REVIEW_2026-09-03.md` §1 (blockers B1–B4) and §2
(high-value fixes H1–H12) after EPA run
`plan-production-readiness-final-2026-09-03` completed every implementation
phase (0, F-eng, A, B, C, D, E, F-saas, G-code).

- Engine: `/srv/crawmatic/crawmatic`, branch
  `epa/plan-production-readiness-final-2026-09-03`, head **5d3ab4f**.
- SaaS: `/srv/crawmatic/saas`, same branch name, head **63ff769** (23 commits
  ahead of `origin/main` c22f06d).
- Written 2026-09-05, before any release.

## Summary — the target is NOT met, and cannot be met before Phase R

The 2026-09-03 review scored the product **64/100 — Risky** on four blockers
and twelve high-value gaps. All four blockers and eleven of the twelve H items
now have code on the two run branches, each with a unit or integration proof and
a passing phase gate (engine `pytest tests/unit -m "not integration"`: 3,991
passed; SaaS `vitest`: 10,081 passed | 15 skipped, 511 files). **But no
deployment has happened in this run.** The owner's standing amendment — no
deploys and no Railway writes until every implementation phase is complete and
verified — put every release, canary and production proof into a final,
owner-triggered Phase R that is still held. Consequently the plan's own G5
target is not met and could not have been: **G1 (D1 canary), G2
(deploy-survival) and G3 (3 h idle soak) have not run**; the **E10 chat smoke
transcript does not exist** (it needs both a release and a real
`OPENAI_API_KEY`, which the box does not have); and **Scenario 2 pricing is
code-complete on both surfaces but live on neither** (the marketing `web` deploy
and the SaaS releases are all queued, and **every SaaS release on this branch —
starting with the first one, C5, not just the pricing release D6 — is blocked by
the owner's Stripe price-seeding step**, because `4d0409d` made
`STRIPE_PRICE_STARTER/GROWTH/SCALE` required at boot and it is already in the
history of the C5 build). One H item (H12, disk) is a
pure owner action that remains open, and two more (H3 fair queue, H10 second
SaaS replica) are deliberate deferrals with triggers. Read this document as a
**code-readiness re-score of 88/100** — a judgement over the same checklist,
counting only what code and tests prove on this box — while
**production-readiness stays at the review's 64/100 until Phase R runs**,
because not one line of this work is running in production. §5 below is the
release runbook that Phase R must execute, in order, to move the second number.

### What Phase R must still do to meet the plan's target
1. Engine releases 1–3 and SaaS releases 1–3 with the prerequisites in §5 (env
   pins, **the Stripe price keys before the FIRST SaaS release**, the SaaS
   `PROXY_BROWSER` migration ahead of the engine transport counters, the
   netledger buffer drain).
2. Run G1 → G2 → G3 in that order and record their evidence — these are the
   only proofs that close B1 and B2 in production.
3. Run the 2026-10-01 fleet-budget check that closes B3 in production (G4's
   scratch-DB proof is done; the calendar check is not).
4. Run the RLS integration test and the C4 live contract test against a real
   stack before C5 — neither has ever executed.
5. Deploy the marketing `web` service and the SaaS pricing release so Scenario 2
   is actually live, then flip `PRICING_IS_PROVISIONAL`.
6. Set a real `OPENAI_API_KEY`, run the E10 manual smoke checklist in §4 and
   attach the transcript.

---

## 1. Blockers (review §1)

Score vocabulary: `CLOSED (code + unit proof)` · `CLOSED pending prod proof
(Phase R: <task>)` · `DEFERRED (owner: <who>, by: <date or trigger>)` ·
`SKIPPED (owner decision)`.

| Item | Fix tasks | Commits | Unit / integration proof | Prod proof | Score |
|---|---|---|---|---|---|
| B1 | A1 (durable `breaker_evaluate` cadence), A2 (auto-close + `/ready` evidence check), G3 (idle soak) — *breaker cold-start deadlock stops all paid scraping after one idle hour* | engine `7814eae`, `96d56ce` | Phase A gate PASS: cadence at `PROXY_BREAKER_EVAL_INTERVAL_SECONDS` (300 s × 4 ≤ 3600), idempotency test derived from `DURABLE_CADENCE_KEYS` (8 keys), `PROXY_BREAKER_AUTO_CLOSE_AFTER_SECONDS=3600`, five-case auto-close matrix pinned by tests, `/ready` `breaker_evidence` participates in the 200/503 decision; suite 3,843 passed | **G3 3 h idle soak — NOT RUN** | CLOSED pending prod proof (Phase R: G3) |
| B2 | A3 (STARTED-target reaper + job deadline), G2 (deploy-survival test) — *deploys orphan RUNNING jobs forever* | engine `0c81cb8` | Phase A gate PASS: revert sweep provably cannot reach a non-STARTED status; deadline sweep scoped to RUNNING jobs and `PENDING/STARTED/DEFERRED` targets; reverted rows clear `dispatched_at`, `dispatch_intent_id`, `locked_at`, `started_at`; `target_started_age_seconds=2_700.0`; tick order reap → finalize | **G2 redeploy-scrapers-mid-job — NOT RUN** | CLOSED pending prod proof (Phase R: G2) |
| B3 | A4 (cap policy + 6-hourly carry-forward cadence), G4 (roll-forward proof) — *fleet monthly budget cap evaporates at the month boundary* | engine `6ce4d0e`, `5d3ab4f` | Phase A gate PASS (never lowers or overwrites a non-NULL limit, never writes counters, carries the latest earlier cap, in-pass feedback). **G4 integration proof DONE** on a self-provisioned `postgres:18-alpine` at alembic head: seeded `2026_10`, called `roll_fleet_budget_caps_forward(now=2026-10-28)` → proxy `2026_10`/`2026_11` = 75,000,000 µUSD, browser = 25,000,000 µUSD; second call `written=0` (idempotent). Evidence: `/srv/crawmatic/evidence/g4-roll-forward-2026-09-04/REPORT.md` | **2026-10-01 production check — NOT RUN** (owner); cap env vars + seeding are Phase R steps | CLOSED pending prod proof (Phase R: G4-prod, 2026-10-01) |
| B4 | C1 (`control_plane_rules` + `product_ceiling`), C2 (six routes + ceiling enforcement), C3 (SaaS desired-state emitters + live client), C4 (live contract test), C5 (release) — *SaaS ↔ engine control plane unimplemented on both sides* | engine `da19005`, `053ceea`, `565d90d`; saas `0029517`, `7fd7e49`, `e8b36e6` | Phase C gate PASS after one fix cycle. Cross-repo contract checked field-by-field (external_id `monitor-<projectId>`, status vocabulary, cadence sets 60/360/720/1440/10080, snake_case bodies, `Bearer` + `hmac.compare_digest`, 409 envelopes). Engine suite 3,991 passed; alembic head `c8d2e3f4a5b6` (an alembic revision id, not a git SHA); SaaS targeted 19 files / 412 passed. C3 blocker (sticky DELINQUENT on a recovered, portal-cancelled subscription) fixed by `e8b36e6` | **Never executed against a real stack:** `tests/integration/test_control_plane_rls.py` (collects only) and the C4 live contract test (11 skipped without `ENGINE_CONTRACT_BASE_URL` + `SAAS_SERVICE_TOKEN`). C5 release NOT RUN | CLOSED pending prod proof (Phase R: C5) |

---

## 2. High-value fixes (review §2)

| Item | Fix tasks | Commits | Unit / integration proof | Prod proof / owner | Score |
|---|---|---|---|---|---|
| H1 | F1 (Phase R, env-only) — *worker/scheduler liveness invisible; `/ready` reports `heartbeats: not-configured`; no uptime check, no APM* | none (no code change required) | none — the `/ready` heartbeat machinery already exists; only `READY_REQUIRED_HEARTBEAT_SERVICES` is unset in prod | Set `READY_REQUIRED_HEARTBEAT_SERVICES=scheduler,worker` at release. External uptime-probe account signup was **not authorized** for this run | DEFERRED (owner: owner, by: Phase R task F1 — same window as engine release 1; external probe by the first paying customer) |
| H2 | F2 — *40-thread Starlette pool vs `DB_POOL_SIZE=5`* | engine `34c3677`, `1dae342` | `API_THREAD_POOL_SIZE=8`, `DB_POOL_SIZE` 5→8, `DB_MAX_OVERFLOW` 2→4; startup hook `apps/api/app/thread_pool.py:configure_thread_pool` sets the anyio limiter; `tests/unit/test_api_thread_pool.py` 3 tests incl. a TestClient boot asserting the limiter | **Prod Railway pins `DB_POOL_SIZE=5` / `DB_MAX_OVERFLOW=2` explicitly on all five engine services**, overriding the new defaults — must be raised or unset at engine release 1 or the fix does nothing | CLOSED pending prod proof (Phase R: A5) |
| H3 | B4 — *efficiency levers shipped but OFF (`JOBS_COALESCING_ENABLED`, `SCHEDULER_FAIR_QUEUE_ENABLED`)* | engine `b88236e` | `JOBS_COALESCING_ENABLED` default flipped to `True` (`config.py:820`); `SCHEDULER_FAIR_QUEUE_ENABLED` **also flipped to `True` by EPA B3 (F07, 2026-09-07)** — the 2026-09-04 deferral assumed the flag gated fairness only, but it also gates per-rule failure isolation, the bounded-retry/dead-letter ledger, the fleet/domain caps and B3's durable occurrence claim, none of which is a tenant-count question; both pinned by `tests/unit/test_w4_flag_defaults.py` incl. env override | Confirm the prod values carry the new default after engine release 2 (prod sets every flag NULL today, so code defaults rule) — the fair pass is now the default scheduling path, so watch `scheduler: fair pass ...` counts on the first tick after deploy | Coalescing: CLOSED pending prod proof (Phase R: B6). Fair queue: CLOSED in code, pending prod proof |
| H4 | B1 (micro-USD ledger), B2 (price by bytes / browser seconds), B4 (cap re-seeding note) — *cost model wrong by 47×–2,174× from the one-cent floor* | engine `04acd4b`, `d2728f2`, `b88236e` | Phase B gate PASS. 15 (table, column) pairs migrated to µUSD by alembic revision `b7c1d2e3f4a5` (an alembic revision id, not a git SHA; one head, `ALTER … USING`, downgrade reverses); the reviewer re-derived the pricing arithmetic independently (PROXY 113,900 B → 107 µUSD; BROWSER 2,530,000 B + 7.7 s → 2,504; DIRECT → None); `MICRO_UNITS_PER_USD` single definition, residual `cost_minor_units` = 0; suite 3,911 passed | Data migration + cap re-seeding are engine release 2 steps; the netledger Redis buffer must be drained first (payload keys renamed) | CLOSED pending prod proof (Phase R: B6) |
| H5 | F3 — *unit suite not runnable standalone (5 tests need the `pgbouncer` host)* | engine `c49619f` | `integration` marker registered in `pyproject.toml`; exactly 5 tests marked; `-m integration --collect-only` → `5/3811 collected (3806 deselected)`; CI runs `-m "not integration"`; suite 3,787 passed / 5 deselected | none needed | CLOSED (code + unit proof). Residual: CI never runs those 5 tests (documented gap) |
| H6 | F4 — *release manifest drift (`source.dirty: true`, stale SaaS SHA)* | engine `ef141b5` | `scripts/write_release_manifest.py` records `source.engine_sha` / `source.saas_sha` / `source.dirty` and exits 2 unless cleanliness is confirmed; `build_deployment_attestation.py::load_and_verify_manifest` raises on `source.dirty is True` (independent second gate); `docs/DEPLOY-ROLLBACK.md` Step 0 added | Only a real deploy proves the manifest regenerates | CLOSED pending prod proof (Phase R: A5) |
| H7 | F5 — *SaaS production env validated lazily, not at boot* | saas `dcb2c8b` | `setup.ts:16-18` calls `assertProductionEnv(); resolveAiProvider(); announceBoot();` in that order; `assertProductionEnv` throws synchronously; `setup.test.ts` covers missing-key-rejects, full-env-resolves, non-production-tolerant | Boot behaviour changes on the next SaaS release — and `STRIPE_PRICE_*` now falls under the same assertion (see §3) | CLOSED pending prod proof (Phase R: SaaS release) |
| H8 | F6 — *Stripe webhook answers 500 on a bad signature* | saas `176fecf`, `66f9876` | `webhook.ts` maps `StripeSignatureVerificationError` and `SyntaxError` to `HttpError(400)`, rethrows `HttpError` unchanged, keeps 500 for the inbox insert; `webhookSignature.test.ts` generates genuine forged/valid signatures with the real Stripe SDK: forged→400, missing header→400, malformed-but-signed→400, inbox throws→500, valid→200 | Stripe dashboard retry behaviour observable only after release | CLOSED pending prod proof (Phase R: SaaS release) |
| H9 | 0.2 — *undeployed work (prod `9446e71` vs `origin/main` `c22f06d`)* | — (deploy task) | none | **Wider, not narrower:** the run branch is now 23 commits ahead of `c22f06d`. The 0.2 deploy is subsumed by the SaaS releases in §5 | DEFERRED (owner: owner, by: Phase R task 0.2 / SaaS release 1) |
| H10 | F8 — *all 20 PgBoss crons run inside the single API server process* | none | none | `PG_BOSS_NEW_OPTIONS` variable + a 7-day verify are queued as F8; a second server replica with jobs pinned is a separate, larger change | Variable: DEFERRED (owner: owner, by: Phase R task F8). Second replica: DEFERRED (owner: owner, by: the first customer beyond the pilot tenant, or p95 API latency > 1 s) |
| H11 | F7 — *Prisma compound-unique `upsert` with `scopeUserId: null`* | saas `0bddd36` | Three sites (`trialConfigAdmin.ts`, `matching/operations.ts`, `judgePrismaPorts.ts`) converted from `.upsert()` to `findFirst` → `update`/`create`; `noNullCompoundUpsert.test.ts` scans 200+ non-test `src/**/*.ts(x)` files and self-tests both the bug shape and the fix | Global `PlatformConfig` writes stop 500-ing after release | CLOSED (code + unit proof); prod effect pending the SaaS release. Residual TOCTOU race and scanner scope → DEFERRED-ITEMS |
| H12 | 0.1 — *box disk at 95 % (3.7 GB free); the DR cron skips dumps below 2 GB* | none (ops) | Authorized subset executed: plugin ZIPs and `/srv/crawmatic/-o` deleted, the two `mushtryati_products*.json` gzipped, `backups/` archived and **verified 121/121 files, `gzip -t` OK, 411 MB at `/srv/crawmatic/backups-archive/backups-2026-09-03.tgz`** | **STILL OPEN.** `rm -rf /srv/crawmatic/backups/*` was denied by the session permission classifier; docker prune (~8.15 GB reclaimable) was never authorized; off-box storage of the archive was never authorized. Free space ≈ 3.9 GB | DEFERRED (owner: owner, by: before the first Phase R build — the SaaS deploy script's backup-freshness gate fails below 2 GB) |

**Row check:** 4 blocker rows + 12 H rows = 16 scored items.

---

## 3. Scenario 2 pricing — code-complete on both surfaces, live on neither

| Surface | Commits | What landed | Live? |
|---|---|---|---|
| SaaS app (`app.crawmatic.com`) | saas `67ac9b1` (D1) | `PLAN_TIERS`: Starter $49 / 300 products / 9,000 credits / cap 100; Growth $129 / 1,500 / 45,000 / cap 200; Scale $349 / 6,000 / 180,000 / cap 600; `ENTERPRISE_CONTACT` by quote at 6,001+. The nine-row economics table was re-derived by hand and by importing `projectPlanCadence`; band boundaries live-verified; `productCeilingForPlan` derives from `PLAN_TIERS`, so the engine ceiling tracks the catalogue | **No** — held for the SaaS release |
| Marketing site (`crawmatic.com`, Railway `web`) | saas `267164c` (carried edits) + `26d766a` (D4) | `PLAN_ANCHOR_SAR` 189/489/1309, `PLAN_CREDITS_PER_MONTH` 9,000/45,000/180,000, `PLAN_PRODUCT_LIMIT` 300/1,500/6,000, `ENTERPRISE.minProducts = scale + 1`; EN copy exact and AR copy byte-exact; `npm test` 6 passed, build exit 0 | **No** — the `web` deploy is a Phase R step |

Supporting work: **D2** `4d0409d` — idempotent Stripe price seeder
(`crawmatic_{plan}_monthly_v2` lookup keys, unit amounts 4900/12900/34900,
creates only missing prices, never archives) and
`STRIPE_PRICE_STARTER/GROWTH/SCALE` promoted to **required** production env.
**D3** `539073d` — one-off plan migration script (dry-run default, `--apply`).
**D5** `47bfe39` — COGS calibrated to the 2026-09-03 measurement, `PROXY_BROWSER`
split out from proxied HTTP.

Two provisional flags are still `true` and must be flipped at release:
`PRICING_IS_PROVISIONAL` (`pricing.ts:520`; the flip touches
`pricing.test.ts:148`, `PlansPage.tsx:136` and its test) and
`COGS_IS_PROVISIONAL` (`cogs.ts:172`; flip together with `PROXY_BROWSER`
2,400 → 250 after the B5 canary).

### The Stripe owner gate — it binds the FIRST SaaS release, not D6
`STRIPE_PRICE_STARTER`, `STRIPE_PRICE_GROWTH` and `STRIPE_PRICE_SCALE` are now
asserted at boot by `serverEnv.ts`. **Any SaaS deploy from this branch at or
after `4d0409d` refuses to start until all three are set in Railway.** Only a
deploy pinned at or before `7fd7e49` escapes the gate.

**The first such deploy is SaaS release 1 (task C5), not D6.** C5 carries the
C3 fix `e8b36e6`, and `git merge-base --is-ancestor 4d0409d e8b36e6` is true —
`4d0409d` is already in its history. `BLOCKERS.md` records the gate as binding
"the SaaS release that carries 4d0409d (D6)"; that note went stale the moment
`e8b36e6` landed on top of `4d0409d`. Following the old ordering literally would
crash-loop the SaaS server on the C5 release. The owner step is therefore placed
before C5 in §5.

The values come from an owner-run `STRIPE_API_KEY=… npx vite-node
src/scripts/stripeSeedPrices.ts -- --run` — live Stripe seeding and price
archiving were **not authorized** for this run, so every SaaS release from C5
onward (C5, D6, E10) is owner-gated on this step. The seeder never archives; a
price mismatch is a manual money decision (bump `LOOKUP_KEY_VERSION`).

Business signal, not a test artifact: **Project M's margin is 85.47 %, below the
150 % floor**, re-derived by the Phase D reviewer from the calibrated rates.

---

## 4. AI agent (E1–E10)

| Task | Commit | Status |
|---|---|---|
| E1 — provider streaming chat with tool calls, fast/strong tiers | saas `b641426` | CLOSED (code + unit proof) |
| E2 — agent tool registry (33 tools: 26 metric + `search_references` + `project_summary` + 5 actions) | saas `5987aa6` | CLOSED (code + unit proof) |
| E3 — per-project and fleet monthly AI budgets + `ai_spend_month` metric | saas `6239c36` | CLOSED (code + unit proof) |
| E4 — agent loop + sourced-numbers guard + `ChatMessage.meta` (migration `20260905010000_chat_message_agent_meta`) | saas `ee06f16` | CLOSED (code + unit proof) |
| E5 — SSE streaming endpoint (`chatStream`) | saas `9349c69` | CLOSED (code + unit proof) |
| E6 — assistant-ui local runtime client | saas `63ff769` | CLOSED (code + unit proof) |
| E7 — Assistant is the first nav item | saas `374e05e`, `0c98b99` | CLOSED (code + unit proof) |
| E8 — real-model judge benchmark | — | DEFERRED (owner: owner, by: an `OPENAI_API_KEY` on the box — none exists today) |
| E9 — Anthropic provider | — | SKIPPED (owner decision) |
| E10 — SaaS release 3 + manual chat smoke | — | **PENDING Phase R.** Needs a release, a real `OPENAI_API_KEY`, and the Stripe gate above. **No smoke transcript exists; none is attached to this document.** |

Phase E gate: **PASS** (fresh reviewer, isolated worktree at `63ff769`). Full
suite in that worktree: `Test Files 510 passed | 1 skipped (511) / Tests 10,081
passed | 15 skipped (10,096)`. Design invariants verified: the sourced-numbers
guard runs on every final text; `CONFIRM_REQUIRED` has exactly one exit
(`actions.prepare()` → `proposeTool`) and the five action tools refuse with
`ACTIONS_UNAVAILABLE` without an actions port; the budget is checked before any
provider call; abort propagates end-to-end and still books cost.

### E10 manual smoke checklist (Phase R, owner) — copied from `phase-E-review.md`
- Real SSE through the Railway proxy: `X-Accel-Buffering: no` / `Cache-Control: no-transform` honoured (incremental deltas, not one burst); 15 s `: ping` keeps the connection alive past proxy idle timeout on a long tool-calling turn.
- `Authorization: Bearer <sessionId>` reaches the api service cross-origin (CORS preflight allows the header; `credentials:'include'` does not break it).
- Wasp JSON-parses `req.body` on the custom `api` route in a real POST (a 400 on the first real message is the tell).
- Pre-header refusals arrive as JSON with the right status (401/404/402/429) and the client shows its own sentence.
- Abort: close the overlay mid-run → server completes the round, row persisted, no orphan provider call; reopen shows the stored answer.
- Guard: force a `guard_tripped` and confirm prior deltas vanish; only the deterministic quotation remains.
- Budget: with `ai.monthlyBudgetMicros` low → router fallback with the "AI budget for this month is used up" notice in en + ar; no `CostEvent` written for that turn.
- CONFIRM_REQUIRED: frequency change → preview card with Confirm/Cancel; nothing applied until Confirm; kill switch blocks Confirm.
- Long-answer autoscroll and RTL on the route and the overlay; Assistant first in the sidebar with Sparkles in both languages.

---

## 5. Phase R release runbook

Assembled from the run's task notes (`STATE.md` rows A5, B6, C5, D6, G4;
`BLOCKERS.md`; the F-eng / A / B / C / D / E / F-saas phase reviews). Every
prerequisite below is traceable to one of those notes. **Nothing here has been
executed.** Releases deploy from the run branches — EPA does not merge to
`main`; merge and tags stay with the owner.

### R0 — before any build
1. **Free disk (H12).** `rm -rf /srv/crawmatic/backups/*` — safe against the verified archive `/srv/crawmatic/backups-archive/backups-2026-09-03.tgz` (121/121 files, `gzip -t` OK, 411 MB). Then `docker image prune -af --filter until=168h` and `docker builder prune -af` (~8.15 GB reclaimable: images 3.76 GB + volumes 4.39 GB; review the volumes before touching them). Then move `backups-archive/` **off-box**. The SaaS deploy script's backup-freshness gate fails while free space is this low.
2. **Regenerate the release manifest** with `scripts/write_release_manifest.py` (F4). It exits 2 unless cleanliness is confirmed, and `build_deployment_attestation.py` refuses `source.dirty: true` independently.

### Engine release 1 (task A5) — the A-phase blocker fixes
Carries F2/F3/F4 + A1–A4 (engine `1dae342` … `6ce4d0e`).
- **Raise or unset `DB_POOL_SIZE` and `DB_MAX_OVERFLOW` on all five engine services.** Prod pins 5/2 explicitly, which overrides the new 8/4 defaults and nullifies H2.
- **Set `FLEET_BUDGET_MONTHLY_CAP_USD_PROXY=75` and `FLEET_BUDGET_MONTHLY_CAP_USD_BROWSER=25`.** This also silences the boot-time `fleet_budget_uncapped` ERROR that fires on every prod API start until the vars exist.
- Set `READY_REQUIRED_HEARTBEAT_SERVICES=scheduler,worker` (F1) in the same window; add the external uptime probe once an account exists.
- Attach the netledger volume (F9) and set the `PG_BOSS_NEW_OPTIONS` variable (F8) while the window is open.

### Before engine release 2 — the SaaS migration ordering trap
**Apply the SaaS migration `20260904000000_cost_category_proxy_browser`
(`ALTER TYPE "CostCategory" ADD VALUE 'PROXY_BROWSER'`) to production BEFORE
engine release 2 ships transport counters.** The sequencing is documented only —
there is no runtime guard. If the counters arrive first, the nightly
`marginReportJob`'s `createMany` fails and the whole project-day cost-event
batch is lost. Either run `prisma migrate deploy` by hand — the safe option if
no SaaS release is ready yet — or order a SaaS release carrying `aa99721` ahead
of engine release 2. **If you choose the release, the Stripe owner gate below
must be done first**: every SaaS build on this branch except one pinned at or
before `7fd7e49` contains `4d0409d` and will not boot without
`STRIPE_PRICE_STARTER/GROWTH/SCALE`.

### Engine release 2 (task B6) — money truth, cap seeding, canary
Carries B1/B2/B3-engine/B4/B5 (engine `04acd4b` … `dccb4f5`).
- **Drain the netledger Redis buffer before deploying** — B1 renamed the payload keys.
- Alembic revision `b7c1d2e3f4a5` (an alembic revision id, not a git SHA) rescales 15 columns via `ALTER … USING` — a real data migration. **Upgrade straight to head**: alembic revision `c4b19e7a2f08` imports live model SQL constants, so stopping at an intermediate revision leaves a trigger naming columns that no longer exist (proven by G4's scratch run).
- After the deploy, seed the caps (owner step B4, from the engine repo with prod DB access — run each with `--propose` first):
  ```
  python scripts/seed_fleet_budget_cap.py --monthly-cap-usd 75 --scope-key proxy   --months-ahead 1 --apply
  python scripts/seed_fleet_budget_cap.py --monthly-cap-usd 25 --scope-key browser --months-ahead 1 --apply
  ```
  Then export DataImpulse's monthly usage and run
  `app_shared.netledger.reconcile.reconcile_window` for the first real
  settlement pass under the new caps.
- **B5 canary** (~$0.30): the proxied-browser domain list is read through the scrapers-browser process's cached `get_settings()`, so **the listed run needs a config change plus a redeploy of `scrapers-browser`**. The script prints a two-phase owner runbook and compares `network_operations` rows by `scrape_job_id` (`--baseline-job-id` / `--candidate-job-id`; there is no `policy_version` column, so `BLOCKLIST_VERSION` travels in the report header). Acceptance: within 3 points **and** ≤ 400,000 bytes/page; an empty phase fails.
- After the canary, flip `COGS_IS_PROVISIONAL` and `PROXY_BROWSER` 2,400 → 250 in the SaaS.

### Before C5 — run the tests that have never run
- `tests/integration/test_control_plane_rls.py` (engine) — collects but has never executed; needs Postgres plus the C1 migration.
- The C4 live contract test (SaaS) — 11 tests skip without `ENGINE_CONTRACT_BASE_URL` **and** `SAAS_SERVICE_TOKEN` pointed at a real engine stack.

### Owner gate — Stripe price keys, BEFORE the first SaaS release
**This must happen before SaaS release 1 (C5), not at D6.** C5 carries
`e8b36e6`, which has `4d0409d` in its history, and `4d0409d` made the three
price keys required at boot. Skip this and the C5 SaaS deploy crash-loops.
1. Run `STRIPE_API_KEY=… npx vite-node src/scripts/stripeSeedPrices.ts -- --run`.
   It prints the three `STRIPE_PRICE_*=price_…` lines. It creates only missing
   prices and never archives — a mismatch is a manual money decision
   (`LOOKUP_KEY_VERSION` bump), and archiving old prices stays manual.
2. Set `STRIPE_PRICE_STARTER`, `STRIPE_PRICE_GROWTH` and `STRIPE_PRICE_SCALE`
   on the SaaS server service in Railway.
3. Only a SaaS deploy pinned at or before `7fd7e49` escapes this gate — no
   release in this runbook is.

### Engine release 3 + SaaS release 1 (task C5) — control plane live
Carries C1/C2 (engine `da19005`, `053ceea`, `565d90d`) and C3/C4 (saas
`0029517`, `7fd7e49`, `e8b36e6`). **Prerequisite: the Stripe owner gate above.**
- Operator check after the first reconcile window: grep the logs for
  `entitlement push was IGNORED as stale evidence`. Repeated hits for one tenant
  = a KILLED tenant (expected, see the `saas_status` deferred item) or two
  writers racing (not expected).
- Confirm Mushtryati's adopted refresh rule is **interval-driven, not cron** — an
  adopted cron rule keeps its cron while `/state` echoes the requested cadence,
  so drift detection is blind to it (server WARNING only).
- Every existing workspace's replicated `product_ceiling` needs a re-push after
  the SaaS pricing release; the next drift tick does it.

### SaaS release 2 (task D6) — pricing live
- The Stripe price keys are already set — that gate was cleared before C5. If
  C5 was skipped or rolled back, do it now: the server refuses to boot without
  `STRIPE_PRICE_STARTER/GROWTH/SCALE`.
- Run `planMigration2026_09.ts` dry-run, then `--apply`. **No plan-change email
  template exists** — the script logs "scheduled but NOT emailed"; contact the
  listed ids by hand (expected today: none, since the only tenant is
  Scale-sized — confirm by running, not from memory).
- Flip `PRICING_IS_PROVISIONAL` together across `pricing.ts:520`,
  `pricing.test.ts:148`, `PlansPage.tsx:136` and its test.

### Marketing `web` deploy (task D4)
- **Owner decision first:** the carried pre-run edits removed pricing from the
  home page and the nav, so `/pricing` (and therefore the Enterprise card) is
  reachable only by URL. Confirm this is the intended end state before
  deploying.

### SaaS release 3 (task E10) — the AI agent
- Set a real `OPENAI_API_KEY` in Railway (none exists on this box).
- Run the §4 manual smoke checklist and attach the transcript to this document.

### Production proofs, in order
1. **G1** — D1 canary on prod (~$0.10).
2. **G2** — deploy-survival: redeploy `scrapers` mid-job and confirm the reaper releases the orphaned STARTED targets (closes B2).
3. **G3** — 3 h idle soak, then confirm proxied work is still authorized (closes B1).
4. **G4-prod** — **2026-10-01 calendar check:** `fleet_cost_budgets` must show capped `2026_10` **and** `2026_11` rows without anyone running the seeder (closes B3).

---

## 6. Open decisions for the owner

1. **Retire the legacy Open SaaS billing surface?** Hard Stop 4 (2026-09-04) was resolved as "keep it for now": `src/payment/plans.ts`, the `PAYMENTS_*` keys, the webhook `getPlanIdByPriceId` mapping and the `User.subscriptionPlan/subscriptionStatus/credits` writes stay; only the legacy checkout route/page/utils were removed. Retirement is a dedicated post-go-live task.
2. **`ai_spend_month` is `adminOnly`.** The payload is raw COGS, which `saas/CLAUDE.md` and the copy guard forbid on customer surfaces — so the metric refuses for every merchant. A merchant-visible "assistant usage this month" needs a **derived, non-COGS** metric. New scope; decide whether to build it.
3. **CI `ENGINE_CONTRACT_BASE_URL`.** `build-check.yml` exports only placeholder `CRAWMATIC_API_URL` / `SAAS_SERVICE_TOKEN` and provisions no engine stack, so the C4 live contract test reports skipped in CI exactly as it does locally. Add a staging URL plus a token secret (or an engine service in the job), or accept that the contract is unverified between releases.
4. **Marketing pricing is unlinked** from the home page and nav (see §5). Intended, or restore the links before the `web` deploy?
5. **Project M's margin is 85.47 %, below the 150 % floor.** A pricing, cadence or COGS decision — not a test failure.
6. **A configured fleet cap of `0` is treated as absent** (threshold < $0.005) and the previous month's cap is carried forward at INFO. An operator typing `0` to mean "stop all spend" will not get that; the spend kill-switch is the breaker, not a zero budget. Confirm the semantics or change them.
7. **Second SaaS replica with jobs pinned** (H10) — when? All 20 PgBoss crons currently share the API process with request traffic.
8. **When to flip the provisional flags** — `PRICING_IS_PROVISIONAL` at the pricing release, `COGS_IS_PROVISIONAL` after the B5 canary. Both are still `true`.

---

## 7. What remains

Everything deferred, with an owner and a trigger, is registered in
[`DEFERRED-ITEMS.md`](./DEFERRED-ITEMS.md) — including the four items the plan
named explicitly (fair queue, second SaaS replica with jobs pinned, Amazon
HTTP-leg extraction research, S-Tech identity) and every non-blocking finding
carried forward by the phase reviews.

---

## 8. 2026-09 post-Stage-D re-score

**This section scores a different, later EPA run than §§1–7 above: `plan-core-production-readiness-2026-09-07`,
engine repo `/srv/crawmatic/crawmatic`, branch `epa/plan-core-production-readiness-2026-09-07`, HEAD
`9e932fa` (phases 0/A/B/C committed) plus Stage D's D1/D2/D3/D5/D4/D6 work, which remains
UNCOMMITTED in the tree per this run's own convention (the orchestrator commits per phase). §§1–7
score the prior `plan-production-readiness-final-2026-09-03` run and its branch; they are left
unmodified above as the historical record of that run. No SaaS repo work is in scope for this run
(core/engine only).**

Reproduces `CORE_PRODUCTION_AUDIT_2026-09-07.md` §13's twelve-row release-gate table. Per
`.epa/plan-core-production-readiness-2026-09-07/ASSUMPTIONS.md` answer 2, this run can end at best
`COMPLETE-WITH-DEFERRED-GATES` — every step that needs a deployed release, a staging environment, or
real spend was deferred to the owner by design. **A fully-PASS table was never an achievable outcome
of this run, and none of the twelve rows below is marked PASS.** Every row that depends on a
deferred owner gate, a deferred staging run, or a deferred spend step is marked **NOT YET MEASURED**
with the exact blocking gate named — never PASS. Rows that combine both real, on-branch evidence and
a still-deferred piece are marked **PARTIAL** and say exactly which half is which. **The plan is not
complete while any row below is not PASS** — this table is a certification instrument, not a
scorecard to be argued up.

The unit gate this section's own verification ran against (see `Verify:` below) is
`4,971 passed, 19 skipped, 6 deselected, 0 failed` — identical to Task D1's own aggregate, confirming
no regression from D2/D3/D5's uncommitted work landing on top of it.

| Area | Verdict | Evidence | Blocking gate |
|---|---|---|---|
| Daily freshness | **NOT YET MEASURED** | Mechanism built and unit-tested: `fleet_daily_scorecard` (Task D5, `tests/unit/test_scorecard_fields.py`, 15 tests) computes `terminal_fraction_24h` from real SQL; `scripts/fleet_test_report.py` (Task D1, `tests/unit/test_fleet_test_report_gates.py`, 33 tests) turns a scorecard window into this exact gate row. Local 2×50 smoke (`reports/D1.md` §4) proves the pipeline end-to-end at toy scale (118/118 matches resolved). **No real-load or staging-scale measurement exists.** | D1 steps 2–4 (owner-created staging environment, 100×5,000 fixture-origin run, seven daily cycles) — `docs/ops/STAGING_ENVIRONMENT.md`, `docs/ops/FLEET_TEST_2026-09.md`; then D4 Step 1 (real-store rollout) — `docs/ops/ROLLOUT_2026-09.md` |
| Headroom | **NOT YET MEASURED** | The ~2x offered-load step is documented with an exact command (`docs/ops/FLEET_TEST_2026-09.md` §3) and the scorecard has an `offered_load_multiplier` field ready to receive it. No run has happened. | D1 step 4 (the 2x-load hour in staging); D4's own rollout is single-load by design (§1 of `docs/ops/ROLLOUT_2026-09.md`), so this row is settled by D1, not D4 |
| Tail latency | **NOT YET MEASURED** | `fleet_daily_scorecard.queue_oldest_seconds_p95`/`persistence_lag_seconds_p95` are wired from real per-target phase timestamps (Task D5, documented proxy — see Deviation 1, `reports/D5.md`) and unit-tested. **The audit's own named finding — the 55-minute startup gap — was never investigated by any task in this plan** (new deferred item, `docs/DEFERRED-ITEMS.md` 2026-09-08 batch). | D1 steps 2–4 (real due-to-dispatch/dispatch-to-first-network/first-network-to-persisted p95 under load); the startup-gap investigation itself, which no task owns yet |
| Durability | **NOT YET MEASURED** | Both scenarios (crash-after-POST, scraper-kill/reaper-release) are implemented as fault-injection scripts with a pure, fully unit-tested `compute_measurement` (Task D2, `tests/load/fault_injection/kill_worker_after_post.py`/`kill_scraper_after_fetch.py`, `tests/load/test_fault_injection_dry_run.py` 29 passed) plus a staging-guard that refuses a non-staging target. **No live run against a real dispatch/scrape pipeline has happened.** | D2 step 1 — `docs/ops/FAULT_INJECTION_2026-09.md`'s exact deferred command per script |
| Tenant fairness | **PARTIAL — not measured as a whole row** | The "concurrent schedulers do not fire the same occurrence twice" half has **real, DB-backed evidence**: Task B3's `tests/integration/test_two_schedulers_one_occurrence.py` (4 new + 5 pre-existing fair-queue tests, 9 passed) ran two real scheduler processes against a scratch Postgres and observed the atomic occurrence claim hold. The "a blocked/oversized store cannot stall another store's due work" half is only fixture-tested (`test_refresh_pass_isolation.py`'s fake session) — the corresponding fault-injection scenario (Task D2, `one_broken_tenant.py`) has never run live. | D2 step 1 (`one_broken_tenant.py` live run) for the isolation-under-real-load half |
| Host protection | **NOT YET MEASURED** | Fleet/domain concurrency admission is unit-tested (`tests/unit/test_fleet_admission.py`, `tests/unit/test_limiter_middleware_fleet.py`). Task D2's `host_limit_hold.py` is written to drive three real `multiprocessing.Process` "nodes" against `admit_fleet`/`release_fleet`/`fleet_snapshot` over real Redis — the strongest of the seven fault-injection scripts by design — but **it has never actually been executed**, live or in staging (`reports/D2.md` Blockers: "no staging environment exists yet ... Step 1 ... is DEFERRED"). | D2 step 1 (`host_limit_hold.py` live run) |
| Security | **PARTIAL — not measured as a whole row** | SSRF/redirect/subresource/private-DNS/worker-popup/regex tests are part of the unit gate and pass continuously — re-verified fresh today (this task's own `Verify:` run below), unchanged since Stage A. Component secrets/grants minimization has real evidence: `provision_db_roles.sql` + `verify_grants.py` ran green in the A10, B10 and C11 rehearsals (`rehearse_upgrade.sh`, 10/10 steps, real restored data). **The live cross-tenant RLS integration test (`tests/integration/test_tenant_isolation_roles.py`) last ran in Task A3 (141 passed, real compose Postgres) — before B3, C7, C9 and D5 added four new tables to the schema — and has not been re-run since.** New tables are individually classified in `scripts/rls_table_manifest.txt` and pass the static `scripts/check_workspace_scoping.py` check, but that is not the same evidence as the live RLS-enforcement suite. | Re-run `tests/integration/test_tenant_isolation_roles.py` + `tests/integration/test_grants_manifest.py` against the current head (throwaway or compose Postgres) — `docs/DEFERRED-ITEMS.md` 2026-09-08 batch |
| Quality | **PARTIAL — not measured as a whole row** | The labeled-sample mechanism, per-domain fixtures (`tests/fixtures/labeled_offers/{noon,amazon,stech}.jsonl`) and the exact regression thresholds (`decide_shadow_gate`: shadow disagreement < 1% AND ranker wins >= 99% of labeled conflicts) exist and are exercised by `tests/benchmarks/test_offer_benchmark_gate.py`. **The live decision itself is explicitly BLOCKED**: `docs/ops/RELEASE_3_2026-09.md` §2.11 Decision 2 records "no shadow export has been drained/scored yet" — the mechanism only starts accumulating evidence once this release deploys and `EXTRACTION_RANKING_POLICY=shadow` runs against real traffic. | C11 Decision 2 (drain and score a real shadow export post-deploy) — `docs/ops/RELEASE_3_2026-09.md` §2.11 |
| Economics | **NOT YET MEASURED** | `scripts/reconcile_canary_costs.py` (Task D3) implements the exact reconciliation math (bytes ±10%, CPU ±25%, `cost_model_drift_ratio` in [0.9, 1.1]) and is unit-tested (24 tests) plus CLI-smoked in `--dry-run`. `docs/ops/COST_MODEL_2026-09.md` documents the calibrated cost model with every input labeled by source. **No real-domain canary spend has happened** (C2/C3/C4/D3's spend steps are all deferred, total run spend $0), so no real reconciliation exists, and — separately — **the audit's own "approved bound" for cost-per-valid-refresh was never set by any task** (new deferred item). | D3 step 1 (≤ $3 canary) plus the C2/C3/C4 spend steps that feed the same cost model; **and** an owner decision setting the cost-per-valid-refresh bound itself, which does not yet exist |
| Database | **PARTIAL — not measured as a whole row** | Rollup at realistic volume has **real evidence at the audit's own target scale**: Task D1 ran `tests/benchmarks/test_rollup_500k.py` (previously written, never executed) for real — 500,000 observations, 2.3 s wall clock against a < 1,800 s bar, on a tmpfs-backed Postgres (disk constraint, not a shortcut on scale). Migration has **real evidence at real (if modest) production-dump scale**: the A10/B10/C11 `rehearse_upgrade.sh` rehearsals ran 10/10 steps green against successively newer real backup sets, most recently 16,706 `network_operations` rows including the C9 partition swap. Retention is built and integration-tested (`tests/integration/test_multi_tenant_retention...` per C8) but **ships disabled** (`RETENTION_ENABLED_CLASSES=[]`) pending the owner's C9/C11 ratification (Decision 1) — by design, not a gap in the code. **Pool exhaustion is genuinely unmeasured: no `pool_wait_p95` metric exists anywhere in the engine** (new deferred item; `scripts/fleet_test_report.py` reads it from an operator-supplied file as a stopgap). | C9/C11 Decision 1 (retention ratification) to turn retention on; a `pool_wait_p95` metric, which does not exist yet, before pool exhaustion can be measured at all; D1 steps 2–4 for rollup/migration/pool behavior under real fleet concurrency rather than a single isolated benchmark |
| Recovery | **NOT YET MEASURED** | The restore mechanism itself is real and repeatedly green: `scripts/dr/rehearse_upgrade.sh` passed 10/10 steps in A10, B10 and C11, most recently against the `set-20260908T160001Z` backup set (25 s wall clock). **The off-host `dr-backup` Railway service (Task C10 step 3) has never been created**, so no first measured off-host backup exists, and `scripts/fleet_test_report.py`'s Recovery row deliberately reads `NO DATA` rather than inventing agreed RPO/RTO targets — **no owner decision has ever set those targets**, which the audit's row requires ("demonstrate agreed RPO/RTO at projected size"). | C10 step 3 (create the `dr-backup` service, run one real off-host backup) — `docs/ops/RELEASE_3_2026-09.md` §2.4; an owner decision setting the agreed RPO/RTO targets, which does not exist; D1 steps 2–4 for the "at projected size" clause |
| Operations | **PARTIAL — not measured as a whole row** | Heartbeat emission is wired and tested for worker pools and the scheduler (Task B9, B9-fix1). The D5 scorecard (`fleet_daily_scorecard`) is the freshness/queue-age/persistence-backlog measurement infrastructure the audit's Operations row needs, built and unit-tested (Task D5), but is not yet collecting real data (no deployment). **B9's own follow-up list still has the `heartbeat_missing{service}` alert rule and seven other snapshot-wiring items marked inert** (`BLOCKERS.md` 2026-09-08 B9-fix1 entry) — alerts do not yet reach an owner for every condition the audit's row names (disk, restore failures, budget denials). | B9's own inert-alert follow-ups (heartbeat-missing rule + 7 snapshot-wiring items); deployment, so heartbeats and the scorecard start reporting real data |

**Row check: 12 audit §13 areas, 0 PASS, 5 NOT YET MEASURED outright, 5 PARTIAL (real on-branch
evidence for part of the row, deferred for the rest), 2 rows (Headroom, Economics) folded under a
single blocking gate each with no partial credit claimed.** Consistent with ASSUMPTIONS.md answer 2:
this run ends `COMPLETE-WITH-DEFERRED-GATES`, not `COMPLETE`.

### What closes each remaining row, in the order it becomes possible

1. **First** (no spend, no deploy, doable now): re-run `test_tenant_isolation_roles.py`/`test_grants_manifest.py` against the current head (Security); build a `pool_wait_p95` metric (Database); the owner sets the cost-per-valid-refresh bound and the agreed RPO/RTO targets (two standing decisions with no code dependency).
2. **Then** (owner deploy, Task C11 Step 2): the release ships; `EXTRACTION_RANKING_POLICY=shadow` starts accumulating real shadow events (feeds Quality/Decision 2); heartbeats and the D5 scorecard start reporting real data (feeds Operations); the `dr-backup` service can be created (feeds Recovery).
3. **Then** (owner staging environment, Task D1 steps 2–4): the 100×5,000 fixture-origin run over seven daily cycles plus a 2x-load hour settles Daily freshness, Headroom, Tail latency, and the concurrency-under-load half of Database.
4. **Then** (owner staging, Task D2 step 1): the seven fault-injection scenarios settle Durability, the remaining half of Tenant fairness, and Host protection.
5. **Then** (owner spend, Tasks C2/C3/C4/D3, all ≤ a few dollars): the Amazon HTTP-leg probe, the document-only browser canary, and the cost-reconciliation canary settle Economics and feed the domain-playbook seeding in `docs/ops/RELEASE_3_2026-09.md` §2.2.
6. **Finally** (owner, Task D4): the staged 1 → 5 → 20 → 100 store rollout (`docs/ops/ROLLOUT_2026-09.md`) is the production-scale confirmation that steps 1–5 above generalize past a synthetic fixture and a handful of canaries.

Every step above has its exact command already written — in `docs/ops/STAGING_ENVIRONMENT.md`,
`docs/ops/FLEET_TEST_2026-09.md`, `docs/ops/FAULT_INJECTION_2026-09.md`, `docs/ops/COST_MODEL_2026-09.md`,
`docs/ops/RELEASE_3_2026-09.md`, and `docs/ops/ROLLOUT_2026-09.md` — so none of this re-score's
DEFERRED rows requires new engineering to close, only owner time, a deployment, and a bounded amount
of real spend.

**Verify:** `cd /srv/crawmatic/crawmatic && sudo -u mahmoud .venv/bin/pytest tests/unit -q -p no:cacheprovider -m "not integration"`
(chunked in 6 per the runtime rules, 339 top-level files + 7 subpackages) → exit 0 on every chunk,
aggregate `4,971 passed, 19 skipped, 6 deselected, 0 failed`; `sudo -u mahmoud bash scripts/check_single_head.sh`
→ exit 0, one head `f6b28c714a93`; `sudo -u mahmoud .venv/bin/python scripts/check_workspace_scoping.py`
→ exit 0.
