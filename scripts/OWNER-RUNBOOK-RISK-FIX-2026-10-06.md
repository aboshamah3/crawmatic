# Owner runbook: engine risk-review fixes (2026-10-06)

Owner-run only. Claude does not touch Railway or prod databases. Engine Railway project
`69dc4bda-0d97-4290-a82f-822ed97d3fb8` (production) lives on the `railway2` account
(`RAILWAY_TOKEN_RAILWAY2` in `/root/.railway/accounts.sh`). Run each `!` line from the Claude prompt
or a root shell. Never deploy between 19:00Z and 23:59Z (nightly refresh window); the release script
refuses to run then unless `ALLOW_NIGHTLY=1`.

## What ships

One engine release from branch `fix/risk-review-engine-2026-10-06`: deployed main `468418d`
plus `fix/security-complete-2026-10-02` (99fa939, E1-E10, H1, I7), `feat/biweekly-cadence`
(5a34a77) and the 10-06 risk fixes (breaker economics, catalog index, API read shape). Four
additive migrations, upgrade only, in this order: `a7c41e9d2b56` (live) -> `b3d9e5a17c42`
(canonical competitor domains, DATA-CHANGING, not reversible by downgrade) -> `c4e8f2a6b913`
(workspaces.external_ref) -> `d7a1f3c5e902` (refresh-token family) -> `e2b8d4f6a1c3`
(`ix_cpm_ws_created_id` on `competitor_product_matches (workspace_id, created_at, id)`, built
with `CREATE INDEX CONCURRENTLY`, a few seconds at today's ~5k matches). **The single head is
`e2b8d4f6a1c3`.** Services changed: `api`, `worker`, `scheduler`, `scrapers`, `scrapers-browser`
(the loader runs from this box).

## Release procedure (one ordered run)

### 1. Pre-checks

- `INDEX_SERVICE_TOKEN` (>= 32 chars) set on the engine `api` service AND in
  `/root/.crawmatic/index-service-token` (0600), equal values. Hardened `/version` needs it.
- Read-only prod engine DSN in `/root/.crawmatic/engine-prod-readonly-dsn` (0600) for the
  competitor-domain collision dry run (the script stops before any migration on a collision) and
  for the SQL checks below.
- Test workspace key in `/root/.crawmatic/test-workspace-key` (0600) for the post-deploy checks.
- `free -m` shows > 1.5 GB available and `df -h /` > 2 GB free; the UTC time is outside 19:00-23:59.
- Commit to ship and its single head:

      git -C /srv/crawmatic/crawmatic log --oneline -3 fix/risk-review-engine-2026-10-06
      cd /srv/crawmatic/crawmatic && uv run alembic heads      # expect: e2b8d4f6a1c3 (head)

- Breaker headroom (read-only). The hourly-ceiling breaker trips when the trailing hour of proxied
  `request_attempts` exceeds `max(PROXY_BREAKER_HOURLY_CEILING_FLOOR, 3 x p95 of the last week's hours)`.
  The busiest proxied hour of the last 14 days (`request_attempts` is partitioned monthly by
  `created_at`; the predicate prunes to the one or two partitions the 14 days touch):

      ! psql "$(cat /root/.crawmatic/engine-prod-readonly-dsn)" -Atc "SELECT date_trunc('hour', created_at AT TIME ZONE 'UTC') AS hour_utc, count(*) AS proxied FROM request_attempts WHERE access_method IN ('PROXY_HTTP','PLAYWRIGHT_PROXY') AND created_at >= now() - interval '14 days' GROUP BY 1 ORDER BY 2 DESC LIMIT 5"

  Compare the top row with **3,000** (the default floor). Under about 1,500: keep `n = 3000` in step 2.
  Between 1,500 and 3,000, or if more than a handful of Mushtryati-sized tenants will be onboarded in one
  nightly window soon: set `n` to at least 2 x the top count, rounded up to the next 500. Above 3,000
  the fleet already exceeds the floor: set `n` to at least 2 x the top count and also read the p95 row
  before shipping.

### 2. Variables (before the deploy; `--skip-deploys`, the release deploy picks them up)

No variable is required (every setting has a code default; reference table at the end). The one that
needs a decision is the ceiling floor. **Set it on every service that evaluates the breaker**: the
evaluator runs in the `worker` (`tasks_maintenance.py`) and in the spiders (`scrape_core/targets.py`,
services `scrapers` and `scrapers-browser`), whichever process wins the lease applies its OWN settings,
and the `api` reads the same setting for the ops dashboard (`opsmetrics/rules.py`). A floor raised on
only some of them makes the trip threshold depend on which process evaluated. Replace `<n>` with the
number from step 1:

    ! bash -c 'source /root/.railway/accounts.sh && cd /srv/crawmatic/crawmatic && sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH=/home/mahmoud/.nvm/versions/node/v24.18.0/bin:$PATH bash -c "for s in worker scrapers scrapers-browser api; do railway variable set PROXY_BREAKER_HOURLY_CEILING_FLOOR=<n> -s \$s -e production -p 69dc4bda-0d97-4290-a82f-822ed97d3fb8 --skip-deploys || echo FAILED \$s; done"'
    ! bash -c 'source /root/.railway/accounts.sh && cd /srv/crawmatic/crawmatic && sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH=/home/mahmoud/.nvm/versions/node/v24.18.0/bin:$PATH bash -c "for s in worker scrapers scrapers-browser api; do echo \$s; railway variable list -s \$s -e production -p 69dc4bda-0d97-4290-a82f-822ed97d3fb8 --json | grep PROXY_BREAKER_HOURLY_CEILING_FLOOR; done"'

(Same railway2 invocation as the egg command in step 4; if the inner `bash -c` quoting bites, run the
`railway variable set ... -s <service>` command by hand four times.) Optional JWT grace is offered by the
deploy script (step 3, `SET_JWT_GRACE=1`).

### 3. Deploy with the in-repo mirror script

Use `scripts/deploy-engine-risk-fix-2026-10-06.sh`, **not** `/srv/crawmatic/deploy-engine-security-2026-10-02.sh`
(that one expects head `d7a1f3c5e902` and refuses this branch's tip; it stays the security-only script).
The mirror script is the 10-02 script retargeted: branch `fix/risk-review-engine-2026-10-06`, base `468418d`
(live engine), `NEW_HEAD=e2b8d4f6a1c3`, live head `a7c41e9d2b56`, `d7a1f3c5e902` or `e2b8d4f6a1c3` accepted,
venv `/srv/crawmatic/crawmatic/.venv`. It does: preflight, collision dry run, DR backup, optional JWT grace,
`migrate` service, then api, worker, scheduler, scrapers (**plus the Scrapyd egg re-register**),
scrapers-browser, then the post-deploy checks (each prints PASS or FAIL; a failure no longer aborts the run).

    ! DEPLOY_SHA=$(git -C /srv/crawmatic/crawmatic rev-parse fix/risk-review-engine-2026-10-06) bash /srv/crawmatic/crawmatic/scripts/deploy-engine-risk-fix-2026-10-06.sh --dry-run
    ! SET_JWT_GRACE=1 DEPLOY_SHA=$(git -C /srv/crawmatic/crawmatic rev-parse fix/risk-review-engine-2026-10-06) bash /srv/crawmatic/crawmatic/scripts/deploy-engine-risk-fix-2026-10-06.sh

`SET_JWT_GRACE=1` avoids logging every engine user out at once. Resume after a failed service (migration
already applied): the same script with `ONLY_SERVICES="<remaining services>" SKIP_MIGRATE=1 SKIP_DR_BACKUP=1`
and the same `DEPLOY_SHA`. Evidence log: `/srv/crawmatic/evidence/deploy-engine-risk-fix-2026-10-06/DEPLOY-LOG.txt`.

### 4. Scrapyd egg re-register (every `scrapers` deploy wipes it)

The script runs this right after the `scrapers` deploy, and ends with **NOT DONE** and a bold
MANDATORY NEXT STEP if it failed (HTTP scraping stalls until the egg is back). Manual command, also
needed after any later `scrapers` redeploy or restart that loses the egg:

    ! bash -c 'source /root/.railway/accounts.sh && cd /srv/crawmatic/crawmatic && sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH=/home/mahmoud/.nvm/versions/node/v24.18.0/bin:$PATH railway ssh -s scrapers -e production -p 69dc4bda-0d97-4290-a82f-822ed97d3fb8 -- python /app/apps/scrapers/register_egg.py'

### 5. Migrations check (head e2b8d4f6a1c3)

- Authenticated `/version` reports the shipped `git_sha` and `db_migration_head = e2b8d4f6a1c3`;
  bearer-less `/version` returns only `{"status":"ok"}` (the script checks both).
- The new index built valid (read-only DSN from step 1):

      ! psql "$(cat /root/.crawmatic/engine-prod-readonly-dsn)" -Atc "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'ix_cpm_ws_created_id'"
      # expect: ix_cpm_ws_created_id|t
      # 'f' = an interrupted CONCURRENTLY build: DROP INDEX CONCURRENTLY ix_cpm_ws_created_id; then re-run the migrate service

### 6. Backfill `workspaces.external_ref` (before enabling SaaS provisioning retries)

Export SaaS `(Project.cmWorkspaceId, Project.id)` to `map.csv`, then

    cd /srv/crawmatic/crawmatic && uv run python scripts/security/backfill_workspace_external_ref.py map.csv > backfill.sql
    # review backfill.sql, then apply with psql using env-only credentials

(The script prints the same steps.)

### 7. Catalog index loader (next time the crawl index is refreshed)

The loader refuses (exit 2, nothing written) when the database volume has under `--min-free-gb`
(default 6 GiB) free, so the wrapper needs the volume size. Read it in the Railway dashboard (engine
project -> `postgres` -> Volume, size in GB), then:

    ! DB_VOLUME_GB=<volume GiB> bash /srv/crawmatic/crawmatic/scripts/load-catalog-index-prod.sh
    # optional: MIN_FREE_GB=8 to demand more headroom; MIN_FREE_GB=0 turns the check off
    # (then DB_VOLUME_GB is not needed)

The summary JSON (`/srv/crawmatic/evidence/catalog-index-load-2026-10-02/load-summary.json`) carries
`rows_before`, `rows_after` and `db_free_gb_before`; both tables are `VACUUM (ANALYZE)`d after the old
generation is deleted. A VACUUM error at that point only logs a WARNING (the new generation is already
live and the old one deleted); autovacuum reclaims the space. DR: after any restore the catalog index
tables are empty (excluded from dumps) while `catalog_index_loads` still says active; run this step
against the restored database before re-enabling SaaS discovery (`scripts/dr/RUNBOOK.md` "Restoring for
real" step 6).

### 8. Post-checks

- A SaaS BIWEEKLY monitor rule (`POST <control-plane base>/rules` with `cadence: BIWEEKLY`) is accepted and
  creates a refresh rule with `interval_minutes = 20160`.
- Next nightly run: no `breaker` regressions (10-05 behaviour: breaker-denied paid attempts deferred,
  unresolved access policy closes the target). Read-only: `proxy_circuit_breakers.observed` contains
  `proxied_requests_7d_p95_hourly`; during any trip, `outbox_messages` holds at most one PENDING
  `breaker-defer:<job>` row per job.
- Roughly 15 minutes after the deploy: `oldest_pending_target_age_seconds` on `/health/scraping` stays small
  (the script prints the command).
- API behaviour change to know about: `GET /v1/variants/competitor-prices` leaves out ARCHIVED
  matches unless `include_archived=true` (contract `docs/contracts/api-variants-competitor-prices.md`).
  The SaaS read-model sync and the monitoring price map call this route; archived (merchant
  removed) matches drop out of their snapshots after the deploy. The per-variant route is
  unchanged. The candidates route (`docs/contracts/admin-index-candidates.md`) now does at most
  50 variants per request and the SaaS already follows `next_cursor`.

### 9. Rollback

Redeploy each service's previous Railway deployment (dashboard: Deployments -> previous -> Redeploy),
i.e. back to `468418d`. This is a code-only rollback and the four migrations STAY applied (an unused
index and columns are harmless). Consequence: the old code expects head `a7c41e9d2b56`, so after the
rollback the api `/ready` reports `MigrationHeadMismatch` (`apps/api/app/routers/ready.py`, expected vs
live head), and anything that acts on `/ready` (an orchestrator or healthcheck) treats the api as not
ready. To clear it you must also move the schema back: either the migrations'
`alembic downgrade a7c41e9d2b56` (each downgrades cleanly; e2b8d4f6a1c3 does
`DROP INDEX CONCURRENTLY IF EXISTS ix_cpm_ws_created_id`) or restore from the DR backup taken in step 3
(`docs/DEPLOY-ROLLBACK.md`). **Warning: the `b3d9e5a17c42` competitor-domain rewrite is NOT undone by
downgrade** (canonical domains stay canonical); only the DR restore reverts that data.

### 10. Emergency: false trip, close the breaker by hand

Symptom: `proxy_circuit_breakers` is OPEN with `trip_reason = HOURLY_CEILING` (see `/ops/metrics`
`.snapshot.breaker`) but the traffic was legitimate (e.g. a big onboarding night). There is a supported
manual reset: `close_breaker()` in `libs/shared/app_shared/access/breaker.py`. State lives in the
`proxy_circuit_breakers` row `scope_key='global'` (Postgres, no Redis); every process re-reads it within
`PROXY_BREAKER_STATE_CACHE_SECONDS` (30 s). The evaluator re-trips on its next pass (every ~300 s) while
the trailing hour is still above the ceiling, so raise the floor FIRST:

1. Raise the floor on all four services (this redeploys them; do it now, not `--skip-deploys`). `<big>` =
   comfortably above the observed trailing hour (the trip `detail` prints the count and the ceiling):

       ! bash -c 'source /root/.railway/accounts.sh && cd /srv/crawmatic/crawmatic && sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH=/home/mahmoud/.nvm/versions/node/v24.18.0/bin:$PATH bash -c "for s in worker scrapers scrapers-browser api; do railway variable set PROXY_BREAKER_HOURLY_CEILING_FLOOR=<big> -s \$s -e production -p 69dc4bda-0d97-4290-a82f-822ed97d3fb8; done"'

2. Close it (run `railway ssh -s api` as in step 4; same code as `docs/ops/RUNBOOK_STOP_DISPATCH_AND_SPEND.md` section 3):

       ! bash -c 'source /root/.railway/accounts.sh && cd /srv/crawmatic/crawmatic && sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH=/home/mahmoud/.nvm/versions/node/v24.18.0/bin:$PATH railway ssh -s api -e production -p 69dc4bda-0d97-4290-a82f-822ed97d3fb8 -- python -c "from app_shared.database import get_system_session as g; from app_shared.access.breaker import close_breaker as c; cm = g(); s = cm.__enter__(); c(s, reason=\"manual reset by owner (false HOURLY_CEILING trip)\"); s.commit(); cm.__exit__(None, None, None); print(\"breaker CLOSED\")"'

   SQL equivalent if SSH is unavailable (needs a WRITE DSN, not the read-only one):

       UPDATE proxy_circuit_breakers SET state='CLOSED', trip_reason=NULL, detail='manual reset', cleared_at=now(), updated_at=now() WHERE scope_key='global';

3. Do nothing at all is also safe: with a clean window the breaker closes itself `PROXY_BREAKER_AUTO_CLOSE_AFTER_SECONDS`
   (3600) after the trip (recorded as `auto-recovery` in `detail`); raising the floor (step 1) is what makes
   the window clean. If it re-trips right after a manual close, the cause is real: do not keep raising.

Roll the floor back to the step-2 value afterwards if it was raised only for the emergency.

## Reference: breaker and dispatch economics (E4, P5, A9 engine lows)

Code only, no migration (the new trip reason `HOURLY_CEILING` fits the existing
`proxy_circuit_breakers.trip_reason` VARCHAR(32)). All settings have code defaults:

| Setting | Default | Meaning |
|---|---|---|
| `PROXY_BREAKER_HOURLY_CEILING` | unset (None) | Fixed trailing-1h proxied-request ceiling. Unset = measured: max(FLOOR, P95_FACTOR x the p95 hourly count of the 168 complete hours before the current one, hours that tripped the breaker left out). |
| `PROXY_BREAKER_HOURLY_CEILING_FLOOR` | `3000` | Floor of the measured ceiling. To disable the hourly-ceiling trip, leave `PROXY_BREAKER_HOURLY_CEILING` unset and set this to an empty value, `none` or `null` (any case); both settings parse those spellings as None. |
| `PROXY_BREAKER_HOURLY_CEILING_P95_FACTOR` | `3.0` | Multiplier on the week's p95 hour. |
| `SCRAPE_BREAKER_DEFER_DISPATCH_DEBOUNCE_SECONDS` | `60` | Debounce of the one outbox `dispatch_job` per job (`dedup_key=breaker-defer:<job>`) that BREAKER_OPEN defers now schedule (attempt-1 and retry paths). |
| `SCRAPE_MAX_BREAKER_DEFER_CYCLES` | `60` | BREAKER_OPEN defers one target may take in one job (about an hour at the 60 s debounce); the next one fails it `FAILED`/`BREAKER_OPEN`. Counted on its own Redis key (`breakerdefercycles:<job>:<match>`), separate from `SCRAPE_MAX_DEFER_CYCLES`. |

Changed meaning of an existing setting: `PROXY_BREAKER_VELOCITY_1H_HORIZON_SECONDS` (86400) is
now the CAP on the 1h horizon; the horizon itself is the day's measured busy hours
(24h count / 1h count, clamped 1-24).

Behaviour to expect after deploy:
- A trip with reason `HOURLY_CEILING` means the trailing hour exceeded the ceiling printed in
  `proxy_circuit_breakers.detail`; the row's `observed` JSON now carries
  `proxied_requests_7d_p95_hourly` and `tripped_hours` (the UTC hours of the last week's trips,
  which the p95 leaves out so a recurring runaway cannot raise its own ceiling). Auto-close
  works as before. The week-by-hour query runs once per process per hour.
- If the fleet grows abruptly (e.g. 5+ new Mushtryati-sized tenants in one nightly window
  before a week of history exists), the measured ceiling can trip on the first big night.
  Raise `PROXY_BREAKER_HOURLY_CEILING_FLOOR` (or set a fixed `PROXY_BREAKER_HOURLY_CEILING`)
  before onboarding such a batch.
- An OPEN breaker no longer refuses PLAYWRIGHT_DIRECT batches at the dispatcher.
- BREAKER_OPEN defers no longer burn `SCRAPE_MAX_DEFER_CYCLES`; they are bounded by
  `SCRAPE_MAX_BREAKER_DEFER_CYCLES` alone (a persisted strategy cursor is never re-charged by the
  attempt ladder, and the per-target deadline re-anchors on every claim, so neither bounds them).
  A trip longer than about an hour therefore fails the affected proxied targets with
  `BREAKER_OPEN` rather than holding the job open to its 12 h deadline.

Rollback of this part: the code-only rollback in step 9; no schema change to undo.
