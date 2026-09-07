"""Unit tests for `scripts/analyze_hot_query_plans.py`'s pg_stat_statements
delta report (EPA A9, deep dive §7.4).

`compute_query_stat_deltas` is the only piece that can be unit-tested without
a live Postgres and a real `--window-minutes` sleep: it takes two fake
snapshots (dicts keyed by `queryid`, shaped like `_snapshot_pg_stat_statements`
returns) and does pure arithmetic. Three things matter:

1. **Delta arithmetic** -- calls/total_time/temp_bytes deltas are computed
   correctly, including a stats-reset case where the counters go backwards
   between snapshots (clamped to the raw after-value, not a negative delta).
2. **Top-N ordering** -- results are ordered by `total_time_delta_ms`
   descending and capped at `top_n`.
3. **A query missing from the second snapshot is reported, not a crash** --
   it lands in the `dropped` list rather than raising or being silently
   dropped from the output.

No database, no network, no `time.sleep`.
"""

from __future__ import annotations

import sys
from pathlib import Path

# `scripts/` has no __init__.py / installed entry point -- match the
# sys.path convention `tests/unit/test_classify_match_set.py` uses to import
# `scripts.<module>`.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.analyze_hot_query_plans import (  # noqa: E402
    TEMP_BLOCK_SIZE_BYTES,
    compute_query_stat_deltas,
)


def _row(query: str, calls: int, total_exec_time: float, temp_read: int = 0, temp_written: int = 0):
    return {
        "query": query,
        "calls": calls,
        "total_exec_time": total_exec_time,
        "temp_blks_read": temp_read,
        "temp_blks_written": temp_written,
    }


class TestDeltaArithmetic:
    def test_delta_is_after_minus_before_for_an_existing_queryid(self):
        before = {1: _row("SELECT 1", calls=10, total_exec_time=100.0, temp_read=2, temp_written=3)}
        after = {1: _row("SELECT 1", calls=25, total_exec_time=400.0, temp_read=5, temp_written=7)}

        result = compute_query_stat_deltas(before, after)

        assert result["top"] == [
            {
                "queryid": 1,
                "query": "SELECT 1",
                "calls_delta": 15,
                "total_time_delta_ms": 300.0,
                "temp_bytes_delta": (3 + 4) * TEMP_BLOCK_SIZE_BYTES,
            }
        ]
        assert result["dropped"] == []

    def test_a_new_queryid_in_after_is_deltad_against_a_zero_baseline(self):
        before: dict = {}
        after = {2: _row("SELECT 2", calls=5, total_exec_time=50.0, temp_read=1, temp_written=1)}

        result = compute_query_stat_deltas(before, after)

        assert len(result["top"]) == 1
        row = result["top"][0]
        assert row["queryid"] == 2
        assert row["calls_delta"] == 5
        assert row["total_time_delta_ms"] == 50.0
        assert row["temp_bytes_delta"] == 2 * TEMP_BLOCK_SIZE_BYTES

    def test_a_stats_reset_between_snapshots_is_clamped_not_negative(self):
        # `after` counters are lower than `before` -- e.g. pg_stat_statements
        # was reset mid-window. The delta must not go negative; it reports
        # the raw after-value instead.
        before = {1: _row("SELECT 1", calls=1000, total_exec_time=9000.0, temp_read=50, temp_written=50)}
        after = {1: _row("SELECT 1", calls=3, total_exec_time=12.0, temp_read=1, temp_written=1)}

        result = compute_query_stat_deltas(before, after)

        row = result["top"][0]
        assert row["calls_delta"] == 3
        assert row["total_time_delta_ms"] == 12.0
        assert row["temp_bytes_delta"] == 2 * TEMP_BLOCK_SIZE_BYTES


class TestTopNOrdering:
    def test_results_are_ordered_by_total_time_delta_descending(self):
        before = {
            1: _row("cheap", calls=1, total_exec_time=0.0),
            2: _row("expensive", calls=1, total_exec_time=0.0),
            3: _row("medium", calls=1, total_exec_time=0.0),
        }
        after = {
            1: _row("cheap", calls=2, total_exec_time=10.0),
            2: _row("expensive", calls=2, total_exec_time=1000.0),
            3: _row("medium", calls=2, total_exec_time=500.0),
        }

        result = compute_query_stat_deltas(before, after)

        ordered_ids = [row["queryid"] for row in result["top"]]
        assert ordered_ids == [2, 3, 1]

    def test_top_n_caps_the_result_length(self):
        before = {i: _row(f"q{i}", calls=0, total_exec_time=0.0) for i in range(10)}
        after = {i: _row(f"q{i}", calls=1, total_exec_time=float(i)) for i in range(10)}

        result = compute_query_stat_deltas(before, after, top_n=3)

        assert len(result["top"]) == 3
        # Highest total_time_delta_ms values (9, 8, 7) come out first.
        assert [row["queryid"] for row in result["top"]] == [9, 8, 7]


class TestMissingFromSecondSnapshot:
    def test_a_query_dropped_from_the_second_snapshot_is_reported_not_a_crash(self):
        before = {
            1: _row("stays", calls=1, total_exec_time=1.0),
            2: _row("evicted", calls=1, total_exec_time=1.0),
        }
        after = {1: _row("stays", calls=2, total_exec_time=5.0)}

        # Must not raise.
        result = compute_query_stat_deltas(before, after)

        assert [row["queryid"] for row in result["top"]] == [1]
        assert result["dropped"] == [{"queryid": 2, "query": "evicted"}]

    def test_all_queries_dropped_yields_an_empty_top_and_a_full_dropped_list(self):
        before = {
            1: _row("gone-1", calls=1, total_exec_time=1.0),
            2: _row("gone-2", calls=1, total_exec_time=1.0),
        }
        after: dict = {}

        result = compute_query_stat_deltas(before, after)

        assert result["top"] == []
        assert {row["queryid"] for row in result["dropped"]} == {1, 2}
