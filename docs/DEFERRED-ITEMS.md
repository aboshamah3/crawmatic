# Deferred Items

A running register of decisions deliberately deferred rather than made
now — each entry says what was deferred, why, and what would trigger
picking it back up. No dedicated register existed in this repo before
EPA plan task B4 (2026-09-04); the other candidates checked
(`docs/ops/*`, root `PLAN_*.md`, `GAP_ANALYSIS.md`) are point-in-time
plans/runbooks that happen to use the word "deferred," not a durable
list, so this file was created fresh at `docs/DEFERRED-ITEMS.md`.

Newest entries first.

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
