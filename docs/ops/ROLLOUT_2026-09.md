# Production rollout — 1 → 5 → 20 → 100 stores (Task D4, Stage D)

**Status: OWNER GATE — NOT STARTED.** EPA wrote this runbook and every command below and
executed **none** of them. Per `.epa/plan-core-production-readiness-2026-09-07/ASSUMPTIONS.md`
answer 2, plan D4 **Step 2** (execute the steps, record each gate) is an owner gate. This document
is the program, not a claim that the rollout has run.

This is the production counterpart to `docs/ops/STAGING_ENVIRONMENT.md`/`docs/ops/FLEET_TEST_2026-09.md`
(the synthetic 100-store fixture-origin test, Task D1) and `docs/ops/FAULT_INJECTION_2026-09.md`
(Task D2). Those exercises certify the platform against controlled load *before* any real store is
exposed to it. This document is what happens after that certification, against **real stores and
real refresh rules**, expanded in four bounded steps so a bad interaction with a real domain mix is
caught at 1 or 5 stores, never discovered for the first time at 100.

---

## §0. Precondition — the staging environment is gone before Step 1

**The temporary Railway staging environment (`docs/ops/STAGING_ENVIRONMENT.md`) must be deleted
after D2 sign-off, before Step 1 of this rollout runs.** It restored a copy of production data and
held it on infrastructure that exists only for the synthetic test; carrying it into the production
rollout window is a standing liability with no purpose once D1/D2 are certified. Confirm the
teardown checklist at the bottom of `STAGING_ENVIRONMENT.md` §6 is fully checked off — in
particular that `docs/ops/FLEET_TEST_2026-09.md` and `docs/ops/FAULT_INJECTION_2026-09.md` have
their measurement rows filled in — before enabling a single production refresh rule under this
document.

```bash
railway environment delete staging          # if not already done under D1/D2 sign-off
railway volumes --environment staging       # expect: nothing
```

---

## §1. The shape: 1 → 5 → 20 → 100, jittered, seven daily cycles per step

At each step the owner enables refresh rules for **N** stores (1, then 5, then 20, then 100),
**jittered** — `next_run_at` staggered across the day rather than all stores due at once, the same
discipline D1's synthetic seeder used (864 s / 14.4 min apart at 100 stores; scale the stagger
interval down as N shrinks so the N stores still spread across a 24 h window, e.g.
`interval = 86400 / N` seconds, capped at a sane minimum). Enabling refresh rules is a per-workspace
`refresh_rules.enabled = true` flip (or the initial creation of the rule) through whatever the
existing per-workspace onboarding tooling is — this document does not invent a new enablement path.

After enabling N stores, **wait seven consecutive daily cycles** and read the D5 scorecard
(`GET /admin/scorecard?days=7`, or `read_scorecard_range` directly) for that window before deciding
whether to advance, hold, or stop.

```bash
# Read the last 7 scorecard days for the current step's window (owner, from the engine host or an
# operator machine with API access — this reads, it does not spend or mutate anything):
curl -sS -H "Authorization: Bearer <SERVICE_TOKEN>" \
  "https://<api-host>/admin/scorecard?days=7" | jq .
```

---

## §2. The ADVANCE bar — stated verbatim and completely (audit §13 / owner decision 10)

Advance to the next N **only if every one of these holds for all seven days of the current step's
window**:

> terminal ≥ 99% within 24 h; fresh comparable ≥ 95%; attempts per valid fresh ≤ 1.5; cost per
> valid refresh ≤ the C11-approved bound; no B9 alert unresolved > 24 h.

Spelled out, with the exact source of each number:

1. **Terminal ≥ 99% within 24 h.** At least 99% of eligible matches reach a valid terminal outcome
   (a real fetch outcome — priced, not-listed, permanently unavailable — not a pending/errored
   state) within 24 hours of being due. Not-listed and permanently unavailable listings are
   excluded from the eligible-match denominator explicitly, per owner decision 10 — a store that is
   genuinely delisted does not count against the bar. Source: `fleet_daily_scorecard.terminal_fraction_24h`
   (Task D5), scoped to the workspaces in the current step.
2. **Fresh comparable ≥ 95%.** Of the eligible matches, at least 95% obtain an actual fresh
   comparable price (not merely a terminal outcome — a priced result usable for the customer-facing
   comparison), per owner decision 10. Source: `fleet_daily_scorecard`'s fresh-comparable fraction
   for the window.
3. **Attempts per valid fresh ≤ 1.5.** Retry amplification stays bounded — no more than 1.5 physical
   attempts on average per match that lands a valid fresh price. Source: `fleet_daily_scorecard`
   (attempts / valid-fresh ratio) for the window.
4. **Cost per valid refresh ≤ the C11-approved bound.** **As of this writing, `docs/ops/RELEASE_3_2026-09.md`
   §2.11 records three owner decisions (retention ratification, `EXTRACTION_RANKING_POLICY` flip,
   `BROWSER_DOCUMENT_ONLY_DOMAINS`) and none of them states a dollar-denominated cost-per-valid-refresh
   ceiling.** The audit's own gate table (§13, "Economics") asks for "cost per valid refresh ... within
   approved bounds" without naming the number, and no task in this plan set one. **This is a real gap,
   not a placeholder to fill in mechanically**: before Step 1 of this rollout can honestly evaluate
   condition 4, the owner must set the bound — via a C11 addendum or a dedicated decision — informed
   by `docs/ops/COST_MODEL_2026-09.md`'s calibrated per-request cost table (Task D3) and the D3 canary
   reconciliation once it has run. Until that number exists, condition 4 cannot be marked PASS or FAIL
   from evidence; treat it as unmeasurable and do not advance past Step 1 on the strength of the other
   four conditions alone. Source once set: `scripts/reconcile_canary_costs.py` (Task D3) reconciliation
   for the window, or the scorecard's cost columns once D3's canary has calibrated them.
5. **No B9 alert unresolved > 24 h.** Every heartbeat/freshness/queue-age/persistence-backlog/
   budget-denial/disk/restore-failure alert (Task B9, `docs/ops/OBSERVABILITY_SLO_AND_ALERTS.md`)
   raised during the window was resolved within 24 hours of firing. Source: the alerting system's
   own resolution log — this runbook does not re-derive alert state.

All five must hold for **all seven days**, not on average across the window — a single bad day
below the bar in any of the five dimensions means the step has not passed and the STOP rule (§3)
governs instead.

---

## §3. The STOP rule — stated verbatim and completely

> any day below 95% terminal, any persistence quarantine, any budget denial, or a freshness alert →
> hold at the current N and open an incident per `docs/INCIDENT_RESPONSE.md`.

Spelled out:

1. **Any single day's terminal fraction falls below 95%** (note: this is a lower, harder floor than
   the 99% *advance* bar — it is the point at which the step is not merely "not yet ready to
   advance" but actively regressing).
2. **Any persistence quarantine** — a scrape result held back by the persistence layer's own
   integrity check rather than written through.
3. **Any budget denial** — a fleet or per-workspace cost-authorization denial for reasons other than
   the expected DIRECT/zero-cost boundary (i.e. a paid rung refused because a budget was exhausted).
4. **A freshness alert** — B9's freshness-lag alert fires for any workspace in the current step.

Any one of these four conditions, on any single day: **hold at the current N** — do not advance, and
do not roll back the stores already enabled unless the incident investigation calls for it — and
**open an incident per `docs/INCIDENT_RESPONSE.md`**, using that document's severity table to
classify it (a persistence quarantine or a widespread terminal-fraction miss is very plausibly a
Sev1/Sev2 by that document's own definitions; a single-workspace freshness alert may be lower).
Resume the rollout attempt only after the incident is resolved and a fresh seven-day window at the
current N clears the ADVANCE bar in §2.

---

## §4. Per-step gate table

Fill one row per step as it completes. A step is not "done" until every ADVANCE-bar column is
recorded for all seven days (or the STOP rule fired and the Result column says so). **A blank cell
means not yet measured — never infer a PASS.**

| Step | N stores | Window (7 days) | Terminal ≥99%/24h | Fresh comparable ≥95% | Attempts/valid-fresh ≤1.5 | Cost/valid-refresh ≤ C11 bound | B9 alerts <24h resolved | Result | Incident (if STOP) |
|---|---:|---|---|---|---|---|---|---|---|
| 1 | 1 | | | | | | | pending owner | — |
| 2 | 5 | | | | | | | pending owner | — |
| 3 | 20 | | | | | | | pending owner | — |
| 4 | 100 | | | | | | | pending owner | — |

`Result` is one of: `ADVANCE` (all five bars cleared for all seven days), `HOLD` (STOP rule fired;
see the Incident column), or `pending owner` (not yet run). No row in this table may be marked
`ADVANCE` on the strength of a partial window or an estimate — the scorecard for all seven days
must exist and clear the bar.

---

## §5. Exact commands per step (owner)

For step N (1, 5, 20, or 100 stores), in order:

```bash
# 1. Confirm the staging environment is gone (§0) — first step only, but safe to re-check every time.
railway environment delete staging 2>/dev/null || true   # no-op if already deleted
railway volumes --environment staging                    # expect: nothing

# 2. Enable refresh rules for the next N stores (jittered), via the existing per-workspace
#    onboarding path. This document does not create a new enablement script; whatever process
#    turns on a workspace's daily refresh_rules row today is the one to use here. Stagger new
#    rules' next_run_at across a 24h window (interval ~= 86400 / N seconds) rather than enabling
#    them all with next_run_at = now.

# 3. Wait seven consecutive daily cycles. Nothing to run — this is elapsed time.

# 4. Read the D5 scorecard for the window, one call per day or one ranged call at the end:
curl -sS -H "Authorization: Bearer <SERVICE_TOKEN>" \
  "https://<api-host>/admin/scorecard?days=7" | jq .

# 5. Read the D3 cost reconciliation for the same window (cost-per-valid-refresh bound):
sudo -u mahmoud .venv/bin/python scripts/reconcile_canary_costs.py \
  --job-id <window-job-id> --provider-window <window> --max-usd 0   # read-only reconciliation;
  # --max-usd 0 because this call spends nothing — it reads network_operations /
  # provider_usage_records for the window already run, it does not launch a new canary.

# 6. Check B9 alert resolution state for the window (owner's alerting system, e.g. the paging
#    tool's own resolved-within log) — not reproduced here, see docs/ops/OBSERVABILITY_SLO_AND_ALERTS.md.

# 7. Fill the §4 gate table row for this step from 4-6. If every ADVANCE-bar column clears for all
#    seven days -> proceed to step N+1. If the STOP rule (§3) fired on any day -> hold at N and
#    open an incident per docs/INCIDENT_RESPONSE.md.
```

---

## §6. What this document deliberately does not do

* It does not invent a refresh-rule enablement script — the plan's Step 1 (this document) is
  documentation only; Step 2 (execution) is the owner's, using whatever tooling already exists for
  turning on a workspace's refresh rule.
* It does not restate the C11-approved cost bound as a number — that number lives in
  `docs/ops/RELEASE_3_2026-09.md` §2.11 and is read from there so this document cannot go stale
  against it.
* It does not authorize skipping a step (e.g. going 1 → 20 directly) — the whole point of a staged
  rollout is that each step's real-domain mix is a genuinely new sample the synthetic fixture test
  (D1) and the fault-injection matrix (D2) could not have produced.

## §7. Step 2 — OWNER GATE, prepare only

**Step 2 (execute the steps; record each gate) is deferred.** No refresh rule has been enabled, no
production store has been added to this rollout, and no Railway variable was changed by this task.
The exact commands are in §5; the gate table in §4 carries `pending owner` in every row until the
owner runs them.
