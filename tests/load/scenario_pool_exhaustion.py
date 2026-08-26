#!/usr/bin/env python3
"""DB pool exhaustion behaviour (EPA W5.5-GA item B, report §10).

Needs the scratch Postgres from `run_load_suite.sh`
(`LOAD_TEST_APP_DATABASE_URL`). Builds a deliberately small, bounded
SQLAlchemy `QueuePool` (`pool_size=2, max_overflow=1, pool_timeout=2` —
3 connections total available) and drives more concurrent workers than
that through it, each holding its connection for a short, deliberate
delay. The property under test is BOUNDEDNESS: a caller past the pool's
capacity must wait (queue) and, if it waits past `pool_timeout`, get a
clear `TimeoutError` from SQLAlchemy — never silent unbounded connection
growth against the database.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.exc import TimeoutError as SATimeoutError  # noqa: E402

from harness import report_and_print, scratch_database_urls, timed  # noqa: E402

POOL_SIZE = 2
MAX_OVERFLOW = 1
POOL_TIMEOUT_SECONDS = 2
CONCURRENT_WORKERS = 8  # deliberately > POOL_SIZE + MAX_OVERFLOW
# HOLD_SECONDS chosen so the queue genuinely outlasts pool_timeout for the
# LAST workers to reach the front: worst-case wait for the final worker is
# roughly ceil(CONCURRENT_WORKERS / pool_capacity) * HOLD_SECONDS =
# ceil(8/3) * 1.2s = 3.6s > POOL_TIMEOUT_SECONDS=2s — this is deliberate,
# to observe BOTH outcomes (acquired vs. clean pool_timeout) in one run,
# not just the "everyone eventually got in" case.
HOLD_SECONDS = 1.2


def _worker(engine, idx: int, results: list, lock: threading.Lock, start_barrier: threading.Barrier):
    start_barrier.wait()
    t0 = time.perf_counter()
    outcome = "unknown"
    try:
        with engine.connect() as conn:
            acquired_at = time.perf_counter() - t0
            conn.execute(text("SELECT pg_sleep(:s)"), {"s": HOLD_SECONDS})
            outcome = "acquired"
    except SATimeoutError:
        acquired_at = time.perf_counter() - t0
        outcome = "pool_timeout"
    except Exception as exc:  # noqa: BLE001 - recorded, not swallowed
        acquired_at = time.perf_counter() - t0
        outcome = f"error:{type(exc).__name__}"
    with lock:
        results.append({"worker": idx, "outcome": outcome, "wait_seconds": round(acquired_at, 3)})


def main() -> int:
    urls = scratch_database_urls()
    engine = create_engine(
        urls["app"],
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        pool_timeout=POOL_TIMEOUT_SECONDS,
    )

    results: list[dict] = []
    lock = threading.Lock()
    barrier = threading.Barrier(CONCURRENT_WORKERS)
    threads = [
        threading.Thread(target=_worker, args=(engine, i, results, lock, barrier))
        for i in range(CONCURRENT_WORKERS)
    ]

    with timed() as t:
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)

    engine.dispose()

    acquired = [r for r in results if r["outcome"] == "acquired"]
    timed_out = [r for r in results if r["outcome"] == "pool_timeout"]
    errored = [r for r in results if r["outcome"] not in ("acquired", "pool_timeout")]
    pool_capacity = POOL_SIZE + MAX_OVERFLOW

    bounded_ok = len(errored) == 0 and (len(acquired) + len(timed_out)) == CONCURRENT_WORKERS

    report_and_print(
        "pool_exhaustion",
        {
            "elapsed_seconds": round(t.elapsed_seconds, 4),
            "pool_size": POOL_SIZE,
            "max_overflow": MAX_OVERFLOW,
            "pool_capacity": pool_capacity,
            "pool_timeout_seconds": POOL_TIMEOUT_SECONDS,
            "concurrent_workers": CONCURRENT_WORKERS,
            "hold_seconds_per_worker": HOLD_SECONDS,
            "workers_acquired": len(acquired),
            "workers_pool_timeout": len(timed_out),
            "workers_other_error": len(errored),
            "results": results,
            "bounded_behavior_confirmed": bounded_ok,
            "finding": (
                f"PASS: with {CONCURRENT_WORKERS} concurrent callers against a "
                f"{pool_capacity}-connection bounded pool (pool_size={POOL_SIZE} + "
                f"max_overflow={MAX_OVERFLOW}), {len(acquired)} acquired a "
                f"connection and {len(timed_out)} got a clean SQLAlchemy "
                "TimeoutError after the configured pool_timeout — no silent "
                "unbounded connection growth, no unhandled exception type."
                if bounded_ok
                else f"FINDING: {len(errored)} worker(s) raised an unexpected "
                "error type instead of a clean pool timeout — see `results` for "
                "detail."
            ),
        },
    )
    return 0 if bounded_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
