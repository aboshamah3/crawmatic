# Deferred Items

A running register of decisions deliberately deferred rather than made
now — each entry says what was deferred, why, and what would trigger
picking it back up. No dedicated register existed in this repo before
EPA plan task B4 (2026-09-04); the other candidates checked
(`docs/ops/*`, root `PLAN_*.md`, `GAP_ANALYSIS.md`) are point-in-time
plans/runbooks that happen to use the word "deferred," not a durable
list, so this file was created fresh at `docs/DEFERRED-ITEMS.md`.

Newest entries first.

Every entry below carries an **Owner** and a **Trigger**. The 2026-09-05 batch
was collected by EPA task G5 from `BLOCKERS.md` and the phase reviews of run
`plan-production-readiness-final-2026-09-03`; see
[`PRODUCTION_READINESS_SCORE_2026-09.md`](./PRODUCTION_READINESS_SCORE_2026-09.md)
for the scored checklist those findings came from.

## 2026-09-05 — Cost lever: Amazon HTTP-leg extraction research

The 2026-09-03 cost report ranks this the single largest remaining cost lever:
amazon.sa is **77.5 % of all proxy bytes** and drives the browser path, which is
the largest compute line. Extracting the Amazon leg over plain HTTP would make
the browser path rare. No research has been done; this is a spike, not a fix.
Owner: owner. Trigger: monthly proxy spend above $30, or before onboarding a
second Amazon-heavy catalogue.

## 2026-09-05 — Cost lever: direct-first for extra.com

306 proxied requests, **18.6 % of proxy spend**, on a site whose profile
suggests an HTTP search API. Owner: owner. Trigger: same as the Amazon lever —
picked up in the same pass.

## 2026-09-05 — Coverage: fix S-Tech identity

**707 wasted targets per run.** This is a coverage defect, not a cost one — the
matcher spends work on targets that can never resolve. Owner: owner. Trigger:
the next matching-quality pass, or the first customer complaint about missing
S-Tech coverage.

## 2026-09-05 — Second SaaS replica with PgBoss jobs pinned to it (review H10)

All 20 PgBoss crons — monthly billing, discovery, briefings, the Stripe inbox —
run inside the single API server process and compete with request latency.
Acceptable at pilot scale. The fix is a second Railway server replica with the
job runner pinned to it. Owner: owner. Trigger: the first customer beyond the
pilot tenant, or p95 API latency above 1 s.

## 2026-09-05 — External uptime probe and APM (review H1, second half)

`READY_REQUIRED_HEARTBEAT_SERVICES=scheduler,worker` is a Phase R env change
(task F1). The second half of H1 — an external uptime check and any APM at all
(there is no Sentry in either codebase, structured logs only) — needs an account
signup that was not authorized for this run. Owner: owner. Trigger: before the
first paying customer.

## 2026-09-05 — `workspace_entitlements` needs a `saas_status` column

PAUSED and KILLED both store engine `SUSPENDED`, and `/state` echoes KILLED back
as PAUSED. The SaaS therefore sees permanent drift for a KILLED tenant and
re-pushes the entitlement on every reconcile tick: engine answers 200
`stale_evidence`, `drift.entitlementReplicated` stays `false`, and a WARNING is
logged every tick. Run outcome is still SUCCEEDED and nothing is lost — the cost
is one extra PUT and one WARN per KILLED tenant per tick. Owner: engine
maintainer. Trigger: the first KILLED tenant in production, or the next
`workspace_entitlements` migration (fold it in).

## 2026-09-05 — Adopted cron refresh rules silently ignore the sold cadence

When the control plane adopts an existing cron-driven refresh rule it keeps the
cron, but `/state` echoes the *requested* cadence, so `ruleDrift` sees no
mismatch — only a server WARNING marks the divergence. Fix: echo the effective
cadence in `/state`, or refuse adoption of cron rules. Owner: engine maintainer.
Trigger: the C5 operator check finds Mushtryati's adopted rule is cron-driven,
or a second tenant is onboarded onto an existing rule.

## 2026-09-05 — SaaS has no handler for the engine's 409 `PRODUCT_CEILING_EXCEEDED`

Catalogue sync over the ceiling fails with a generic loud `EngineError` and no
merchant-facing copy. No data loss. Owner: SaaS maintainer. Trigger: the first
merchant who crosses their plan's product ceiling.

## 2026-09-05 — `productCeiling` NULL is coerced to 0

`engineControlPlane.ts getWorkspaceState` does `Number(… ?? 0)`, losing the
NULL ≠ 0 distinction that the C1 column deliberately preserves ("no ceiling"
becomes "ceiling of zero" in the SaaS's view). Owner: SaaS maintainer. Trigger:
the first workspace intentionally left uncapped.

## 2026-09-05 — `findWorkspaceLinks` runs inside the inbox Prisma transaction window

`Project.findMany` is issued on the non-transactional client inside the Stripe
inbox transaction — exactly the class of long-transaction hazard the inbox
buffering exists to prevent. Owner: SaaS maintainer. Trigger: any Stripe inbox
latency regression, or the next `eventInbox` change.

## 2026-09-05 — The RLS and live-contract tests have never executed

`tests/integration/test_control_plane_rls.py` (engine) collects but has never
run — it needs Postgres plus the C1 migration. The SaaS C4 live contract test
reports 11 skipped both locally and in CI, because `build-check.yml` exports
only placeholder `CRAWMATIC_API_URL` / `SAAS_SERVICE_TOKEN` and provisions no
engine stack; the test is gated on `ENGINE_CONTRACT_BASE_URL` +
`SAAS_SERVICE_TOKEN`. Owner: owner (CI secrets) + engine maintainer (a stack in
the job). Trigger: **before the C5 release** for the manual run; before the next
control-plane change for the CI wiring.

## 2026-09-05 — No plan-change email template exists

`planMigration2026_09.ts` writes the schedule columns and then logs "scheduled
but NOT emailed" through an injected notifier port. Affected accounts must be
contacted by hand (expected today: none — the only tenant is Scale-sized).
Owner: owner. Trigger: the D6 release run of the migration script returns a
non-empty id list.

## 2026-09-05 — Retire the legacy Open SaaS subscription/credit surface

Hard Stop 4 (2026-09-04) was resolved as *keep it for now*: `src/payment/plans.ts`,
the `PAYMENTS_*` env keys, the webhook `getPlanIdByPriceId` mapping and the
`User.subscriptionPlan/subscriptionStatus/credits` writes all feed the live
payments webhook and the pages that render it. Only the legacy checkout
route/page/utils were removed (saas `4d0409d`). Retiring the rest is a dedicated
post-go-live task with its own migration of the account/admin surfaces. Owner:
owner. Trigger: after Scenario 2 pricing has been live and stable for one full
billing cycle.

## 2026-09-05 — Dead `paymentFailed` / `paymentRecovered` intents

Both intents exist with zero production callers, and **no writer ever sets a
project `Subscription.status` to `PAST_DUE`** even though the enum has it.
Delinquency lives only on `User.subscriptionStatus` plus the engine desired
state. Either wire the intents or delete them. Owner: SaaS maintainer. Trigger:
the next billing-state change, or the legacy-surface retirement above.

## 2026-09-05 — F7 leaves a benign TOCTOU race on the global `PlatformConfig` row

The `upsert` → `findFirst`/`create`/`update` conversion (H11) is strictly better
than the 500 it replaced, but two concurrent writers can both miss on
`findFirst` and the second `create` throws P2002 instead of falling through to
`update`. All three sites are low-concurrency admin/cron paths. Fix: a
transaction with `SELECT … FOR UPDATE`, or catch-and-retry on P2002. Owner: SaaS
maintainer. Trigger: the first P2002 in production logs, or any of these paths
becoming user-triggered.

## 2026-09-05 — `noNullCompoundUpsert.test.ts` only guards `key_scopeUserId`

`AgentRun.projectId_kind_sourceRef` and `ProductMirror.projectId_externalId`
upserts are safe today purely by static typing (`sourceRef` / `externalId` are
`string`, never nullable, in every port interface). The scanner would not catch
a regression if one of those interfaces were later loosened to `string | null`.
Owner: SaaS maintainer. Trigger: any change that makes a compound-unique
component nullable.

## 2026-09-05 — The SaaS has no typecheck gate

`npx tsc --noEmit -p .` reports pre-existing errors (`eventInboxJob.ts` narrowed
entities type, `w2Acceptance.test.ts` `notifyPaidOrder`, `reconciler.test.ts:517`
`entitlementStaleEvidence`), and vitest does not typecheck. A TypeScript/Prisma
enum mismatch has no automated gate — this is exactly how the Phase B
`PROXY_BROWSER` blocker reached a gate review. Owner: SaaS maintainer. Trigger:
before adding any new Prisma enum value; ideally a `typecheck` script in CI
first.

## 2026-09-05 — Engine: scope the STARTED revert sweep to non-terminal jobs

`revert_stale_started_targets` (A3) is not scoped to RUNNING jobs, so a STARTED
orphan attached to an already-terminal job becomes a PENDING orphan that nobody
dispatches. Not a regression — `target_pending_age_seconds` surfaces it — but
worth tightening. Owner: engine maintainer. Trigger: the first non-zero
`target_pending_age_seconds` alert that traces to a terminal job.

## 2026-09-05 — Engine documentation drift after the A-phase fixes

Three doc-level items: the `breaker.open` alert copy in `opsmetrics/rules.py`
still says recovery is manual (A2 made it automatic); the fleet-budget seeder
runbook text says the same; and nothing documents that a configured cap of `0`
is treated as **absent** (threshold < $0.005) so the previous month's cap is
carried forward — an operator typing `0` to mean "stop all spend" will not get
that. Owner: engine maintainer. Trigger: the next docs pass, or any operator
confusion report.

## 2026-09-05 — `proxy_bytes` excludes browser sub-resource bytes

The usage-export aggregation counts only network operations reachable from an
attempt row, so a browser page's child operations' bytes are excluded. Harmless
today; it understates bytes the moment `PROXY_BROWSER` calibration goes
byte-based. Owner: engine maintainer. Trigger: any move to byte-based
`PROXY_BROWSER` pricing.

## 2026-09-05 — No integration-DB test for the usage-export aggregation

B3's engine half is verified by compile-string assertions plus a router fixture
only. Owner: engine maintainer. Trigger: the next change to the usage-export
SQL.

## 2026-09-05 — Engine test/runtime housekeeping

CI never runs the five `integration`-marked tests (documented gap accepted by
the plan — they need the compose `pgbouncer` host), and FastAPI `on_event` is
deprecated in 0.139 and should migrate to `lifespan`. Owner: engine maintainer.
Trigger: the next CI overhaul (integration job), and the next FastAPI major
bump (lifespan).

## 2026-09-05 — AI agent: sourced-numbers guard is sign-blind

`NUMBER_TOKEN` (`numbersAreSourced.ts:29`) drops a leading `-`, so "fell -12 %"
passes against evidence containing `12`. Owner: SaaS maintainer. Trigger: the
next guard change; low risk today.

## 2026-09-05 — AI agent: guard can over-trip on today's date

`now` is in the system prompt JSON (`prompts.ts:71`) but not in the evidence set
(`loop.ts:157` seeds `[input.summary]` only), so a model that states today's
date trips the deterministic fallback. Fix is one extra evidence entry. Owner:
SaaS maintainer. Trigger: the first support report of a spurious fallback.

## 2026-09-05 — AI agent: mid-stream provider failure leaves unguarded prose on screen

`runTurn` fails over to the router (`runTurn.ts:497-503`), but `streamApi.ts:215`
suppresses the replacement because `sawText` is true and
`chatStreamAdapter.ts:346` only substitutes the stored row when `text === ''`.
The **persisted** row is the sourced router answer, so a reload is correct — but
the live screen keeps unguarded partial model prose. Fix: emit `guard_tripped`
before the failover. Owner: SaaS maintainer. Trigger: before the AI assistant is
promoted out of its current audience, or the first such report.

## 2026-09-05 — AI agent: cost is not booked when a provider stream throws mid-stream

`openaiProvider.ts` throws out of `chat()` without `done()` / `recordCost`, so
tokens already consumed never reach `CostEvent` and the monthly budget
under-counts on broken streams. Owner: SaaS maintainer. Trigger: the first month
where AI spend materially exceeds the recorded `ai_spend_month`.

## 2026-09-05 — OWNER DECISION: merchant-visible "assistant usage" metric

`metric_ai_spend_month` is `adminOnly` — the payload is raw COGS, which
`saas/CLAUDE.md` and the copy guard forbid on customer surfaces — so the metric
is handed to every merchant's model and always refuses for non-admins
(gracefully). Reviewer judged `adminOnly` correct. A merchant-visible
"assistant usage this month" therefore needs a **derived, non-COGS** metric:
new scope, owner decision. Secondary: filter `adminOnly` specs out of the tool
list for non-admins to save prompt tokens. Owner: owner (decision), SaaS
maintainer (build). Trigger: the first merchant question about assistant usage.

## 2026-09-05 — AI agent: `request_match_review` is unreachable and `s_` handles are never recorded

`handleSchema` accepts `[psm]_\d{1,3}` but `searchReferences` mints only `p_` and
`s_` handles, so every `request_match_review` call refuses at `prepare` — dead
weight in the registry. Follow-up: add a `search_matches` tool or drop the tool.
Latent alongside it: `recordingSearch` re-derives the handle book with
`competitors: []`, so `s_` handles are never recorded — harmless today
(`add_or_exclude_competitor` takes a domain) but a live bug the day an action
takes an `s_` handle. Owner: SaaS maintainer. Trigger: adding any action tool
that consumes an `s_` or `m_` handle.

## 2026-09-05 — AI agent: handle-book safety (verified sound, no action)

Recorded for completeness — the Phase E reviewer verified the per-turn handle
book is filled only by `search_references`, and an unknown or stale handle makes
`prepare` throw so the tool refuses. **No path can act on the wrong product.**
No action required; listed so a future refactor knows this property is
load-bearing. Owner: SaaS maintainer. Trigger: any change to the handle book or
`prepare`.

## 2026-09-05 — AI agent: duplicate `searchReferences` query per lookup

`recordingSearch` issues a second `searchReferences` query for every lookup — one
extra DB round trip per reference search. Owner: SaaS maintainer. Trigger: the
next chat-latency pass.

## 2026-09-05 — AI agent: budget metric reports the default cap, not the override

`metric_ai_spend_month` reports the default $5 cap; the enforced cap is whatever
`checkAiBudget` resolves. Related: the per-project budget override is keyed by
`Project.ownerId` (not project id), because `PlatformConfig` has no project
scope — so multi-project owners share one override. This mirrors
`trialConfigResolver.ts`. Both need documenting at minimum. Owner: SaaS
maintainer. Trigger: the first owner with two projects who wants different AI
budgets.

## 2026-09-05 — AI agent: abort-on-unmount is unverified

The client adapter forwards `runOptions.abortSignal`, but whether assistant-ui's
LocalRuntime actually fires it when the overlay unmounts mid-run has never been
observed. Owner: owner. Trigger: the E10 manual smoke run (it is on that
checklist).

## 2026-09-05 — Pricing/COGS follow-ups from Phase D

Four small items, all non-blocking: (1) `marginJob.test.ts:196-203` now asserts
`n * COGS_UNIT_COST_MICROS.X`, which is tautological and safe only while
`cogs.test.ts:68-79` pins the literals; (2) `PROJECT_M_COSTS` MODEL_OUTPUT says
140,000 where 9 × 15,000 = 135,000 — **pre-existing**, untouched by D5, the
fixture is not fully self-consistent; (3) `activeProjects` is computed once per
window rather than per day (fine for the nightly 7-day window, a hazard for long
historical re-runs); (4) the price seeder's CLI exit codes 2/3 are not asserted
through `main`. Owner: SaaS maintainer. Trigger: the next COGS calibration pass.

## 2026-09-05 — Owner item: Project M is below the margin floor

Re-derived from the calibrated rates: **85.47 % against a 150 % floor.** This is
a real business signal, not a test artifact — it is a pricing, cadence or COGS
decision, and no code change will move it. Owner: owner. Trigger: before the
Scenario 2 pricing release goes live.

## 2026-09-04 — `SCHEDULER_FAIR_QUEUE_ENABLED` left `False`

Single-tenant fleet in production today — weighted fair queuing
(`app_shared.scheduling.fair_queue`) has nothing to arbitrate fairly
between when there is only one workspace generating scheduler load.
Revisit at 3+ tenants. (EPA plan task B4.)

## 2026-09-03 — `UsageSnapshotArchive` (SaaS) not extended with the B3 proxy counters

`proxied_http_attempted`, `proxied_browser_attempted`, `proxy_bytes`
(EPA plan task B3, engine commit adding them to `GET /v1/admin/usage`)
were not backfilled into the SaaS side's `UsageSnapshotArchive` model —
archived snapshot copies of a usage row drop the three counters. Live
`/v1/admin/usage` rows are unaffected; this only affects historical
snapshots taken via the archive path. (Finding carried over from an
earlier task; recorded here per EPA plan task B4.)

## 2026-09-04 — Owner step B4: seed the fleet monthly budget caps after this release deploys

**Not done by this task — engine repo changes only, no deploys, no DB
writes.** Once the engine release containing this plan's changes
migrates to production, the owner must run, from the engine repo with
production DB access:

```
python scripts/seed_fleet_budget_cap.py --monthly-cap-usd 75 --scope-key proxy --months-ahead 1 --apply
python scripts/seed_fleet_budget_cap.py --monthly-cap-usd 25 --scope-key browser --months-ahead 1 --apply
```

(`--scope-key` is repeatable and singular per the script's own
`argparse` definition, not `--scope`; `proxy` / `browser` are the real
`FLEET_PROVIDER_PROXY` / `FLEET_PROVIDER_BROWSER` scope-key values from
`app_shared.costauth.fleet_budget_policy.DEFAULT_SCOPE_KEYS` — `direct`
is free and intentionally excluded. Run each command with `--propose`
first, without `--apply`, to see the dry-run diff before committing.)

Then export DataImpulse's usage for the month and run
`app_shared.netledger.reconcile.reconcile_window` against it (see
`apps/workers/app/workers/tasks_maintenance.py` for the existing
scheduled call site and `libs/shared/app_shared/netledger/reconcile.py`
for the function contract) so the fleet ledger's estimated costs get
their first real settlement pass under the new caps.
