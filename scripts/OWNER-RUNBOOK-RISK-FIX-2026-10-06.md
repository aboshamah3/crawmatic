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
| `PROXY_BREAKER_HOURLY_CEILING` | unset (None) | Fixed trailing-1h proxied-request ceiling. Unset = measured: max(FLOOR, P95_FACTOR x the trailing week's p95 hourly count). |
| `PROXY_BREAKER_HOURLY_CEILING_FLOOR` | `3000` | Floor of the measured ceiling. Set to empty/None (with no fixed ceiling) to disable the hourly-ceiling trip. |
| `PROXY_BREAKER_HOURLY_CEILING_P95_FACTOR` | `3.0` | Multiplier on the week's p95 hour. |
| `SCRAPE_BREAKER_DEFER_DISPATCH_DEBOUNCE_SECONDS` | `60` | Debounce of the one outbox `dispatch_job` per job (`dedup_key=breaker-defer:<job>`) that BREAKER_OPEN defers now schedule. |

Changed meaning of an existing setting: `PROXY_BREAKER_VELOCITY_1H_HORIZON_SECONDS` (86400) is
now the CAP on the 1h horizon; the horizon itself is the day's measured busy hours
(24h count / 1h count, clamped 1-24).

Behaviour to expect after deploy:
- A trip with reason `HOURLY_CEILING` means the trailing hour exceeded the ceiling printed in
  `proxy_circuit_breakers.detail`; the row's `observed` JSON now carries
  `proxied_requests_7d_p95_hourly`. Auto-close works as before.
- If the fleet grows abruptly (e.g. 5+ new Mushtryati-sized tenants in one nightly window
  before a week of history exists), the measured ceiling can trip on the first big night.
  Raise `PROXY_BREAKER_HOURLY_CEILING_FLOOR` (or set a fixed `PROXY_BREAKER_HOURLY_CEILING`)
  before onboarding such a batch.
- An OPEN breaker no longer refuses PLAYWRIGHT_DIRECT batches at the dispatcher.
- BREAKER_OPEN defers no longer burn `SCRAPE_MAX_DEFER_CYCLES`; they are bounded by the per-target
  and job deadlines instead.

Verify (read-only, after the next nightly run): `proxy_circuit_breakers.observed` contains
`proxied_requests_7d_p95_hourly`; during any trip, `outbox_messages` holds at most one PENDING
`breaker-defer:<job>` row per job.

Rollback: redeploy the previous engine deployment; no schema change to undo.
