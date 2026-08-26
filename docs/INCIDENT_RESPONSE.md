# Incident Response

EPA task W5.5-GA, item D (report §10). **Status: DRAFT.** Paging contacts
are placeholders (`OWNER` — names are the owner's to fill in, never
invented here). The outage-exercise matrix (§8) lists every exercise as
**PENDING** — none has been run. This document is the program, not a
claim that the program has been exercised.

---

## 1. Severity definitions

Concrete examples are drawn from mechanisms that actually exist in this
codebase — cited by file, not hypothetical — so a responder can match a
real symptom to a real severity without guessing.

### Sev1 — active harm, data integrity, or tenant-isolation breach

Immediate page, all hands, no waiting for a scheduled check-in.

* **RLS breach** — a query returns another workspace's rows. The system
  that should have prevented this: `crawmatic_app` provisioned
  `NOSUPERUSER`/`NOBYPASSRLS` (`scripts/provision_db_roles.py`), FORCE RLS
  + >=1 policy on every relation the reviewed manifest
  (`scripts/rls_table_manifest.txt`) classifies as tenant-owned, verified
  by `tests/integration/test_tenant_isolation_roles.py` in CI's
  `tenant-isolation` job. A Sev1 here means that verification chain
  itself failed to catch a real gap — see `--verify`'s FAIL/WARN output
  format (`READY_REGISTER.md` READY-007/013) for what the diagnostic
  looks like when it IS working.
* **Double-billing** — a tenant or the fleet is charged twice for the
  same physical operation. The mechanism that should prevent this:
  `network_operations`/`network_operation_settlements` are DB-
  trigger-enforced append-only (`libs/shared/app_shared/models/
  network_operations.py` — `trg_network_operation_settlements_append_only`
  rejects both UPDATE and DELETE; a closed operation's own
  `BEFORE UPDATE` trigger rejects further writes), and C5's
  `reconcile_window` (`libs/shared/app_shared/netledger/reconcile.py`)
  appends a NEW `settlement_version` rather than mutating on a re-import
  — a genuine double-charge means either that trigger was bypassed
  (an owner-authorized exception used incorrectly — see
  `docs/RETENTION_POLICY.md` §2.6.a) or a bug upstream of the ledger
  wrote two operations for one physical fetch.
* **Runaway domain spend** — spend on a domain or by a workspace
  continues past its authorized budget. The mechanism that should
  prevent this: `libs/shared/app_shared.costauth.service
  .CostAuthorizationService` — **the single gate every paid dispatch
  passes**, six ordered fail-closed gates (entitlement, breaker
  freshness, domain certification, concurrency cap, dedupe, then the
  four budget dimensions), one DB transaction, `SELECT ... FOR UPDATE`
  on the budget rows, no Redis/DB dual-write for correctness (`service.py`'s
  own module docstring). A Sev1 runaway-spend incident means this gate
  was bypassed, mis-configured, or a caller dispatched without ever
  calling it — the **budget kill-switch is this gate's deny-all path**:
  any of the six gates raising denies the ENTIRE reservation, at the
  fundamental-and-cheapest gate first (entitlement), and nothing spends
  without a reservation existing first (`service.py`: *"Nothing is
  reserved on any denial path"*).

### Sev2 — degraded but contained

Page during business hours; overnight page only if trending toward Sev1.

* **Stuck job** — a `scrape_jobs` row remains RUNNING well past its
  expected completion with no forward progress. Related, existing
  mechanisms: the cancellation fence (`libs/shared/app_shared/jobs/
  cancellation.py::cancel_and_reconcile_job`, migration `c1d7a4e9b350`)
  and the dispatch-identity/reconciliation machinery (`libs/shared/
  app_shared/scrapyd/reconcile.py::reconcile_inflight`, wired into
  `ScrapydDispatchClient.schedule()` step 0b). Stall-detection itself
  (`tests/unit/test_jobs_stall_recovery.py`) is **fenced for this task**
  (another worker's active lane) — cited here for the runbook index,
  not re-verified.
* **Bad parser release** — a deployed extraction-strategy change causes
  a spike in `NOT_LISTED`/false-negative observations on a certified
  domain. The 2026-08-24 production incident this exact shape already
  happened once: S-Tech (`stech.ink`) misread a product identifier and
  emitted false terminal `NOT_LISTED` for 26 of 30 canary targets
  (`READY-003`, `PRODUCTION_READINESS_REPORT_2026-08-24.md`), fixed by
  typed identifier resolution (`libs/scrape-core/scrape_core/adapters/
  shopify.py::resolve_shopify_variant`, migration `a3f5c81d7e46`) and
  guarded going forward by the domain-certification gate (`tests/
  integration/test_domain_certification.py`, zero-`xfail`-marker
  enforcement) — `scrape_core/extraction/` is **fenced for this task**;
  cited, not touched.
* **Provider outage** (proxy/DataImpulse-shaped) — the fleet's upstream
  transport provider degrades or goes dark. Existing mechanism: the
  proxy circuit breaker (`proxy_circuit_breakers`, gate 2 of C3's six —
  see Sev1 runaway-spend above) fails closed on stale or missing
  evidence, and is consulted for **every** transport including DIRECT,
  specifically so a fleet whose brake has stopped reporting does not
  start new work of any kind.

### Sev3 — minor, no tenant impact, or fully mitigated by an existing control

Next business day.

* A single-workspace budget denial working as designed (a gate in §Sev1's
  six correctly said no).
* A partition-drop skipped pending rollup coverage
  (`partitions_skipped_pending_rollups`,
  `libs/shared/app_shared/maintenance/retention.py`) — the self-healing
  path working, not a failure.
* An individual dead-lettered scheduler item
  (`app_shared.scheduling.fair_queue.RetryLedger`) — isolated by design,
  replayable (`RetryLedger.replay`/`replay_all`), does not block other
  tenants.

---

## 2. Paging routes

**Placeholder — owner to fill in real contacts and an actual paging tool
(PagerDuty/Opsgenie/etc.) before this document is load-bearing.**

| Severity | Primary | Secondary | Channel |
|---|---|---|---|
| Sev1 | OWNER | OWNER | PENDING (no paging tool configured today) |
| Sev2 | OWNER | — | PENDING |
| Sev3 | OWNER (async) | — | PENDING |

No on-call rotation, paging tool, or status page exists today. This
document names the gap rather than inventing a rotation nobody has
agreed to.

---

## 3. Runbook index

Every entry below is a REAL, already-shipped artifact — cited by path,
not a placeholder for one this task invented.

| Runbook | Path | Covers |
|---|---|---|
| Release identity / deploy | `/srv/crawmatic/evidence/A5_DEPLOY_RUNBOOK.md` | Baking release identity into an image, `/version` verification, deployment attestation. **Status per that runbook: PREPARED, NOT EXECUTED** — steps 1-5 (code+tests) complete, steps 6-8 (bake/deploy/verify/attest) require the owner to run them; no staging environment exists. |
| Disaster recovery / restore | `scripts/dr/RUNBOOK.md` (+ `scripts/dr/backup_prod.sh`, `verify_restore.sh`, `dr_lib.sh`, `install_schedule.sh`) | Backup schedule, restore-drill procedure, the honest RPO (4h, not the 15m objective) / RTO (met, ~66s measured, two orders of magnitude of headroom) statement. Drill transcripts: `/srv/crawmatic/evidence/restore-drill-2026-08-25.md`, `w54-drill-1-2026-08-25.txt`, `w54-drill-2-2026-08-25.txt`. |
| DB role provisioning | `scripts/provision_db_roles.py` (`--provision` / `--verify` / `--check-deployment-config`) | Provisioning `crawmatic_app`/`crawmatic_auth`/`crawmatic_migrate` from zero, and verifying role attributes + RLS posture against the reviewed manifest (`scripts/rls_table_manifest.txt`). Run live in CI's `tenant-isolation` job (`.github/workflows/ci.yml`) against a from-scratch database every push/PR. |
| Cost / budget kill-switch | `libs/shared/app_shared/costauth/service.py` | The six-gate deny-all authorization path (Sev1 runaway-spend, above). **Fenced for this task** — cited, not modified. |
| Credential rotation | `/srv/crawmatic/evidence/OWNER_RUNBOOK_credential_rotation.md` | Owner-actioned credential rotation procedure (referenced by `READY_REGISTER.md`'s secret-custody section — two known outstanding rotation items as of 2026-08-25: `NEON_API_KEY` in bash history, `x-mush-key` in a plaintext doc). |
| Secret scanning | `.gitleaks.toml` + CI `security` job (engine/SaaS/plugin `.github/workflows/*.yml`) | Continuous gitleaks scan on every push/PR; `/srv/crawmatic/evidence/secret-scan-2026-08-25.md`, `gitleaks-reports-2026-08-25/`. |
| Retention program | `docs/RETENTION_POLICY.md` (this task, item C) | What gets deleted, when, and by what mechanism — including which tables cannot be row-deleted at all (append-only-by-trigger). |

---

## 4. Incident roles

Minimal set, sized for the team this system actually has today (no
dedicated on-call org exists — see §2):

* **Incident Commander (IC)** — owns the decision to declare/de-escalate
  severity, coordinates response, is the single voice on status comms.
  Does not necessarily fix anything themselves.
* **Responder(s)** — investigate and mitigate. For a Sev1 RLS/billing/
  spend incident, a responder MUST be someone who can read (not
  necessarily edit) `costauth/`, `netledger/`, or the RLS provisioning
  scripts named in §3 — the mechanisms are dense enough that
  first-response-by-someone-unfamiliar risks a worse mitigation than no
  mitigation.
* **Scribe** — timestamps decisions and actions as they happen, feeds
  the post-incident review (§7). Can be the IC for a Sev3; should not be
  for a Sev1 (the IC is too busy deciding to also be transcribing).

For a team of the current size, IC/Responder/Scribe may collapse to one
or two people — the roles are named so a growing team knows what to split
apart first, not to mandate three humans on a Sev3.

---

## 5. Status-comms template

```
[SEV{1,2,3}] {short title} — {OPEN|MONITORING|MITIGATED|RESOLVED}

What's happening: {one or two sentences, plain language, no internal
  jargon a merchant/support agent wouldn't recognise}

Impact: {which tenants/workspaces, or "fleet-wide", or "internal only,
  no tenant impact"}

Current status: {what's been done, what's being tried next}

Next update: {time or "on change"}
```

Sev1 updates: every 30 minutes or on material change, whichever is
sooner. Sev2: hourly. Sev3: no live comms required — the post-incident
review is the record.

---

## 6. Post-incident review template

Filled in after mitigation, before closing:

```
# Post-incident review — {date} — {short title}

Severity: Sev{1,2,3}
Duration: {detection time} -> {mitigation time} -> {resolution time}
Detected by: {alert name, or "reported by X", or "found during Y"}

## Timeline
{scribe's timestamped log from §5, condensed}

## Root cause
{the actual mechanism that failed — cite the file/gate/trigger by name,
  same discipline this document uses throughout: a root cause is a real
  code path, not a vibe}

## What worked
{which existing mechanism (§1's examples, §3's runbooks) DID catch or
  contain this, even if something else let it start}

## What didn't
{gap named plainly — "no mechanism existed for X" is an acceptable,
  honest answer; do not paper over a gap with a vague action item}

## Action items
| Item | Owner | Due | Status |
|---|---|---|---|

## Was the severity classification (§1) correct in hindsight?
{yes/no — feeds back into keeping §1's examples accurate over time}
```

---

## 7. SLO definitions

**Tracked by release manifest**, not by a separate uptime dashboard that
could drift from what was actually deployed — this is the direct
consequence of task A5's release-identity work
(`/srv/crawmatic/evidence/A5_DEPLOY_RUNBOOK.md`,
`libs/shared/app_shared/release.py`): `/version` reports `manifest_id`,
`source_digest`, `image_digest`, `expected_db_migration` vs.
`live_db_migration`, and `/ready`'s `checks.migrations` is **fail-closed**
on a mismatch (`apps/api/app/routers/ready.py` — 503, error
`MigrationHeadMismatch`, publishes no revision ids on mismatch) and
`checks.heartbeats` aggregates per-instance worker/scheduler freshness
(`app_shared.heartbeat`). An SLO claim ("the API was up") is only as
credible as the release manifest it's measured against being the one
actually running — which is exactly what A5 makes checkable rather than
asserted.

| SLO | Target | Measured by | Status |
|---|---|---|---|
| API availability | PENDING OWNER (no number set) | `/ready` (`checks.migrations`, `checks.heartbeats`, other dependency checks — `apps/api/app/routers/ready.py`) | Mechanism exists; target number not set |
| RPO | Owner-approved objective: 15 min | `scripts/dr/RUNBOOK.md` | **NOT MET** — measured 4h (cron cadence), honestly stated in the runbook itself |
| RTO | Owner-approved objective: 2h | `scripts/dr/RUNBOOK.md` | **MET** with ~2 orders of magnitude headroom (data-restore only; full service reprovisioning not measured) |
| Release identity accuracy | `/version` must report the truth or nothing | `apps/api/app/routers/version.py`, `env_mismatches` field | Implemented; not yet certified live (A5 steps 6-8 not executed) |

No error-budget policy exists. PENDING OWNER.

---

## 8. Outage-exercise matrix

**Every row is PENDING — no drill in this matrix has been run.** Listed
here as the exercise plan, not as evidence of readiness.

| Exercise | Trigger simulation idea | Expected behavior (real mechanism cited) | Drill status |
|---|---|---|---|
| Provider outage | Point the proxy breaker's evidence source at a stale/frozen feed (or block the provider's IPs from the scratch environment) and observe dispatch. | C3 gate 2 (breaker freshness) denies with `BREAKER_EVIDENCE_STALE`; an OPEN breaker denies with `BREAKER_OPEN`; consulted for every transport including DIRECT (`costauth/service.py`). | PENDING |
| Credential revocation | Rotate/revoke a live provider or DB credential in a scratch environment and observe. | `/srv/crawmatic/evidence/OWNER_RUNBOOK_credential_rotation.md` (owner-actioned rotation procedure); no automated detection-and-alert on a revoked credential was found in this review — likely surfaces as ordinary connection failures rather than a named "credential revoked" signal. **This gap itself is a finding**, not just an unrun drill. | PENDING |
| DB failover | Kill the primary connection mid-transaction against the scratch/restore-drill database; observe app-level reconnect and RLS/session-GUC re-establishment (`workspace_context` in `maintenance/scoping.py` explicitly refuses to enter on a session already mid-transaction — relevant to what "clean failover" must mean here). | Restore path is proven (`scripts/dr/verify_restore.sh`, drill transcripts in §3); live failover of an IN-SERVICE connection pool is a distinct, unmeasured scenario from cold restore. | PENDING |
| Bad parser release | Deploy a deliberately-broken extraction strategy against the certification fixture set and confirm it is CAUGHT before shipping, not after. | `tests/integration/test_domain_certification.py`'s zero-`xfail`-marker guard (`test_gate_b_certified_amazon_css_has_zero_xfail_markers`) — a method is certified only when its fixture pass rate meets its profile threshold. This is a pre-deploy gate, not a live-outage drill; the 2026-08-24 S-Tech incident (§1, Sev2) is the real-world case this gate now exists to catch going forward. | PENDING (gate exists; a live "did it actually block a bad release" drill has not been run) |
| Runaway domain spend | Attempt to dispatch past a workspace's or the fleet's budget ceiling in a scratch environment; confirm C3 denies before any spend, not after. | C3's six-gate deny-all (`costauth/service.py`) — "nothing is reserved on any denial path." | PENDING |
| Stripe outage | Point the SaaS side's Stripe client at an unreachable/erroring endpoint (mocked — per `saas-wt-w51`'s own binding operating constraint, *"Stripe mocked behind the seam"*) and observe billing-flow behavior. | Out of this repo (engine) — the mechanism lives in `saas-wt-w51`; this row is recorded here because the exercise matrix should be complete even where the mechanism is cross-repo. Not investigated further by this task (engine-repo-scoped). | PENDING |

---

## 9. Honest gaps, stated plainly

* No paging tool, on-call rotation, or status page exists (§2).
* No log-retention policy exists (`docs/RETENTION_POLICY.md` §2.8) —
  relevant to incident forensics: a Sev1 investigated a week after the
  fact may find the logs it needs are already gone, with no documented
  window either way.
* No credential-revocation detection/alerting mechanism was found (§8) —
  only a manual rotation runbook.
* Zero drills in §8 have been run. This document is the plan; running it
  is future work the owner must schedule.
