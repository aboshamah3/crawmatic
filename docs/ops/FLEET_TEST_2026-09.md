# Fleet test — 100 stores x 5,000 products, controlled origin (Task D1, Stage D)

**Status: SKELETON — NOT RUN.** Every measurement cell below is empty on purpose. The run needs
the temporary Railway staging environment of `docs/ops/STAGING_ENVIRONMENT.md`, which is an
**owner gate** (plan D1 step 2) and was not created; plan D1 **steps 3 and 4 are therefore
DEFERRED**. Nothing in this document has been executed against Railway, and no money was spent.

What EPA did ship, and what is proven, is in §1. What still has to be run, and the exact command
for each row, is in §3–§6. **Do not fill a cell from an estimate.** A blank cell and
`scripts/fleet_test_report.py`'s `NO DATA` verdict mean the same thing, and both are honest; a
guessed number in a release-gate table is not.

---

## §1. What exists now (EPA, done)

| Artifact | What it is |
|---|---|
| `apps/fixture-origin/` | FastAPI controlled origin. Generates 500,000 SKUs x N stores from the request path — no data file. Per-store latency / error / redirect / blocked / not-listed profiles, deterministic per `(store, sku)`. HTML page (JSON-LD) and a JSON endpoint variant. **Makes no external calls** (asserted structurally from its own AST in `tests/unit/test_fixture_origin_pages.py`). |
| `scripts/seed_synthetic_fleet.py` | Seeds 100 workspaces x 5,000 products x 1.185 matches = **592,500 matches** at fixture-origin URLs, one daily `WORKSPACE` refresh rule per workspace staggered **864 s (14.4 min)** apart (audit §10), plus one `domain_rules` row so the fleet is not capped at 6 concurrent requests against the single fixture host. Refuses any non-staging target. |
| `scripts/fleet_test_report.py` | Reads the D5 scorecard (`fleet_daily_scorecard`) and the A5 baseline gauges for a window, plus a measurements file, and emits the audit §13 gate table with a verdict per row. **A missing measurement is `NO DATA`, never `PASS`** (exit 3 = incomplete, 1 = a gate failed). |
| `docs/ops/STAGING_ENVIRONMENT.md` | Create / tear-down runbook for the temporary environment. |
| Local smoke (below, §2) | 2 stores x 50 products against the fixture origin on a throwaway Postgres — proves the seeder → origin → report path end to end at small scale. |

## §2. Local smoke — RUN, and green

Small-scale proof that the pieces fit together, run on the build host against a throwaway
`postgres:17.5-bookworm` container on a random loopback port (torn down afterwards) and a local
`uvicorn` fixture origin. **This is not the fleet test** — it is 100 products, not 500,000, and it
exercises no scrapyd node, no Celery worker and no proxy.

| Step | Result |
|---|---|
| `alembic upgrade head` on an empty DB | 3.5 s, head `f6b28c714a93` |
| Seed 2 x 50 | 2 workspaces, 100 products, 100 variants, **118 matches**, 2 refresh rules, 1 domain rule, 0.3 s |
| Match count arithmetic | `round(50 x 1.185) = 59` per store, 118 total — matches the 592,500 the full fleet produces |
| Refresh-rule stagger | `fleet-000` 00:00:00Z, `fleet-001` 00:14:24Z — exactly 864 s |
| Every seeded URL fetched from the fixture origin | 118/118 answered: **101 `200` with a price, 17 `404 not_listed`** (`fleet-000` is the `thin_catalogue` archetype at a 25% not-listed rate) |
| Determinism | Bodies byte-identical on refetch |
| Idempotent re-seed | Second run wrote 0 rows, reported `skipped_existing: [fleet-000, fleet-001]`, match count unchanged at 118 |
| `scripts/fleet_test_report.py` against that DB | 12 rows, all `NO DATA`, exit 3 — correct: nothing had been measured |

### 2.1 The C7 rollup benchmark — RUN AT FULL 500,000 SCALE, and green

`tests/benchmarks/test_rollup_500k.py` (written by C7, never run until now) was run on the same
throwaway Postgres. **This is the plan's "rollup duration for 500,000 variants" figure, but it is
NOT the staging measurement**: it is one workspace, one day, on a RAM-backed local
`postgres:17.5-bookworm` (production is 18.x), with none of the fleet's other traffic in flight.
Re-run it in staging against the restored database before filling §4's `rollup_seconds` cell.

| Scale | Batches | Statements | Rollup wall clock |
|---|---:|---:|---:|
| 2,000 variants x 50 obs = **100,000** observations | 5 | 12 | **0.6 s** |
| 5,000 variants x 50 obs = **250,000** observations | 2 | 6 | **1.2 s** |
| 5,000 variants x 100 obs = **500,000** observations (the target) | 2 | 6 | **2.3 s** |

Bar: **< 1,800 s**. The N+1 guard holds — 6 statements for 5,000 variants, i.e. cost scales with
BATCHES, not with variants.

**Two defects in the benchmark were found by running it, and fixed** (it had never executed):

1. `variant_ids` collected `variant.id` straight after `session.add()`, before the INSERT that
   evaluates the `default=new_uuid7` column default — so the list was 5,000 `None`s and the first
   seeding statement failed with `invalid input syntax for type uuid: "None"`.
2. The observation seed named `created_at`/`updated_at`, which `price_observations` does not have
   (it is the one workspace-scoped model that deliberately omits `TimestampMixin`; it is immutable
   and partitioned by `scraped_at`).

### 2.2 Finding: the rollup is catastrophically plan-sensitive to STALE STATISTICS

**This matters for the staging run and is not a defect in the rollup SQL.** On the first attempt,
one batch of 500 variants over 100,000 freshly bulk-loaded observations **had not completed after
18 minutes** at 100% CPU. The same statement, on the same data, after `ANALYZE`, takes **0.19 s**.
Measured during the hang:

* the `driver` CTE alone: **131 ms**;
* `driver` + `latest_per_match` + the aggregate: **175 ms**;
* the whole statement in `dry_run` form (identical CTEs, no `INSERT`): **0.14 s**.

So nothing in the statement is inherently slow; the planner had chosen a nested loop from
statistics that still described the partition as empty, because autoanalyze had not caught up with
the bulk load. `ANALYZE price_observations` was added to the benchmark's seeding step.

**Operational consequence for `docs/ops/STAGING_ENVIRONMENT.md`:** after the restore in §2 and
after the seeder in §5, run `ANALYZE` (or `VACUUM ANALYZE`) before the first scheduled rollup.
A freshly restored/seeded database is exactly the stale-statistics case, and a rollup that appears
to hang there will be misread as a capacity failure.

## §3. Steps 3 and 4 — DEFERRED (need the staging environment)

Run in this order, once `docs/ops/STAGING_ENVIRONMENT.md` §1–§4 are done.

```bash
cd /srv/crawmatic/crawmatic

# 1. Seed the fleet (STAGING_ENVIRONMENT.md §5).
sudo -u mahmoud .venv/bin/python scripts/seed_synthetic_fleet.py \
  --i-know-this-is-staging \
  --database-url "$STAGING_DSN" \
  --origin-base-url https://<fixture-origin-public-domain> \
  --stores 100 --products-per-store 5000 \
  --start-at <YYYY-MM-DD>T00:00:00+00:00

# 2. One full daily cycle: let the scheduler fire all 100 staggered rules and
#    wait 24 h from the FIRST rule's next_run_at. Nothing to run — this is
#    elapsed time. Capture the window:
#      SINCE=<first rule date>  UNTIL=<SINCE + 1 day>

# 3. A 2x offered-load hour: a SECOND daily rule per workspace, offset half a
#    stagger (432 s) so the two waves interleave rather than collide.
sudo -u mahmoud .venv/bin/python scripts/seed_synthetic_fleet.py \
  --i-know-this-is-staging --database-url "$STAGING_DSN" \
  --origin-base-url https://<fixture-origin-public-domain> \
  --stores 100 --products-per-store 5000 \
  --slug-prefix fleet --start-at <same day>T00:07:12+00:00   # refuses: slugs exist
#   -> the second rule is added by hand instead (the seeder is idempotent by
#      slug and will not double-seed a catalogue):
#      INSERT INTO refresh_rules (...) SELECT ... FROM refresh_rules
#      WHERE name = 'fleet-test daily refresh';   -- next_run_at + 432 s
```

The 2x wave is deliberately a **second rule**, not a halved interval: audit §13's headroom row asks
for "approximately 2x normal offered load for a bounded interval", and doubling the rule count for
one cycle is the shape that leaves the normal cycle's numbers comparable.

## §4. Measurements to record (plan D1 step 3) — ALL EMPTY

Fill each cell, then write the same values into a JSON file for `fleet_test_report.py`
(field names in the third column).

| Measurement | Value | `measurements.json` key |
|---|---|---|
| Completion fraction within 24 h | | `terminal_fraction_24h` |
| Consecutive daily cycles completed | | `daily_cycles_completed` |
| `crawmatic_target_phase_p95_seconds{due_to_dispatch}` | | read from the DB by the script |
| `crawmatic_target_phase_p95_seconds{dispatch_to_first_network}` | | read from the DB by the script |
| `crawmatic_target_phase_p95_seconds{first_network_to_persisted}` | | read from the DB by the script |
| DB pool wait p95 (ms) | | `pool_wait_p95_ms` |
| Queue depth at window start / end | | `queue_depth_start` / `queue_depth_end` |
| Offered-load multiplier during the 2x hour | | `offered_load_multiplier` |
| Spool pending (scrapyd, per node) | | *(narrative, §6)* |
| Node loads (1 HTTP + 2 browser) | | *(narrative, §6)* |
| Rollup duration for 500,000 variants (C7 benchmark) | | `rollup_seconds` |
| Retention dry-run duration | | `retention_dry_run_seconds` |
| Retention deletions on incomplete rollup coverage | | `retention_deletions_on_incomplete_rollup` (must be 0) |
| `alembic upgrade head` duration on the restored DB | | `alembic_upgrade_seconds` |
| Backup export bytes / seconds | | `backup_export_bytes` / `backup_export_seconds` |
| Restore RPO / RTO achieved, and the agreed targets | | `restore_*_seconds` / `agreed_*_seconds` |
| D2 fault-injection verdicts (7 rows) | | `fault_injection` |
| Heartbeat process classes reporting / expected | | `heartbeat_process_classes_*` |
| Security evidence (test suites) | | `security_evidence` |
| Quality evidence (labelled samples) | | `quality_evidence` |

### The pass bar (plan D1 step 3, audit §13)

| Bar | Value | Threshold key |
|---|---|---|
| Terminal within 24 h | **>= 99%** | `terminal_fraction_min` |
| Queue | **no unbounded queue** — depth at window end must not exceed depth at start | *(structural)* |
| Rollup for 500,000 variants | **< 30 min** | `rollup_seconds_max` |
| DB pool wait p95 | **< 100 ms** | `pool_wait_p95_ms_max` |
| Consecutive daily cycles | **7** | `daily_cycles_min` |
| Offered load in the headroom window | **~2x** | `offered_load_multiplier_min` |

Then:

```bash
sudo -u mahmoud .venv/bin/python scripts/fleet_test_report.py \
  --database-url "$STAGING_DSN" \
  --measurements docs/ops/fleet-test-measurements.json \
  --since "$SINCE" --until "$UNTIL" --format markdown
# exit 0 = every gate PASS/MANUAL; 1 = a gate FAILED; 3 = table incomplete
```

## §5. The §13 gate table — EMPTY

Paste the script's markdown output here verbatim, with its exit status.

| Area | Verdict | Measurement |
|---|---|---|
| Daily freshness | | |
| Headroom | | |
| Tail latency | | |
| Durability | | |
| Tenant fairness | | |
| Host protection | | |
| Security | | |
| Quality | | |
| Economics | | |
| Database | | |
| Recovery | | |
| Operations | | |

`fleet_test_report.py` exit status: _____

## §6. Derived node-count formula for production (step 4) — EMPTY

Audit §10 sizes the browser fleet from *hypotheses* (4.3 s/page, 7.7 CPU-s/page, 2-page
concurrency, ~40,200 pages/day/node ideal, ~9 nodes at 50% utilisation for Amazon alone). Step 4
replaces each of those with a measurement from this run:

| Hypothesis (audit §10) | Measured here | Replaces |
|---|---|---|
| 4.3 s per browser page | | seconds/page on 2 browser nodes at the observed concurrency |
| 7.7 CPU-seconds per page | | CPU-seconds/page in the deployed container |
| ~40,200 pages/day/node ideal | | pages/day/node actually sustained |
| 50% effective utilisation | | observed utilisation at the completion bar |
| 1 HTTP node's sustained checks/day | | |

**Formula to write once the cells are filled:**

```
browser_nodes = ceil( browser_checks_per_day / (measured_pages_per_day_per_node x measured_utilisation) )
http_nodes    = ceil( http_checks_per_day    / measured_http_checks_per_day_per_node )
```

with `browser_checks_per_day` / `http_checks_per_day` taken from the real domain mix, not from the
fixture's. **State explicitly** in this section that the fixture origin's latency profile is a
model, not a retailer: the node counts derived here are a *floor* (the platform's own capacity),
and the real-domain canary (D3) plus the domain strategy work (C2–C4) are what move them.

## §7. Known caveats to state alongside any number produced here

1. **The fixture origin is one host.** All 592,500 checks/day hit a single domain, so per-domain
   fairness and hot-domain routing (F11/B4) are exercised in their *worst* configuration. Real
   traffic spreads across domains; do not read a hot-domain result here as the production case.
2. **No proxy path is exercised.** Proxy credentials are absent by design (`STAGING_ENVIRONMENT.md`
   §0), so `bytes_compressed` on the `PROXY` transport, provider reconciliation and
   `cost_model_drift_ratio` stay `NULL` for this run. The Economics gate is therefore expected to
   read `NO DATA` here and to be settled by D3's real-domain canary instead.
3. **The behaviour mix is chosen, not observed.** `PROFILE_ARCHETYPES` in
   `apps/fixture-origin/app/main.py` is a deliberate spread (fast / typical / slow / flaky /
   redirecting / no-structured-data / hostile / thin-catalogue). Completion fractions depend on it.
   Record the archetype histogram (`GET /stores`) alongside the completion number, or the number is
   not reproducible.
4. **Storage numbers scale with the browser share**, which here is whatever the strategy resolver
   chooses for a fixture domain with no playbook — almost certainly the HTTP path. Deep dive §9.1's
   per-resource ledger growth is therefore *under*-exercised by this run.
