"""EPA D2: offline/fixture-based proof that every fault-injection script's
measurement logic is correct, with no staging target, no Docker, no
Redis, and no database (PLAN_CORE_PRODUCTION_READINESS_2026-09-07.md Task
D2's Pre-Flight note: "ship ... dry-run/offline modes with fixture-based
tests").

Each script under `tests/load/fault_injection/` is a standalone CLI, not
a pytest module (see that directory's `README.md`), so this file loads
each one by file path (`importlib`, not a package import — that
directory deliberately carries no `__init__.py`, matching
`tests/load/harness.py`'s sibling-script convention) and exercises:

1. Its ``compute_measurement`` against both a fixture that should PASS
   and a hand-built fixture that should FAIL — proving the pass bar is
   actually discriminating, not vacuously true.
2. Its CLI's staging guard: refuses without ``--staging-target staging``,
   and refuses a live (non-``--dry-run``) invocation missing
   ``--database-url``/``--redis-url``.
3. Its own ``--dry-run`` CLI path end-to-end (``main()``), which is the
   same smoke test an operator gets from running the script by hand.

Marked ``load`` per the existing convention
(``tests/load/test_api_with_slow_dependencies.py``): "local stub-based
... test (no real Redis/Postgres); run explicitly with -m load".
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.load

FAULT_INJECTION_DIR = Path(__file__).resolve().parent / "fault_injection"


def _load_module(name: str) -> types.ModuleType:
    path = FAULT_INJECTION_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"fault_injection_{name}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # The scripts do `sys.path.insert(0, .../fault_injection)` themselves
    # on import (so `from _common import ...` resolves) — register the
    # module before exec so a script that imports itself recursively
    # (none do, but future ones might) would not double-execute.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def modules() -> dict[str, types.ModuleType]:
    names = [
        "kill_worker_after_post",
        "kill_scraper_after_fetch",
        "pause_postgres_60s",
        "pause_redis_60s",
        "two_schedulers",
        "one_broken_tenant",
        "host_limit_hold",
    ]
    return {name: _load_module(name) for name in names}


# --- 1. compute_measurement discriminates PASS from FAIL ------------------


def test_kill_worker_after_post_pass_and_fail(modules):
    mod = modules["kill_worker_after_post"]
    passing = mod.compute_measurement(mod._dry_run_fixture())
    assert passing.passed

    duplicate = mod.IntentRecord("id-1", ("job-a", "job-b"), "CONFIRMED", observation_count=1)
    lost = mod.IntentRecord("id-2", ("job-c",), "CONFIRMED", observation_count=0)
    failing = mod.compute_measurement([duplicate, lost])
    assert not failing.passed
    assert failing.duplicate_physical_fetches == 1
    assert failing.lost_observations == 1


def test_kill_scraper_after_fetch_pass_and_fail(modules):
    mod = modules["kill_scraper_after_fetch"]
    passing = mod.compute_measurement(mod._dry_run_fixture())
    assert passing.passed
    assert passing.targets_released_by_reaper == passing.targets_started_under_killed_container

    wedged = [
        mod.TargetRecord("t1", "STARTED", "STARTED", dispatched_at_cleared=False),
    ]
    failing = mod.compute_measurement(wedged)
    assert not failing.passed
    assert failing.targets_still_wedged == 1


def test_pause_postgres_60s_pass_and_fail(modules):
    mod = modules["pause_postgres_60s"]
    records, recovery = mod._dry_run_fixture()
    passing = mod.compute_measurement(records, recovery_seconds=recovery)
    assert passing.passed

    duplicate = mod.IntentRecord("id-1", ("job-a", "job-b"), "CONFIRMED", observation_count=1)
    failing = mod.compute_measurement([duplicate], recovery_seconds=None)
    assert not failing.passed


def test_pause_redis_60s_pass_and_fail(modules):
    mod = modules["pause_redis_60s"]
    records, stale = mod._dry_run_fixture()
    passing = mod.compute_measurement(records, stale_fleet_semaphore_entries=stale)
    assert passing.passed

    failing = mod.compute_measurement(records, stale_fleet_semaphore_entries=3)
    assert not failing.passed


def test_two_schedulers_pass_and_fail(modules):
    mod = modules["two_schedulers"]
    passing = mod.compute_measurement(mod._dry_run_fixture())
    assert passing.passed
    assert passing.duplicate_occurrence_rows == 0

    same_key_twice = [
        mod.OccurrenceRecord("rule-1", "2026-09-08T12:00:00Z", "job-1"),
        mod.OccurrenceRecord("rule-1", "2026-09-08T12:00:00Z", "job-2"),
    ]
    failing = mod.compute_measurement(same_key_twice)
    assert not failing.passed
    assert failing.duplicate_occurrence_rows == 1


def test_one_broken_tenant_pass_and_fail(modules):
    mod = modules["one_broken_tenant"]
    passing = mod.compute_measurement(mod._dry_run_fixture(), baseline_block_rate=0.05)
    assert passing.passed
    assert passing.broken_tenant_block_rate == 1.0
    assert passing.other_tenants_completed > 0

    # broken tenant only PARTIALLY blocked -> must fail (proving the
    # script does not just check "some blocking happened").
    partially_blocked = [mod.TargetOutcome("ws-broken", True, "COMPLETED")] + [
        mod.TargetOutcome("ws-broken", True, "BLOCKED") for _ in range(9)
    ]
    failing = mod.compute_measurement(partially_blocked, baseline_block_rate=0.05)
    assert not failing.passed

    # broken tenant fully blocked but NO other tenant made progress ->
    # must fail (proving "fairness" isn't vacuous when nobody else ran).
    only_broken = [mod.TargetOutcome("ws-broken", True, "BLOCKED") for _ in range(5)]
    failing2 = mod.compute_measurement(only_broken, baseline_block_rate=0.05)
    assert not failing2.passed


def test_host_limit_hold_pass_and_fail(modules):
    mod = modules["host_limit_hold"]
    samples, nodes = mod._dry_run_fixture()
    passing = mod.compute_measurement(samples, nodes_contending=nodes)
    assert passing.passed
    assert passing.cap_exceeded_samples == 0

    over_cap = [mod.LeaseSample(timestamp=0.0, in_flight=7, concurrency_cap=4)]
    failing = mod.compute_measurement(over_cap, nodes_contending=3)
    assert not failing.passed
    assert failing.cap_exceeded_samples == 1

    too_few_nodes = mod.compute_measurement(samples, nodes_contending=1)
    assert not too_few_nodes.passed


# --- 2. staging guard refuses correctly ------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "kill_worker_after_post",
        "kill_scraper_after_fetch",
        "pause_postgres_60s",
        "pause_redis_60s",
        "two_schedulers",
        "one_broken_tenant",
        "host_limit_hold",
    ],
)
def test_refuses_without_staging_target(modules, name, capsys):
    mod = modules[name]
    exit_code = mod.main(["--dry-run"])  # no --staging-target at all
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err


@pytest.mark.parametrize(
    "name",
    [
        "kill_worker_after_post",
        "kill_scraper_after_fetch",
        "pause_postgres_60s",
        "two_schedulers",
        "one_broken_tenant",
    ],
)
def test_refuses_live_run_without_database_url(modules, name, capsys):
    mod = modules[name]
    exit_code = mod.main(["--staging-target", "staging"])  # no --dry-run, no --database-url
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err
    assert "database-url" in captured.err


def test_pause_redis_60s_refuses_live_run_without_redis_url(modules, capsys):
    mod = modules["pause_redis_60s"]
    exit_code = mod.main(
        ["--staging-target", "staging", "--database-url", "postgresql://x/y"]
    )
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "redis-url" in captured.err


def test_host_limit_hold_refuses_live_run_without_redis_url(modules, capsys):
    mod = modules["host_limit_hold"]
    exit_code = mod.main(["--staging-target", "staging"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "redis-url" in captured.err


def test_staging_target_must_be_exact_literal(modules, capsys):
    mod = modules["kill_worker_after_post"]
    exit_code = mod.main(["--staging-target", "production", "--dry-run"])
    assert exit_code == 2
    captured = capsys.readouterr()
    assert "REFUSED" in captured.err


# --- 3. each script's --dry-run CLI path runs end to end -------------------


@pytest.mark.parametrize(
    "name,extra_args",
    [
        ("kill_worker_after_post", []),
        ("kill_scraper_after_fetch", []),
        ("pause_postgres_60s", []),
        ("pause_redis_60s", []),
        ("two_schedulers", []),
        ("one_broken_tenant", []),
        ("host_limit_hold", []),
    ],
)
def test_dry_run_cli_passes(modules, name, extra_args, capsys):
    mod = modules[name]
    exit_code = mod.main(["--staging-target", "staging", "--dry-run", *extra_args])
    captured = capsys.readouterr()
    assert "verdict=PASS" in captured.out
    assert exit_code == 0
