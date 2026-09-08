# Measured cost model — 2026-09 (Task D3, Stage D)

**Status: PREPARED, MEASUREMENTS DEFERRED.** Task D3 Step 1 (the one
mixed Amazon+Noon+S-Tech job of 150 targets this document's "measured"
column depends on) is a SPEND step (≤ $3, production scrapers) and is
NOT run — ASSUMPTIONS.md answer 3 defers all remaining spend in this
plan. Every "measured" cell below is deliberately empty; filling one in
without the real canary run would be a fabricated number wearing this
document's authority.

The reconciliation tool is `scripts/reconcile_canary_costs.py`
(`--job-id --provider-window --max-usd`; refuses to run without
`--max-usd`). Its reconciliation math has fixture-based unit coverage in
`tests/unit/test_reconcile_canary_costs.py` and a `--dry-run` mode that
runs the identical math against a small passing fixture.

## Where this table comes from

`CORE_COST_PERFORMANCE_DEEP_DIVE_2026-09-07.md` §10 ("Transparent
100-store cost model") built a monthly fetch-layer cost model from
INPUT ASSUMPTIONS — modeled, not measured, numbers, stated as such
throughout that section (§10.1: "indicative allocations guided by local
measurements, not Railway-certified per-page costs"). Every stage of
Stage C since then existed to replace one input assumption with a real
measurement:

- **C2** (`scripts/probe_amazon_http_leg.py`, `docs/ops/AMAZON_STRATEGY_2026-09.md`)
  measures the real Amazon HTTP-leg success rate — the input §10.1 calls
  "one browser page per Amazon check, preceded by 1.3 proxied HTTP
  attempts" was an assumption; C2's Step 2 (spend, ≤ $1, deferred) is
  the real measurement.
- **C3** (`scripts/canary_document_only_browser.py`) measures real
  bytes/page for the document-only browser policy against §10.1's
  assumed "2.5 MB/page full, 0.3 MB/page document-only"; its Step 2
  (spend, ≤ $3, deferred per `docs/ops/RELEASE_3_2026-09.md`) is the
  real measurement.
- **C4** (`scripts/run_domain_canary.py`, `docs/ops/DOMAIN_STRATEGIES_2026-09.md`)
  measures real Noon/S-Tech match accuracy under the versioned domain
  playbook strategy; its Step 2 (spend, ≤ $2, deferred) is the real
  measurement.
- **The real catalogue** (not a script — the production `matches`
  table) is the source for matches/product, replacing §10.1's pilot
  figure ("4,372/3,690 matches/product").
- **The container** (Railway's own `CPU_USAGE`/`MEMORY_USAGE_GB`
  metrics for `scrapers-browser`, read the same way
  `scripts/railway_cost_watchdog.py` already does) is the source for
  CPU/page and memory/page, replacing §10.1's assumed "8 CPU-seconds + 3
  GB-seconds/page full; 6.5 CPU-seconds + 4 GB-seconds/page
  document-only".

D3's canary is the FIRST point in this chain where all four
measurements exist at once, for the SAME 150-target job, so this
document also carries `reconcile_canary_costs.py`'s own two independent
checks (ledger bytes vs. DataImpulse metered bytes; modeled browser CPU
vs. Railway metered CPU) as a cross-check that the assembled model
agrees with itself, not just with each input in isolation.

## The re-computed §10.2 table, sourced

| Input | §10.1 assumed value | Source once measured | Measured value |
|---|---|---|---|
| Matches/product | 4,372/3,690 (pilot) | Real catalogue (`matches` table, this canary's workspace) | *(empty — D3 Step 1 deferred)* |
| Amazon HTTP attempts/check before browser escalation | 1.3 | C2 Step 2 (`scripts/probe_amazon_http_leg.py`, ≤ $1, deferred) | *(empty)* |
| Amazon HTTP bytes/attempt | 250 KB | C2 Step 2 | *(empty)* |
| Noon bytes/attempt, attempts/check | 10 KB, 1.5 | C4 Step 2 (`scripts/run_domain_canary.py`, ≤ $2, deferred) | *(empty)* |
| Full browser bytes/page | 2.5 MB | C3 Step 2 (`scripts/canary_document_only_browser.py`, ≤ $3, deferred) | *(empty)* |
| Document-only browser bytes/page | 0.3 MB | C3 Step 2 | *(empty)* |
| Full browser CPU-seconds + GB-seconds/page | 8 + 3 | The container (Railway `CPU_USAGE`/`MEMORY_USAGE_GB`, `scrapers-browser` service, this canary's window) | *(empty)* |
| Document-only browser CPU-seconds + GB-seconds/page | 6.5 + 4 | The container | *(empty)* |
| Proxy rate | $1/GiB | `libs/shared/app_shared/costauth/pricing.py` (`PROXY_BILLING_UNIT_BYTES`, A8) — this one is a contract term, not something a canary measures | $1/GiB (unchanged; contractual) |

## §10.2 scenario table, re-run once the above is measured

The deep dive's own scenario table (browser resource policy × Amazon
browser share requiring proxy → modeled monthly fetch-layer cost) is
reproduced here with its ORIGINAL (assumption-based) values; re-running
`calculate_scenarios.py` (referenced in the deep dive, not part of this
task's scope) against the measured inputs above, once D3 Step 1 runs,
is how this table gets a second, measured column.

| Browser resource policy | Amazon browser share requiring proxy | Modeled monthly fetch-layer cost (§10.1 assumptions) | Re-computed from measured inputs |
|---|---:|---:|---:|
| Full | 0% | $1,902 | *(empty)* |
| Full | 20% | $4,186 | *(empty)* |
| Full | 100% | $13,326 | *(empty)* |
| Document-only candidate | 0% | $1,864 | *(empty)* |
| Document-only candidate | 20% | $2,138 | *(empty)* |
| Document-only candidate | 100% | $3,235 | *(empty)* |

## D3's own reconciliation (this canary's job only)

`scripts/reconcile_canary_costs.py` compares two pairs of numbers for
the single 150-target job:

| Check | Ledger/model side | Ground truth side | Tolerance | Pass bar |
|---|---|---|---|---|
| Bytes | `network_operations.bytes_compressed` sum, PROXY transport, this job | DataImpulse `provider_usage_records.total_bytes` for `--provider-window` | ±10% | `bytes_within_tolerance == True` |
| CPU | Browser-op count × `ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST` (`app_shared.costauth.pricing`) | Railway `CPU_USAGE` for `scrapers-browser`, same window | ±25% | `cpu_within_tolerance == True` |
| Drift | `cost_model_drift_ratio` = ledger bytes / provider bytes (same formula as the A5 gauge `crawmatic_cost_model_drift_ratio`, `app_shared.opsmetrics.emit.COST_MODEL_DRIFT_RATIO_SQL`) | — | — | **0.9 – 1.1** (tighter than the gauge's own ops-alerting band, 0.5–2.0 in `app_shared.opsmetrics.rules` — a controlled single-job canary has no business drifting as far as organic 24h traffic is tolerated to) |
| Spend | Ledger `estimated_cost_micro_units` sum, this job, converted to USD | `--max-usd` | — | `ledger_cost_usd <= max_usd` |

### Exact command (Step 1 — DEFERRED, not run)

```sh
cd /srv/crawmatic/crawmatic
uv run python scripts/reconcile_canary_costs.py \
    --job-id <the 150-target job's scrape_jobs.id> \
    --provider-window "<canary_start_iso>/<canary_end_iso>" \
    --database-url "$PRODUCTION_DATABASE_URL" \
    --railway-service scrapers-browser \
    --max-usd 3
```

Owner pre-condition: `scripts/import_dataimpulse_usage.py` must have
already imported the DataImpulse usage export covering
`--provider-window` (the runbook it follows is unchanged by this task).

### Result

*(empty — Step 1 deferred; the exact printed report from a real run goes
here, per `scripts/reconcile_canary_costs.py`'s `format_report` output)*

## Offline verification (run today, proves the reconciliation math)

```sh
cd /srv/crawmatic/crawmatic
sudo -u mahmoud .venv/bin/pytest tests/unit/test_reconcile_canary_costs.py -q
uv run python scripts/reconcile_canary_costs.py --dry-run --max-usd 3
```
