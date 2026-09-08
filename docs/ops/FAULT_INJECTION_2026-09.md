# Fault-injection matrix — 2026-09 (Task D2, Stage D)

**Status: PREPARED, RESULTS DEFERRED.** No staging environment exists yet
(Task D1 creates it; ASSUMPTIONS.md answer 2 defers every owner-gated
staging/spend step in this plan). This document states the matrix, the
pass bar per row, and the exact command per script. Every result cell
below is empty by design — filling one in without a real staging run
would be a fabricated pass.

Scripts live in `tests/load/fault_injection/` (`README.md` there has the
one-line-per-script index and the `--dry-run`/staging-guard contract
every script shares). Offline proof that each script's measurement
logic is correct — no staging target, no Docker, no Redis, no database —
is `tests/load/test_fault_injection_dry_run.py` (`-m load`).

## Why this matrix, now

Three of the seven rows only became meaningful recently:

- **Row 2** (scraper kill / reaper) is the 2026-09-03 G2 deploy-survival
  proof. It was **not** a meaningful test before EPA A5: before A5,
  `mark_targets_started` never actually wrote the `STARTED` status, so
  the `STARTED` bucket `reap_stale_targets`'s pass 1
  (`revert_stale_started_targets`) reaps was always empty — a scraper
  kill could not have exposed a broken reaper because nothing was ever
  in the state the reaper looks for. A5 made `STARTED` real; this row
  re-proves the reaper against a genuinely wedged target for the first
  time.
- **Row 5** (two schedulers) exercises EPA B3/F07's
  `refresh_rule_occurrences` table against real, independently-deployed
  scheduler processes — a stronger claim than
  `tests/integration/test_two_schedulers_one_occurrence.py`'s two
  threads in one process, which cannot exercise true process-level
  interleaving or a process crash mid-claim.
- **Row 7** (host limit) exercises EPA B5/F10's fleet-wide Redis
  admission gate from three genuinely independent OS processes, which
  is the actual shape of the claim ("the fleet sees one host, not N
  workers") — a single-process test can only simulate that shape, never
  prove it.

## The matrix

| # | Script | Fault | Mechanism under test | Measurement | Pass bar |
|---|---|---|---|---|---|
| 1 | `kill_worker_after_post.py` | Kill the `worker` container immediately after a `schedule.json` POST is accepted | EPA B2/F06 dispatch-intent idempotency (`dispatch_intents`, `deterministic_scrapyd_job_id`, `reconcile_inflight_intents`) | Duplicate physical fetches (distinct `scrapyd_job_id`s POSTed per identity); lost observations (CONFIRMED intents with zero persisted `price_observations`) | Both `== 0` |
| 2 | `kill_scraper_after_fetch.py` | Kill the `scrapers`/`scrapers-browser` container mid-fetch, targets left `STARTED` | EPA A5 `STARTED` truth + `reap_stale_targets` pass 1 (`revert_stale_started_targets`, `SCRAPE_STARTED_REAP_AFTER_SECONDS`) | Targets released by the reaper (`STARTED` → `PENDING`, dispatch stamps cleared) vs. targets started under the killed container | `released == started_under_killed_container`; `still_wedged == 0` |
| 3 | `pause_postgres_60s.py` | `docker pause` the `postgres` container for exactly 60s mid-dispatch, then unpause | Dispatch-intent idempotency under a slow (not gone) database | Duplicate physical fetches; lost observations; recovery time to first post-unpause `CONFIRMED` (reported, not gated) | Both counts `== 0` |
| 4 | `pause_redis_60s.py` | `docker pause` the `redis` container for exactly 60s mid-dispatch | Dispatch-intent idempotency + fleet semaphore decay under a Redis outage | Duplicate physical fetches; lost observations; stale fleet semaphore entries after the recovery grace window | All three `== 0` |
| 5 | `two_schedulers.py` | Run two independent `scheduler` processes against one staging database, one due refresh rule | EPA B3/F07 `refresh_rule_occurrences` primary key (`rule_id`, `scheduled_for`) | Occurrence rows sharing the identical `(rule_id, scheduled_for)` key (`duplicate_occurrence_rows`) | `== 0` — exactly one `scrape_jobs` row per due occurrence |
| 6 | `one_broken_tenant.py` | One workspace's fixture store/domain returns a block signal for 100% of requests | Workspace-scoped fleet/tenant admission (`app_shared.limiter.keys`, `costauth.service._check_concurrency`) | Broken tenant's block rate; other tenants' completed count; other tenants' collateral block rate vs. an operator-supplied baseline | Broken tenant block rate `== 1.0`; other tenants' completed `> 0`; collateral block rate within baseline `+ 0.10` |
| 7 | `host_limit_hold.py` | Three independent processes ("nodes") hammer one `(domain, transport)` fleet lease concurrently | EPA B5/F10 `admit_fleet`/`fleet_snapshot` (no `workspace_id` — fleet-wide) | Sampled `in_flight` vs. `concurrency` cap across the contention window | `cap_exceeded_samples == 0` across `>= 3` contending nodes |

## Exact commands (Step 1 — DEFERRED, not run)

Every command below requires a real staging database/Redis URL the owner
provides once Task D1 stands up the environment. None was executed by
this EPA run.

```sh
cd /srv/crawmatic/crawmatic

# Row 1
uv run python tests/load/fault_injection/kill_worker_after_post.py \
    --staging-target staging --database-url "$STAGING_DATABASE_URL" \
    --scrape-job-id <uuid>

# Row 2
uv run python tests/load/fault_injection/kill_scraper_after_fetch.py \
    --staging-target staging --database-url "$STAGING_DATABASE_URL" \
    --target-ids-file /tmp/started-targets.txt

# Row 3
uv run python tests/load/fault_injection/pause_postgres_60s.py \
    --staging-target staging --database-url "$STAGING_DATABASE_URL" \
    --container postgres --scrape-job-id <uuid>

# Row 4
uv run python tests/load/fault_injection/pause_redis_60s.py \
    --staging-target staging --database-url "$STAGING_DATABASE_URL" \
    --redis-url "$STAGING_REDIS_URL" --container redis --scrape-job-id <uuid>

# Row 5
uv run python tests/load/fault_injection/two_schedulers.py \
    --staging-target staging --database-url "$STAGING_DATABASE_URL" \
    --rule-ids <uuid>[,<uuid>...]

# Row 6
uv run python tests/load/fault_injection/one_broken_tenant.py \
    --staging-target staging --database-url "$STAGING_DATABASE_URL" \
    --broken-workspace-id <uuid> \
    --window-start 2026-09-08T00:00:00Z --window-end 2026-09-08T01:00:00Z \
    --baseline-block-rate 0.05

# Row 7
uv run python tests/load/fault_injection/host_limit_hold.py \
    --staging-target staging --redis-url "$STAGING_REDIS_URL" \
    --domain fault-injection-host-limit.example --transport HTTP \
    --concurrency 4 --rate-per-minute 600
```

## Results

*(deliberately empty — Step 1 is deferred; fill in per row once run
against a real staging environment)*

| # | Ran at (UTC) | Result | Notes |
|---|---|---|---|
| 1 | | | |
| 2 | | | |
| 3 | | | |
| 4 | | | |
| 5 | | | |
| 6 | | | |
| 7 | | | |

## Offline verification (run today, proves the scripts work)

```sh
cd /srv/crawmatic/crawmatic
sudo -u mahmoud .venv/bin/pytest tests/load/test_fault_injection_dry_run.py -q -m load
```

Every script also accepts `--dry-run` standalone, e.g.:

```sh
uv run python tests/load/fault_injection/host_limit_hold.py --staging-target staging --dry-run
```
