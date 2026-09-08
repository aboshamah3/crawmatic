# `tests/load/fault_injection/` — EPA D2 staging fault-injection matrix

Same convention as the parent `tests/load/README.md`: these are
standalone CLI scripts, run directly (`uv run python
tests/load/fault_injection/<script>.py ...`), never collected by pytest
(no `test_*`-prefixed module here). `tests/load/test_fault_injection_dry_run.py`
(one directory up, `-m load`) is the pytest-collected file — it imports
each script's pure `compute_measurement`/fixture functions and proves
the measurement logic offline, against fixtures, with no staging target
and no network/DB/Redis at all.

See `docs/ops/FAULT_INJECTION_2026-09.md` for the matrix table, the pass
bar per row, and why Step 1 (the actual staging runs) is DEFERRED —
no staging environment exists yet (D1, ASSUMPTIONS.md answer 2).

## What is here

| Script | Row | What it drives | Needs |
|---|---|---|---|
| `kill_worker_after_post.py` | 1 | EPA B2/F06 crash-after-POST window | `--database-url` |
| `kill_scraper_after_fetch.py` | 2 | 2026-09-03 G2 reaper proof, valid now A5 made `STARTED` real | `--database-url` |
| `pause_postgres_60s.py` | 3 | 60s Postgres pause mid-dispatch | `--database-url`, docker |
| `pause_redis_60s.py` | 4 | 60s Redis pause mid-dispatch | `--database-url`, `--redis-url`, docker |
| `two_schedulers.py` | 5 | EPA B3/F07 one-occurrence-one-job guarantee, two live scheduler processes | `--database-url` |
| `one_broken_tenant.py` | 6 | Tenant fairness: one 100%-blocked workspace must not stall others | `--database-url` |
| `host_limit_hold.py` | 7 | EPA B5/F10 fleet-wide admission cap, three real contending processes | `--redis-url` |

Every script:

- Refuses to run (`_common.require_staging`) without `--staging-target staging`
  exactly, and without an explicit `--database-url`/`--redis-url` (no env
  fallback) unless `--dry-run` is passed.
- Prints one line: the measurement(s) the plan requires, the pass bar in
  English, and `verdict=PASS` or `verdict=FAIL`. Exit code mirrors the
  verdict (`0`/`1`); a guard refusal exits `2`.
- Has a `--dry-run` mode that runs the identical `compute_measurement`
  function against a small, deliberately-passing fixture — a smoke test
  of the script's own wiring, not a claim about staging.

## Running (once a staging environment exists — D1)

```sh
cd /srv/crawmatic/crawmatic
uv run python tests/load/fault_injection/kill_worker_after_post.py \
    --staging-target staging \
    --database-url "$STAGING_DATABASE_URL" \
    --scrape-job-id <uuid>
```

Every other script follows the same shape; see each script's own
`--help` and module docstring for its specific flags.

## Why these seven, and not more

The task names exactly this set: the crash-after-POST window, the
scraper-kill/reaper proof, a Postgres pause, a Redis pause, the
two-scheduler race, one broken tenant, and the fleet host-admission cap
— each maps to one line of `PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md`
Task D2's Step 1. Nothing here duplicates an existing unit/integration
test's coverage of the SAME code path at a smaller scale
(`tests/unit/test_dispatch_kill_after_post.py`,
`tests/integration/test_two_schedulers_one_occurrence.py`); it re-proves
those claims against REAL, independently-deployed staging processes and
containers, which a fake-session unit test cannot do.
