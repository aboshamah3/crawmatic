"""Shared utilities for `tests/load/` (EPA W5.5-GA item B, report §10).

Not a pytest module — these scripts are run directly (`uv run python
tests/load/scenario_*.py`), not collected. `test_*`-prefixed function names
are avoided everywhere in this directory for that reason (pytest's default
`tests/` collection would otherwise try, and fail, to collect these as
tests — they take no fixtures and print a report to stdout/JSON instead).

Every timing/count number this package produces is DEV-SERVER-BOUND — see
`README.md`. `label()` below is the one place that wording lives, so every
scenario script quotes it identically rather than five slightly different
disclaimers.
"""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

DEV_SERVER_BOUND_LABEL = (
    "DEV-SERVER-BOUND — measured on this one dev box under docker, no other "
    "tenant traffic, no production hardware. Never quote as a production "
    "budget without re-measurement on production-shaped infrastructure."
)


def label() -> str:
    return DEV_SERVER_BOUND_LABEL


@contextmanager
def timed() -> Iterator["Timing"]:
    t = Timing()
    start = time.perf_counter()
    try:
        yield t
    finally:
        t.elapsed_seconds = time.perf_counter() - start


@dataclass
class Timing:
    elapsed_seconds: float = 0.0


@dataclass
class QueryCounter:
    """Counts SQL statements executed on a SQLAlchemy `Engine`/`Connection`
    via the `before_cursor_execute` event — the N+1 detector.

    Usage::

        counter = QueryCounter()
        counter.attach(engine)
        ... do work ...
        counter.count  # total statements executed since attach()
        counter.statements  # the raw SQL text of each, in order

    `attach`/`detach` are explicit (not a context manager) so a scenario
    can attach once and read `.count` at several checkpoints without
    re-attaching — exactly what the N+1 scenario needs (query count at
    N=10, N=50, N=200 pairs, same running total reset between runs via
    `.reset()`).
    """

    count: int = 0
    statements: list[str] = field(default_factory=list)
    _engine: object = None

    def attach(self, engine) -> None:
        from sqlalchemy import event

        self._engine = engine

        def _before_cursor_execute(
            conn, cursor, statement, parameters, context, executemany
        ):
            self.count += 1
            self.statements.append(statement)

        event.listen(engine, "before_cursor_execute", _before_cursor_execute)
        self._listener = _before_cursor_execute

    def detach(self) -> None:
        from sqlalchemy import event

        if self._engine is not None and getattr(self, "_listener", None) is not None:
            event.remove(self._engine, "before_cursor_execute", self._listener)
        self._engine = None

    def reset(self) -> None:
        self.count = 0
        self.statements.clear()


def scratch_database_urls() -> dict[str, str]:
    """Resolve the three DSNs `run_load_suite.sh` exports for the DB-backed
    scenarios. Raises `RuntimeError` with a clear message (never a bare
    `KeyError`) if run standalone without the env the shell driver sets up
    — the DB-backed scenarios are meant to be invoked through
    `run_load_suite.sh`, not directly against a developer's real DB.
    """
    required = ("LOAD_TEST_APP_DATABASE_URL", "LOAD_TEST_SYSTEM_DATABASE_URL")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"missing {missing} — run this scenario through "
            "`bash tests/load/run_load_suite.sh`, which starts the named "
            "scratch Postgres container and exports these; it must never "
            "be pointed at a real DATABASE_URL."
        )
    return {
        "app": os.environ["LOAD_TEST_APP_DATABASE_URL"],
        "system": os.environ["LOAD_TEST_SYSTEM_DATABASE_URL"],
    }


def write_json_result(name: str, payload: dict) -> Path:
    """Write one scenario's structured result to the results directory
    `run_load_suite.sh` collects into the final report. Falls back to
    `tests/load/.results/` (gitignored-by-convention scratch, cleaned by
    the shell driver) when `LOAD_TEST_RESULTS_DIR` isn't set, so a
    standalone `uv run python tests/load/scenario_x.py` still works and
    prints instead of erroring.
    """
    out_dir = Path(os.environ.get("LOAD_TEST_RESULTS_DIR", "tests/load/.results"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    return out_path


def report_and_print(name: str, payload: dict) -> None:
    payload = {"scenario": name, "dev_server_bound": label(), **payload}
    path = write_json_result(name, payload)
    print(f"=== {name} ===", file=sys.stderr)
    print(json.dumps(payload, indent=2, default=str), file=sys.stderr)
    print(f"(written to {path})", file=sys.stderr)
