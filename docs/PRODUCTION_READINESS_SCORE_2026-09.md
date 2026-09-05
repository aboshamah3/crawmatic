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
| H3 | B4 — *efficiency levers shipped but OFF (`JOBS_COALESCING_ENABLED`, `SCHEDULER_FAIR_QUEUE_ENABLED`)* | engine `b88236e` | `JOBS_COALESCING_ENABLED` default flipped to `True` (`config.py:820`); `SCHEDULER_FAIR_QUEUE_ENABLED` stays `False` (`config.py:768`); both pinned by `tests/unit/test_w4_flag_defaults.py` incl. env override | Confirm the prod values carry the new default after engine release 2 (prod sets every flag NULL today, so code defaults rule) | Coalescing: CLOSED pending prod proof (Phase R: B6). Fair queue: DEFERRED (owner: owner, by: 3+ tenants) |
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
