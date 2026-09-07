"""Read-only production probe for audit risks M2 (extraction vocabulary) and M3
(hot maintenance-query plans), plus a `pg_stat_statements` delta report
(deep dive §7.4).

Standalone: takes a libpq URL on the command line or in ``ANALYZE_DATABASE_URL``
and prints (a) per-domain extraction outcomes and (b) ``EXPLAIN (ANALYZE,
BUFFERS)`` plans for every hot maintenance predicate identified in
``REPORT_EXTRACTION_AND_INDEXES_2026-08-15.md``.

SAFETY: the session is pinned ``default_transaction_read_only = on`` before any
statement runs, and every probe is a ``SELECT``. It creates nothing and writes
nothing. It is safe to point at production.

    uv run python scripts/analyze_hot_query_plans.py "postgresql://..."

`--from-pg-stat-statements --window-minutes N` switches to a different report:
snapshot `pg_stat_statements`, sleep N minutes, snapshot again, and print the
deltas ordered by total time, with calls and temp bytes. Requires the
extension to already be installed -- see `docs/ops/QUERY_STATS.md` for the
one-time owner setup step. Still read-only (`pg_stat_statements` is a view;
only the two `SELECT`s against it run).

    uv run python scripts/analyze_hot_query_plans.py "postgresql://..." \\
        --from-pg-stat-statements --window-minutes 10
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

import psycopg

# --- M2: what the extraction chain actually produces, per domain -------------
EXTRACTION_OUTCOMES = """
SELECT c.domain,
       count(*)                                                        AS obs,
       count(*) FILTER (WHERE po.success)                              AS ok,
       round(100.0 * count(*) FILTER (WHERE po.success) / count(*), 1) AS pct,
       count(*) FILTER (WHERE po.extraction_method = 'JSON_LD')        AS json_ld,
       count(*) FILTER (WHERE po.extraction_method = 'CSS')            AS css,
       count(*) FILTER (WHERE po.extraction_method = 'REGEX')          AS regex,
       count(*) FILTER (WHERE po.extraction_method = 'SINGLE_NUMBER')  AS single_number,
       count(*) FILTER (WHERE po.extraction_method IN
                        ('PLATFORM_JSON', 'EMBEDDED_JSON', 'XPATH', 'PLAYWRIGHT'))
                                                                       AS unimplemented,
       count(*) FILTER (WHERE po.error_code IN
                        ('PRICE_NOT_FOUND', 'INVALID_PRICE_FORMAT', 'LOW_CONFIDENCE'))
                                                                       AS extraction_fail,
       count(*) FILTER (WHERE po.error_code IN
                        ('RATE_LIMITED', 'HTTP_429', 'BLOCKED', 'HTTP_403',
                         'TIMEOUT', 'PROXY_FAILED', 'PLAYWRIGHT_FAILED'))
                                                                       AS transport_fail
FROM price_observations po
JOIN competitor_product_matches m ON m.id = po.match_id
JOIN competitors c ON c.id = m.competitor_id
WHERE po.scraped_at >= %(since)s
GROUP BY 1
ORDER BY obs DESC
"""

# Paid (proxied) attempts spent on a page whose price the chain could not read.
PAID_ATTEMPTS_WITHOUT_PRICE = """
WITH failed AS (
    SELECT DISTINCT po.match_id, po.scrape_job_id, po.error_code
    FROM price_observations po
    WHERE po.scraped_at >= %(since)s
      AND po.success = false
      AND po.error_code IN ('PRICE_NOT_FOUND', 'INVALID_PRICE_FORMAT')
)
SELECT c.domain,
       failed.error_code,
       count(DISTINCT failed.match_id) AS matches,
       sum((SELECT count(*)
            FROM request_attempts ra
            WHERE ra.match_id = failed.match_id
              AND ra.scrape_job_id = failed.scrape_job_id
              AND ra.created_at >= %(since)s
              AND ra.access_method IN ('PROXY_HTTP', 'PLAYWRIGHT_PROXY'))) AS paid_attempts
FROM failed
JOIN competitor_product_matches m ON m.id = failed.match_id
JOIN competitors c ON c.id = m.competitor_id
GROUP BY 1, 2
ORDER BY paid_attempts DESC NULLS LAST
"""

# Every extraction method the strategy optimizer has ever recorded a stat for.
LEARNED_EXTRACTION_METHODS = """
SELECT method_name, count(*) AS profiles, sum(attempt_count) AS attempts,
       sum(success_count) AS successes
FROM strategy_attempt_stats
WHERE method_type = 'EXTRACTION'
GROUP BY 1
ORDER BY attempts DESC
"""

# --- M3: hot maintenance predicates, one EXPLAIN each ------------------------
# Each entry: (label, SQL, params). Every one is a SELECT, so EXPLAIN ANALYZE
# only ever executes reads.
HOT_PREDICATES: list[tuple[str, str, dict[str, Any]]] = [
    (
        "_scan_job_refs (finalize/redispatch/recover job scan)",
        "SELECT id, workspace_id FROM scrape_jobs "
        "WHERE status IN ('PENDING', 'RUNNING', 'DISPATCHED')",
        {},
    ),
    (
        "redispatch_pending_jobs: any DEFERRED target for a job",
        "SELECT * FROM scrape_job_targets "
        "WHERE workspace_id = %(ws)s AND scrape_job_id = %(job)s "
        "AND status = 'DEFERRED' LIMIT 1",
        {},
    ),
    (
        "recover_stalled_batches: unlocked PENDING targets for a job",
        "SELECT * FROM scrape_job_targets "
        "WHERE workspace_id = %(ws)s AND scrape_job_id = %(job)s "
        "AND status = 'PENDING' AND locked_at IS NULL",
        {},
    ),
    (
        "aggregate_counts: per-job status histogram",
        "SELECT status, count(*) FROM scrape_job_targets "
        "WHERE workspace_id = %(ws)s AND scrape_job_id = %(job)s GROUP BY status",
        {},
    ),
    (
        "run_refresh_pass: due-rule claim (FOR UPDATE omitted; read-only session)",
        "SELECT id FROM refresh_rules WHERE enabled AND next_run_at <= now() "
        "ORDER BY next_run_at LIMIT 1",
        {},
    ),
    (
        "daily_rollup driver scan (scraped_at::date defeats pruning)",
        "SELECT DISTINCT workspace_id, product_variant_id, product_id "
        "FROM price_observations WHERE scraped_at::date = %(day)s",
        {},
    ),
    (
        "daily_rollup per-(workspace, variant) scan -- runs once PER VARIANT",
        "SELECT price, currency, success, comparable FROM price_observations "
        "WHERE scraped_at::date = %(day)s AND workspace_id = %(ws)s "
        "AND product_variant_id = %(variant)s",
        {},
    ),
]


# --- pg_stat_statements delta report (deep dive §7.4) ------------------------
# `total_exec_time` and `temp_blks_*` are the PG13+ column names (compose runs
# postgres:17.5-bookworm; production's actual major is unconfirmed -- see
# docs/ops/QUERY_STATS.md).
PG_STAT_STATEMENTS_SNAPSHOT = """
SELECT queryid, query, calls, total_exec_time, temp_blks_read, temp_blks_written
FROM pg_stat_statements
"""

# PostgreSQL's fixed page size; temp_blks_read/written are counted in these.
TEMP_BLOCK_SIZE_BYTES = 8192


def _snapshot_pg_stat_statements(cur: psycopg.Cursor) -> dict[Any, dict[str, Any]]:
    """One read of `pg_stat_statements`, keyed by `queryid`."""
    cur.execute(PG_STAT_STATEMENTS_SNAPSHOT)
    snapshot: dict[Any, dict[str, Any]] = {}
    for queryid, query, calls, total_exec_time, temp_read, temp_written in cur.fetchall():
        snapshot[queryid] = {
            "query": query,
            "calls": calls or 0,
            "total_exec_time": total_exec_time or 0.0,
            "temp_blks_read": temp_read or 0,
            "temp_blks_written": temp_written or 0,
        }
    return snapshot


def compute_query_stat_deltas(
    before: dict[Any, dict[str, Any]],
    after: dict[Any, dict[str, Any]],
    *,
    top_n: int = 20,
) -> dict[str, list[dict[str, Any]]]:
    """Delta two `pg_stat_statements` snapshots keyed by `queryid`.

    Pure function -- no I/O, no sleep -- so the arithmetic, ordering, and the
    missing-query case are all unit-testable on fake snapshots.

    Returns ``{"top": [...], "dropped": [...]}``:

    - ``top``: every queryid present in *after*, delta'd against its row in
      *before* (a queryid that is new in *after* is delta'd against a zero
      baseline, i.e. reported in full), ordered by ``total_time_delta_ms``
      descending, capped at ``top_n``.
    - ``dropped``: queryids present in *before* but missing from *after*
      (evicted from the stats cache, or a stats reset in between) -- reported
      here rather than raised, so the report never crashes on a query that
      disappeared mid-window.
    """
    zero_row = {
        "calls": 0,
        "total_exec_time": 0.0,
        "temp_blks_read": 0,
        "temp_blks_written": 0,
    }

    rows: list[dict[str, Any]] = []
    for queryid, after_row in after.items():
        before_row = before.get(queryid, zero_row)

        calls_delta = after_row["calls"] - before_row["calls"]
        total_time_delta = after_row["total_exec_time"] - before_row["total_exec_time"]
        temp_read_delta = after_row["temp_blks_read"] - before_row["temp_blks_read"]
        temp_written_delta = after_row["temp_blks_written"] - before_row["temp_blks_written"]

        # A stats reset between the two snapshots makes an existing queryid's
        # counters go backwards. Report the raw after-value rather than a
        # meaningless negative delta.
        if calls_delta < 0:
            calls_delta = after_row["calls"]
            total_time_delta = after_row["total_exec_time"]
            temp_read_delta = after_row["temp_blks_read"]
            temp_written_delta = after_row["temp_blks_written"]

        rows.append(
            {
                "queryid": queryid,
                "query": after_row.get("query"),
                "calls_delta": calls_delta,
                "total_time_delta_ms": total_time_delta,
                "temp_bytes_delta": (temp_read_delta + temp_written_delta)
                * TEMP_BLOCK_SIZE_BYTES,
            }
        )

    rows.sort(key=lambda r: r["total_time_delta_ms"], reverse=True)

    dropped = [
        {"queryid": queryid, "query": row.get("query")}
        for queryid, row in before.items()
        if queryid not in after
    ]

    return {"top": rows[:top_n], "dropped": dropped}


def _run_pg_stat_statements_report(url: str, window_minutes: float, top_n: int) -> int:
    """Snapshot, sleep, snapshot, report. Read-only throughout."""
    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            try:
                before = _snapshot_pg_stat_statements(cur)
            except psycopg.Error as exc:
                print(f"pg_stat_statements is not queryable: {exc}", file=sys.stderr)
                print(
                    "See docs/ops/QUERY_STATS.md for the one-time owner setup step.",
                    file=sys.stderr,
                )
                conn.rollback()
                return 2
            conn.rollback()

        print(f"[snapshot 1 taken ({len(before)} queries); sleeping {window_minutes} minute(s)]")
        time.sleep(window_minutes * 60)

        with conn.cursor() as cur:
            cur.execute("SET default_transaction_read_only = on")
            after = _snapshot_pg_stat_statements(cur)
            conn.rollback()

    result = compute_query_stat_deltas(before, after, top_n=top_n)

    print(
        f"\n=== pg_stat_statements deltas over {window_minutes} minute(s) "
        f"(top {top_n} by total time) ==="
    )
    print("calls_delta | total_time_delta_ms | temp_bytes_delta | query")
    for row in result["top"]:
        query_preview = (row["query"] or "").replace("\n", " ").strip()[:120]
        print(
            f"{row['calls_delta']} | {row['total_time_delta_ms']:.2f} | "
            f"{row['temp_bytes_delta']} | {query_preview}"
        )

    dropped = result["dropped"]
    if dropped:
        noun = "query" if len(dropped) == 1 else "queries"
        print(
            f"\n=== {len(dropped)} {noun} present in the first snapshot but missing "
            "from the second (evicted from pg_stat_statements, or a stats reset "
            "in between) ==="
        )
        for row in dropped:
            query_preview = (row["query"] or "").replace("\n", " ").strip()[:120]
            print(f"- {query_preview}")

    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="libpq URL; falls back to ANALYZE_DATABASE_URL",
    )
    parser.add_argument(
        "--from-pg-stat-statements",
        action="store_true",
        help=(
            "Snapshot pg_stat_statements, sleep --window-minutes, snapshot "
            "again, and report the deltas instead of running the M2/M3 probes."
        ),
    )
    parser.add_argument(
        "--window-minutes",
        type=float,
        default=5.0,
        help="Minutes to sleep between the two pg_stat_statements snapshots (default: 5).",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="Queries to report, ordered by total time delta descending (default: 20).",
    )
    return parser.parse_args(argv)


def _fetch_probe_params(cur: psycopg.Cursor) -> dict[str, Any]:
    """Resolve the workspace/job/variant/day the plan probes bind to."""
    cur.execute("SELECT id FROM workspaces LIMIT 1")
    row = cur.fetchone()
    workspace_id = row[0] if row else None

    cur.execute("SELECT id FROM scrape_jobs ORDER BY total_targets DESC LIMIT 1")
    row = cur.fetchone()
    job_id = row[0] if row else None

    cur.execute(
        "SELECT product_variant_id, scraped_at::date FROM price_observations "
        "ORDER BY scraped_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    variant_id, day = (row[0], row[1]) if row else (None, None)

    return {"ws": workspace_id, "job": job_id, "variant": variant_id, "day": day}


def _print_table(cur: psycopg.Cursor, title: str) -> None:
    print(f"\n=== {title} ===")
    if cur.description is None:
        print("(no result set)")
        return
    headers = [d.name for d in cur.description]
    rows = cur.fetchall()
    print(" | ".join(headers))
    for row in rows:
        print(" | ".join("" if v is None else str(v) for v in row))


def main(argv: list[str]) -> int:
    args = parse_args(argv[1:])
    url = args.url or os.environ.get("ANALYZE_DATABASE_URL", "")
    if not url:
        print(
            "usage: analyze_hot_query_plans.py <libpq-url> "
            "[--from-pg-stat-statements --window-minutes N]  "
            "(or set ANALYZE_DATABASE_URL)",
            file=sys.stderr,
        )
        return 2
    # SQLAlchemy-style URLs are accepted for convenience; psycopg wants plain libpq.
    url = url.replace("postgresql+psycopg://", "postgresql://")

    if args.from_pg_stat_statements:
        return _run_pg_stat_statements_report(url, args.window_minutes, args.top_n)

    since = os.environ.get("ANALYZE_SINCE", "2026-08-10")

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            # Hard read-only guard: any accidental write below fails loudly.
            cur.execute("SET default_transaction_read_only = on")

            cur.execute(EXTRACTION_OUTCOMES, {"since": since})
            _print_table(cur, f"M2 extraction outcomes per domain (since {since})")

            cur.execute(PAID_ATTEMPTS_WITHOUT_PRICE, {"since": since})
            _print_table(cur, f"M2 paid attempts yielding no price (since {since})")

            cur.execute(LEARNED_EXTRACTION_METHODS)
            _print_table(cur, "M2 extraction methods the optimizer has ever seen")

            params = _fetch_probe_params(cur)
            print(f"\n[plan probes bound to {params}]")
            for label, sql, _extra in HOT_PREDICATES:
                print(f"\n=== M3 PLAN: {label} ===")
                try:
                    cur.execute(f"EXPLAIN (ANALYZE, BUFFERS, COSTS OFF) {sql}", params)
                except psycopg.Error as exc:
                    print(f"(skipped: {exc})")
                    conn.rollback()
                    cur.execute("SET default_transaction_read_only = on")
                    continue
                for (line,) in cur.fetchall():
                    print(line)

        conn.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
