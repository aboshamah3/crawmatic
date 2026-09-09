# Query statistics in production (deep dive §7.4)

`scripts/analyze_hot_query_plans.py --from-pg-stat-statements --window-minutes N`
snapshots `pg_stat_statements`, sleeps `N` minutes, snapshots it again, and
prints the delta per query over that window: calls, total execution time, and
temp bytes (spill to disk), ordered by total time descending. A query that
disappears from the second snapshot (evicted from the stats cache, or a stats
reset in between) is reported in a separate section, not treated as an error.

**Read-only.** The session is pinned `default_transaction_read_only = on`
before either snapshot; `pg_stat_statements` is a view, so both reads are
plain `SELECT`s. Safe to point at production once the extension is installed
(Step 1 below).

## Step 1 (owner gate) — install the extension

`pg_stat_statements` is not installed by default. This requires a Postgres
**restart**, so it is an OWNER GATE, executed in the A10 window against the
Railway Postgres service — **not** run as part of this task.

```sql
-- as the Railway Postgres admin role
ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';
ALTER SYSTEM SET pg_stat_statements.track = 'all';
-- restart the Railway Postgres service here
CREATE EXTENSION pg_stat_statements;
```

Reset time (owner fills in when Step 1 runs):

```
Extension reset at (UTC):
```

## Step 2 — run the report

```bash
uv run python scripts/analyze_hot_query_plans.py "$ANALYZE_DATABASE_URL" \
    --from-pg-stat-statements --window-minutes 10
```

Flags:

- `--from-pg-stat-statements` — switch from the default M2/M3 probe report to
  this delta report.
- `--window-minutes N` — minutes to sleep between the two snapshots (default
  5). Pick a window long enough to catch a representative mix of dispatch,
  finalize, and rollup traffic — 10-15 minutes during normal load is a
  reasonable start.
- `--top-n N` — how many queries to print, ordered by total time delta
  descending (default 20).

Sample output shape:

```
=== pg_stat_statements deltas over 10.0 minute(s) (top 20 by total time) ===
calls_delta | total_time_delta_ms | temp_bytes_delta | query
118 | 42311.07 | 0 | SELECT id, workspace_id FROM scrape_jobs WHERE status IN (...)
...

=== 1 query present in the first snapshot but missing from the second (evicted from pg_stat_statements, or a stats reset in between) ===
- SELECT ...
```

If the extension is not yet installed, the command exits `2` with a pointer
back to Step 1 rather than a raw Postgres error.

## What this closes

Ties hot-query suspicion (§7.3's `EXPLAIN` probes in the same script, run
without `--from-pg-stat-statements`) to measured production load: which
queries actually accumulate the most total time and temp-file spill over a
real window, not just what a single `EXPLAIN` plan predicts.
