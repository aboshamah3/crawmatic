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

## 2026-09-08 batch — surfaced by EPA run `plan-core-production-readiness-2026-09-07`, Stage D (D1–D6)

Collected from `.epa/plan-core-production-readiness-2026-09-07/{BLOCKERS.md,ASSUMPTIONS.md}` and
the Stage D task reports by EPA task D6; see
[`PRODUCTION_READINESS_SCORE_2026-09.md`](./PRODUCTION_READINESS_SCORE_2026-09.md)'s "2026-09
post-Stage-D re-score" section for the gate table these items block.

## 2026-09-08 — No `pool_wait_p95` metric exists anywhere in the engine

`scripts/fleet_test_report.py`'s Database gate row needs a DB-connection-pool
wait-time p95 to judge the audit's "< 100 ms" bar, and no such metric is
collected anywhere in the codebase today (checked: `app_shared.opsmetrics`
collects gauges from live tables, not from the SQLAlchemy pool itself). The
report script reads it from an operator-supplied measurements file as a
stopgap. Fix: instrument `SQLAlchemy`'s pool checkout/checkin events (or sample
`pg_stat_activity` wait events) and emit a real gauge. Owner: engine
maintainer. Trigger: before the Database gate row can be marked PASS, or
before the D4 rollout's step 3 (20 stores) if pool contention becomes
observable informally first. Evidence: `reports/D1.md` Blockers.

## 2026-09-08 — `VACUUM ANALYZE` must run before the first rollup after any restore or bulk seed

D1 found the rollup batch statement is catastrophically plan-sensitive to
stale table statistics: one batch of 500 variants over 100,000 freshly
bulk-loaded observations had not completed after 18 minutes at 100% CPU: the
planner chose a nested-loop plan from statistics that still described the
partition as empty. The same statement took 0.19 s after `ANALYZE`. This is
now documented as a manual step in `docs/ops/STAGING_ENVIRONMENT.md` §5 and
`docs/ops/FLEET_TEST_2026-09.md` §2.2, but it is not wired into
`scripts/dr/rehearse_upgrade.sh` or any restore runbook as an automatic step —
a human has to remember it. A monthly partition rollover (a much smaller
version of "just-created, statistics describe it as empty") is the same
failure mode at production scale. Owner: engine maintainer (whoever owns
C7/C8/the restore tooling). Trigger: before the next restore-runbook or
rollup-benchmark change, or the first production incident that looks like a
"rollup hung" report.

## 2026-09-08 — The audit's "cost per valid refresh" bound was never set

Audit §13's Economics gate and this plan's own D4 ADVANCE bar both require
"cost per valid refresh <= the C11-approved bound," but no task in this plan
set that number: `docs/ops/RELEASE_3_2026-09.md` §2.11 records three owner
decisions (retention ratification, `EXTRACTION_RANKING_POLICY`,
`BROWSER_DOCUMENT_ONLY_DOMAINS`) and none of them is a cost-per-refresh
ceiling. `docs/ops/ROLLOUT_2026-09.md` §2 flags this explicitly: the D4
rollout cannot honestly evaluate its own ADVANCE condition 4 until this number
exists. Owner: owner, informed by `docs/ops/COST_MODEL_2026-09.md`'s
calibrated cost table and the D3 canary reconciliation. Trigger: before D4
Step 1 (enabling the first production store) can honestly evaluate its
ADVANCE bar.

## 2026-09-08 — The audit's 55-minute startup-gap finding (Tail latency, audit §13) was never investigated

Audit §13's Tail latency row explicitly asks to "investigate the observed
55-minute startup gap." No task in this plan (Stage A through D) investigated
it — `grep` across every task report finds no mention of it. The D5 scorecard
now carries phase p95 fields (`queue_oldest_seconds_p95`,
`persistence_lag_seconds_p95`) that could support the investigation once real
traffic exists, but the gap itself remains unexplained. Owner: engine
maintainer. Trigger: before the Tail latency gate row can be marked PASS, or
the D1 staging fleet test's `due_to_dispatch`/`dispatch_to_first_network` p95
figures if they reproduce something like the gap.

## 2026-09-08 — The live cross-tenant RLS integration test has not been re-run since Stage A

`tests/integration/test_tenant_isolation_roles.py` (141 assertions across all
reviewed tables) last ran, and passed, in task A3 against a real compose
Postgres — before B3 added `refresh_rule_occurrences`, C7 added
`rollup_completion`, C9 partitioned `network_operations`, and D5 added
`fleet_daily_scorecard`. Every one of those new tables was individually
classified in `scripts/rls_table_manifest.txt` and unit-checked by
`scripts/check_workspace_scoping.py` (a static check, not a live-DB RLS
enforcement test) at the time it was added, but the full live suite that
actually exercises Postgres RLS policies under the provisioned roles has not
been run end-to-end against the tree as it stands after Stage D. Owner: engine
maintainer. Trigger: before the Security gate row can be marked PASS —
`sudo -u mahmoud .venv/bin/pytest tests/integration/test_tenant_isolation_roles.py tests/integration/test_grants_manifest.py -q -m integration`
against a throwaway or compose Postgres provisioned by `provision_db_roles.sql`
at the current head.

## 2026-09-08 — `EVIDENCE_STORE_DIR` / Railway volume not yet mounted in production

C5's raw-evidence hash (`offer_raw_evidence_hash`) is wired end-to-end in code
and tests but stays `NULL` in production until a Railway volume is mounted on
the relevant service(s) and `EVIDENCE_STORE_DIR` is set — by design, not a
bug. No size cap on `raw_evidence` beyond Scrapy's `DOWNLOAD_MAXSIZE` is a
related follow-up. Owner: owner (volume + variable) / engine maintainer (size
cap). Trigger: before the Quality gate's labeled-sample evidence needs raw
page bodies for a real (non-fixture) domain, or the C11 release-3 deploy
window.

## 2026-09-08 — Disk on the build host is at 99% (1.4 GB free), worse than the 97–98% recorded through Stages A–C

Every Docker-backed or disk-backed verification in this run had to route
around this (tmpfs Postgres data directories, throwaway containers torn down
immediately, deferred integration suites). It is not merely a build-host
inconvenience: the SaaS deploy script's own backup-freshness gate and any real
staging/production restore both need headroom this host does not have. Owner:
owner (H12 in the 2026-09-03 review's own re-score is the same underlying
item, still open). Trigger: before any staging environment (D1) or production
restore rehearsal that needs disk-backed Postgres is attempted on this host;
consider ephemeral tmpfs-based verification the standing workaround, not a
fix.

## 2026-09-08 — ~110/122 Stage-C files (and more since) are root-owned in the working tree

Flagged by the phase-C review (2026-09-08): most files this run's workers
touched are owned by `root:root` rather than `mahmoud:mahmoud`, an artifact of
how the box's Docker/root-privileged steps interact with file creation. Not a
security issue in itself (single-operator host), but it means the next worker
to touch one of these files needs `chown mahmoud:mahmoud <file>` before an
edit succeeds as `mahmoud`. Owner: engine maintainer / whoever administers
this build host. Trigger: before granting a second human or a lower-privilege
CI runner write access to this tree.

## 2026-09-08 — Compose Postgres major (17.5) does not match production (18.4/18.6)

Re-confirmed independently by A10, B10, C10, C11 and D1: `docker-compose.yml`
pins `postgres:17.5-bookworm`; the newest production backup manifests say
18.4 (engine) / 18.6 (SaaS). Every throwaway-container rehearsal in this run
that needed the *real* major used a dedicated container at the manifest's
version, not compose — `docker-compose.yml` itself is the one place still
carrying the stale pin. Owner: engine maintainer. Trigger: the next
`docker-compose.yml` edit, or before trusting a compose-based integration run
as representative of production behavior.

## 2026-09-08 — Two pre-existing integration test files have known bugs, unrelated to this plan's own code

`tests/integration/test_partition_create_live.py` (a stale `webhook_events`-absent
assertion; a `(scraped_at, id)` vs `(id, scraped_at)` identity-key ordering bug
on `price_observations`) and `tests/integration/test_retention_drop_live.py`
(missing `latest_alert_type` on insert; needs a `rollup_completion` row once
the first bug is fixed). Confirmed pre-existing and out of every Stage C/D
task's declared scope (`reports/C9.md`, `docs/ops/RELEASE_3_2026-09.md` owner
item 10). Owner: engine maintainer. Trigger: the next change to either test
file's subject area, or a dedicated test-debt sweep.

## 2026-09-05 — `BLOCKERS.md` records a stale SaaS release-ordering note

The run's `BLOCKERS.md` says `STRIPE_PRICE_STARTER/GROWTH/SCALE` must be set
"BEFORE the SaaS release that carries 4d0409d (D6)". That was true when written
and went stale when `e8b36e6` landed on top of `4d0409d`:
`git merge-base --is-ancestor 4d0409d e8b36e6` is now true, so the **first** SaaS
release (C5) already carries it and would crash-loop without the keys. The
runbook in `PRODUCTION_READINESS_SCORE_2026-09.md` §5 carries the corrected
order; `BLOCKERS.md` itself is a run artifact outside this repo and was not
edited. Owner: owner. Trigger: reading `BLOCKERS.md` while planning Phase R —
prefer the §5 runbook where the two disagree.

## 2026-09-05 — Cost lever: Amazon HTTP-leg extraction research — **TOOLING CLOSED 2026-09-08 (EPA C2); LIVE RUN STILL DEFERRED**

The 2026-09-03 cost report ranks this the single largest remaining cost lever:
amazon.sa is **77.5 % of all proxy bytes** and drives the browser path, which is
the largest compute line. Extracting the Amazon leg over plain HTTP would make
the browser path rare.

**What EPA plan task C2 closed (2026-09-08):** the "no research tooling exists"
gap. `scripts/probe_amazon_http_leg.py` (new) implements the full probe: a
`--targets/--runs/--max-usd/--report` CLI, the exact >=80% / 30–80% / <30%
decision table from `docs/ops/AMAZON_STRATEGY_2026-09.md`, spend-guarded
(`SpendGuard`, refuses without `--max-usd`), and reuses `scrape_core.targets`'s
own `resolve_effective_policy`/`assign_proxy` (the real spider's dispatch path)
for target resolution — not a probe-specific reimplementation. 29 unit tests
(`tests/unit/test_probe_amazon_report.py`) cover the report math and every
decision-table boundary. Evidence: `reports/C2.md`.

**What is still deferred, and is now its own item below:** the live run itself
(<= $1 spend) has not happened — no Amazon request has been made under this
tooling. `docs/ops/RELEASE_3_2026-09.md` §2.11 ("Decision 3", related paragraph)
carries the exact command. Owner: owner. Trigger: monthly proxy spend above
$30, or before onboarding a second Amazon-heavy catalogue — unchanged from the
original trigger, since the underlying business signal has not changed, only
the tooling to measure it now exists.

## 2026-09-08 — Run the Amazon HTTP-leg probe (spend <= $1, tooling ready)

Follow-up to the item above: `scripts/probe_amazon_http_leg.py` is built,
tested, and spend-guarded, but has never been run against real Amazon targets.
Command (from `docs/ops/RELEASE_3_2026-09.md` §2.11):

```bash
uv run python scripts/probe_amazon_http_leg.py \
  --targets 50 --runs 3 --max-usd 1.00 --report /tmp/probe-amazon-http.json
```

Owner: owner. Trigger: same window as C11's other deferred spend decisions
(Decision 3, `BROWSER_DOCUMENT_ONLY_DOMAINS`), since this probe's result feeds
that same domain's `domain_playbooks` seeding.

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

## 2026-09-05 — `proxy_bytes` excludes browser sub-resource bytes — **CLOSED 2026-09-08 (EPA A7/C6)**

The usage-export aggregation counts only network operations reachable from an
attempt row, so a browser page's child operations' bytes are excluded. Harmless
today; it understates bytes the moment `PROXY_BROWSER` calibration goes
byte-based. Owner: engine maintainer. Trigger: any move to byte-based
`PROXY_BROWSER` pricing.

**Closed by EPA plan tasks A7 and C6 (2026-09-08).** A7 added
`WireBytesMiddleware` (`libs/scrape-core/scrape_core/middlewares/wire_bytes.py`)
so every operation — parent and child alike — carries a real measured
`wire_bytes`/`bytes_compressed` figure instead of the old signal-derived total.
C6 rewrote `apps/api/app/services/admin_usage.py`'s aggregation (F17): a new
`per_op` CTE walks children via `parent_operation_id` and folds them with
`op_totals`, so `proxy_bytes` and the `proxied_browser` count are now summed
over the parent **and every child** operation, not the parent alone. Verified
live against a throwaway `postgres:18-alpine` container
(`tests/integration/test_admin_usage_fixtures.py`, 7 passed) with a before/after
comparison on the same fixture data: `OLD-RULE bytes (summed per attempt):
3,290,000` (children missing) vs the new per-op rule counting `1 proxied + 1
parent + 2 children = 4` operations correctly. Evidence: `reports/A7.md`,
`reports/C6.md` (attempt 2, "Files changed" and Verification §4).

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

## 2026-09-04 — `SCHEDULER_FAIR_QUEUE_ENABLED` left `False` — **CLOSED 2026-09-07 (EPA B3/F07)**

Single-tenant fleet in production today — weighted fair queuing
(`app_shared.scheduling.fair_queue`) has nothing to arbitrate fairly
between when there is only one workspace generating scheduler load.
Revisit at 3+ tenants. (EPA plan task B4.)
Owner: engine maintainer. Trigger: a third tenant generating scheduler load.

**Closed by EPA plan task B3 (2026-09-07): the default is now `True`**
(`libs/shared/app_shared/config.py`). The deferral's premise was that the
flag gated *fairness only*. It does not: the fair pass is also the only
scheduling path with per-rule failure isolation (a bounded-retry ledger
and a dead-letter sink instead of the legacy loop's pass-ending `break`),
the only one with the fleet/domain concurrency caps that bound what a
single merchant's WAF sees, and — as of B3 — the only one that claims a
due occurrence durably in `refresh_rule_occurrences` before creating a
job. None of those is a tenant-count question. An operator can still set
`SCHEDULER_FAIR_QUEUE_ENABLED=false` to fall back to the legacy loop,
which B3 also gave per-rule isolation, and `tests/unit/
test_w4_flag_defaults.py` still pins both the default and the override.

## 2026-09-03 — `UsageSnapshotArchive` (SaaS) not extended with the B3 proxy counters

`proxied_http_attempted`, `proxied_browser_attempted`, `proxy_bytes`
(EPA plan task B3, engine commit adding them to `GET /v1/admin/usage`)
were not backfilled into the SaaS side's `UsageSnapshotArchive` model —
archived snapshot copies of a usage row drop the three counters. Live
`/v1/admin/usage` rows are unaffected; this only affects historical
snapshots taken via the archive path. (Finding carried over from an
earlier task; recorded here per EPA plan task B4.)
Owner: SaaS maintainer. Trigger: the first time an archived usage snapshot is
read for a report or a billing dispute.

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
Owner: owner. Trigger: immediately after engine release 2 (task B6) migrates to
production.
