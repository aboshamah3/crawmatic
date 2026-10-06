# Owner runbook: engine risk-review fixes (2026-10-06)

Owner-run only. Claude does not touch Railway or prod databases. Engine Railway project
`69dc4bda-0d97-4290-a82f-822ed97d3fb8` (production) lives on the `railway2` account
(`RAILWAY_TOKEN_RAILWAY2` in `/root/.railway/accounts.sh`). Run each `!` line from the Claude prompt
or a root shell. Never deploy between 19:00Z and 23:59Z (nightly refresh window).

## Task 6: security branch + BIWEEKLY cadence (A8)

What ships: branch `fix/risk-review-engine-2026-10-06` (merge commits `4f52c50` security,
`c551417` biweekly; or the branch tip if later tasks added commits). It contains deployed main
468418d plus `fix/security-complete-2026-10-02` (99fa939, E1-E10, H1, I7) and
`feat/biweekly-cadence` (5a34a77). Three additive migrations, upgrade only, in this order:
`a7c41e9d2b56` (live) -> `b3d9e5a17c42` (canonical competitor domains, DATA-CHANGING, not
reversible by downgrade) -> `c4e8f2a6b913` (workspaces.external_ref) -> `d7a1f3c5e902`
(refresh-token family). `alembic heads` on the branch = `d7a1f3c5e902` (single head).

Prerequisites (from the 10-02 security runbook, still required):
- `INDEX_SERVICE_TOKEN` (>= 32 chars) set on the engine `api` service AND in
  `/root/.crawmatic/index-service-token` (0600), equal values. Hardened `/version` needs it.
- Read-only prod engine DSN in `/root/.crawmatic/engine-prod-readonly-dsn` (0600) for the
  competitor-domain collision dry run (the script stops before any migration on a collision).
- Test workspace key in `/root/.crawmatic/test-workspace-key` (0600) for the post-deploy checks.
- `free -m` shows > 1.5 GB available and `df -h /` > 2 GB free.

Steps:

1. Confirm the commit to ship and its single head:

       git -C /srv/crawmatic/crawmatic log --oneline -3 fix/risk-review-engine-2026-10-06
       cd /srv/crawmatic/crawmatic && uv run alembic heads      # expect: d7a1f3c5e902 (head)

2. Dry run (prints every action, mutates nothing). `DEPLOY_SHA` must be the branch tip:

       ! DEPLOY_SHA=$(git -C /srv/crawmatic/crawmatic rev-parse fix/risk-review-engine-2026-10-06) bash /srv/crawmatic/deploy-engine-security-2026-10-02.sh --dry-run

3. Real deploy (preflight, collision dry run, DR backup, optional `JWT_LEGACY_AUD_GRACE_UNTIL`,
   `migrate` service runs `alembic upgrade head` to `d7a1f3c5e902`, then api, worker, scheduler,
   scrapers, scrapers-browser, then checks). Answer the grace offer with `SET_JWT_GRACE=1` to
   avoid logging every engine user out at once:

       ! SET_JWT_GRACE=1 DEPLOY_SHA=$(git -C /srv/crawmatic/crawmatic rev-parse fix/risk-review-engine-2026-10-06) bash /srv/crawmatic/deploy-engine-security-2026-10-02.sh

   Resume after a failed service (migration already applied):

       ! ONLY_SERVICES="<remaining services>" SKIP_MIGRATE=1 SKIP_DR_BACKUP=1 DEPLOY_SHA=<same sha> bash /srv/crawmatic/deploy-engine-security-2026-10-02.sh

4. Re-register the Scrapyd egg (the 10-02 script predates this step; every `scrapers` deploy
   wipes the egg and HTTP scraping stalls until it is back):

       ! bash -c 'source /root/.railway/accounts.sh && cd /srv/crawmatic/crawmatic && sudo -n -u mahmoud -H env RAILWAY_API_TOKEN="$RAILWAY_TOKEN_RAILWAY2" PATH=/home/mahmoud/.nvm/versions/node/v24.18.0/bin:$PATH railway ssh -s scrapers -e production -p 69dc4bda-0d97-4290-a82f-822ed97d3fb8 -- python /app/apps/scrapers/register_egg.py'

5. Backfill `workspaces.external_ref` (before enabling SaaS provisioning retries): export SaaS
   `(Project.cmWorkspaceId, Project.id)` to `map.csv`, then

       cd /srv/crawmatic/crawmatic && uv run python scripts/security/backfill_workspace_external_ref.py map.csv > backfill.sql
       # review backfill.sql, then apply with psql using env-only credentials

6. Verify:
   - authenticated `/version` reports the shipped `git_sha` and `db_migration_head = d7a1f3c5e902`;
     bearer-less `/version` returns only `{"status":"ok"}`;
   - a SaaS BIWEEKLY monitor rule (`POST <control-plane base>/rules` with `cadence: BIWEEKLY`) is
     accepted and creates a refresh rule with `interval_minutes = 20160`;
   - next nightly run: no `breaker` regressions (10-05 behaviour: breaker-denied paid attempts
     deferred, unresolved access policy closes the target).

Rollback: redeploy each service's previous Railway deployment (dashboard: Deployments ->
previous -> Redeploy). The three migrations downgrade cleanly but `b3d9e5a17c42`'s domain
rewrite is not undone by downgrade; restore from the DR backup taken in step 3 if the schema
must go back. See `docs/DEPLOY-ROLLBACK.md`.

## Task 7: breaker and dispatch economics (E4, P5, A9 engine lows)

Code only, no migration (the new trip reason `HOURLY_CEILING` fits the existing
`proxy_circuit_breakers.trip_reason` VARCHAR(32)). Ships with the next engine deploy of
`worker`, `scheduler`, `scrapers` and `scrapers-browser` (the breaker, dispatcher and spider
all changed). No env var is required; every new setting has a code default:

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

Verify (read-only, after the next nightly run): `proxy_circuit_breakers.observed` contains
`proxied_requests_7d_p95_hourly`; during any trip, `outbox_messages` holds at most one PENDING
`breaker-defer:<job>` row per job.

Rollback: redeploy the previous engine deployment; no schema change to undo.

## Task 8: catalog index and API read shape (P6, P7)

What ships (with the Task 6 deploy, same branch): one more additive migration
`e2b8d4f6a1c3` on top of `d7a1f3c5e902`, so **the single head is now `e2b8d4f6a1c3`** (Task 6
step 1 and the `/version` check in step 6 expect it instead of `d7a1f3c5e902`). It builds
`ix_cpm_ws_created_id` on `competitor_product_matches (workspace_id, created_at, id)` with
`CREATE INDEX CONCURRENTLY` (no write lock; a few seconds at today's ~5k matches). Code changes
ship in `api` (competitor-prices archived filter, candidates cap, title-stage ordering); the loader
runs from this box.

**Deploy with the in-repo script `scripts/deploy-engine-risk-fix-2026-10-06.sh`, not the 10-02
script.** It is the 10-02 security deploy script retargeted to this release: branch
`fix/risk-review-engine-2026-10-06`, base `468418d` (live engine), `NEW_HEAD=e2b8d4f6a1c3`, a live
head of `a7c41e9d2b56`, `d7a1f3c5e902` or `e2b8d4f6a1c3` accepted, venv
`/srv/crawmatic/crawmatic/.venv`. These commands **replace Task 6 steps 2 and 3** (the 10-02 script
expects head `d7a1f3c5e902` and refuses this branch's tip). The original
`/srv/crawmatic/deploy-engine-security-2026-10-02.sh` only had its venv repointed to
`/srv/crawmatic/crawmatic/.venv` (so the `crawmatic-wt-security-2026-10-02` worktree can go) and
remains the security-only release script:

    cd /srv/crawmatic/crawmatic && uv run alembic heads      # expect: e2b8d4f6a1c3 (head)
    ! DEPLOY_SHA=$(git -C /srv/crawmatic/crawmatic rev-parse fix/risk-review-engine-2026-10-06) bash /srv/crawmatic/crawmatic/scripts/deploy-engine-risk-fix-2026-10-06.sh --dry-run
    ! SET_JWT_GRACE=1 DEPLOY_SHA=$(git -C /srv/crawmatic/crawmatic rev-parse fix/risk-review-engine-2026-10-06) bash /srv/crawmatic/crawmatic/scripts/deploy-engine-risk-fix-2026-10-06.sh

   Resume after a failed service: the same script with `ONLY_SERVICES="<remaining services>"
   SKIP_MIGRATE=1 SKIP_DR_BACKUP=1` and the same `DEPLOY_SHA`.

Steps after the deploy:

1. Confirm the index built valid (read-only DSN from Task 6):

       ! psql "$(cat /root/.crawmatic/engine-prod-readonly-dsn)" -Atc "SELECT c.relname, i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = 'ix_cpm_ws_created_id'"
       # expect: ix_cpm_ws_created_id|t
       # 'f' = an interrupted CONCURRENTLY build: DROP INDEX CONCURRENTLY ix_cpm_ws_created_id; then re-run the migrate service

2. API behaviour change to know about: `GET /v1/variants/competitor-prices` leaves out ARCHIVED
   matches unless `include_archived=true` (contract `docs/contracts/api-variants-competitor-prices.md`).
   The SaaS read-model sync and the monitoring price map call this route; archived (merchant
   removed) matches drop out of their snapshots after the deploy. The per-variant route is
   unchanged. The candidates route (`docs/contracts/admin-index-candidates.md`) now does at most
   50 variants per request and the SaaS already follows `next_cursor`.

3. Next catalog index load (whenever the crawl index is refreshed). The loader now refuses
   (exit 2, nothing written) when the database volume has under `--min-free-gb` (default 6 GiB)
   free, so the wrapper needs the volume size. Read it in the Railway dashboard (engine project ->
   `postgres` -> Volume, size in GB), then:

       ! DB_VOLUME_GB=<volume GiB> bash /srv/crawmatic/crawmatic/scripts/load-catalog-index-prod.sh
       # optional: MIN_FREE_GB=8 to demand more headroom; MIN_FREE_GB=0 turns the check off
       # (then DB_VOLUME_GB is not needed)

   The summary JSON (`/srv/crawmatic/evidence/catalog-index-load-2026-10-02/load-summary.json`)
   now carries `rows_before`, `rows_after` and `db_free_gb_before`; both tables are
   `VACUUM (ANALYZE)`d after the old generation is deleted.

4. DR: `scripts/dr/RUNBOOK.md` "Restoring for real" step 6. After any restore, the catalog index
   tables are empty (their data is excluded from dumps) while `catalog_index_loads` still says
   active: run step 3's command against the restored database before re-enabling SaaS discovery.

Rollback: redeploy the previous `api` deployment. The migration downgrades cleanly
(`DROP INDEX CONCURRENTLY IF EXISTS ix_cpm_ws_created_id`); leaving the index in place is harmless.
