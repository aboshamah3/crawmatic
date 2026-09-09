"""Unit tests for `scripts/railway_cost_watchdog.py` (EPA A8, deep dive §8.3).

Before this fix the script pointed at a hard-coded Railway project id and a
hand-maintained ``{serviceId: name}`` map that belonged to a DIFFERENT
project than this core one — its output was never actually a reading of
THIS project's cost. The fix: the project id comes from
``RAILWAY_PROJECT_ID`` (refused if unset), the service name map comes from
the API's own ``project.services`` query, and every daily run reconciles
the hour-by-hour usage sum against a single whole-window query (the shape
a 2026-08-03 incident suspected of under-reporting by ~3.7x, not
reproduced 2026-09-06) rather than trusting an inherited correction
factor.

No real network call is made anywhere in this file — `_graphql` is always
monkeypatched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import railway_cost_watchdog as watchdog  # noqa: E402


# --- RAILWAY_PROJECT_ID is required -------------------------------------------


def test_refuses_to_run_without_railway_project_id(monkeypatch):
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)
    with pytest.raises(SystemExit):
        watchdog.project_id()


def test_refuses_to_run_with_an_empty_railway_project_id(monkeypatch):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "   ")
    with pytest.raises(SystemExit):
        watchdog.project_id()


def test_project_id_reads_the_env_var(monkeypatch):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "some-project-id")
    assert watchdog.project_id() == "some-project-id"


def test_main_never_reaches_the_network_without_railway_project_id(monkeypatch):
    monkeypatch.delenv("RAILWAY_PROJECT_ID", raising=False)

    def _poisoned(*args, **kwargs):  # pragma: no cover - must never be called
        raise AssertionError("main() reached the network without RAILWAY_PROJECT_ID")

    monkeypatch.setattr(watchdog, "_read_token", _poisoned)
    monkeypatch.setattr(watchdog, "_graphql", _poisoned)
    with pytest.raises(SystemExit):
        watchdog.main()


# --- the hard-coded service map is gone ---------------------------------------


def test_the_hard_coded_service_map_no_longer_exists():
    assert not hasattr(watchdog, "SERVICE_NAMES")


def test_the_hard_coded_project_id_no_longer_exists():
    assert not hasattr(watchdog, "PROJECT_ID")


def test_service_names_are_fetched_from_the_api(monkeypatch):
    def _fake_graphql(token, query, variables):
        assert variables == {"p": "proj-1"}
        return {
            "data": {
                "project": {
                    "services": {
                        "edges": [
                            {"node": {"id": "svc-1", "name": "api"}},
                            {"node": {"id": "svc-2", "name": "worker"}},
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr(watchdog, "_graphql", _fake_graphql)
    names = watchdog.fetch_service_names("tok", "proj-1")
    assert names == {"svc-1": "api", "svc-2": "worker"}


def test_service_names_falls_back_to_the_raw_id_when_the_api_has_no_name(monkeypatch):
    def _fake_graphql(token, query, variables):
        return {
            "data": {
                "project": {
                    "services": {"edges": [{"node": {"id": "svc-9", "name": None}}]}
                }
            }
        }

    monkeypatch.setattr(watchdog, "_graphql", _fake_graphql)
    names = watchdog.fetch_service_names("tok", "proj-1")
    assert names == {"svc-9": "svc-9"}


def test_service_names_handles_an_error_response_gracefully(monkeypatch):
    monkeypatch.setattr(watchdog, "_graphql", lambda *a, **k: {"error": "boom"})
    assert watchdog.fetch_service_names("tok", "proj-1") == {}


# --- hourly-vs-window reconciliation -------------------------------------------


def test_aggregate_measurement_totals_sums_across_services():
    totals = {
        "api": {"MEMORY_USAGE_GB": 10.0, "CPU_USAGE": 5.0},
        "worker": {"MEMORY_USAGE_GB": 20.0, "NETWORK_TX_GB": 1.0},
    }
    aggregate = watchdog.aggregate_measurement_totals(totals)
    assert aggregate == {"MEMORY_USAGE_GB": 30.0, "CPU_USAGE": 5.0, "NETWORK_TX_GB": 1.0}


def test_reconciliation_is_zero_when_hourly_and_window_agree():
    hourly = {"api": {"MEMORY_USAGE_GB": 100.0, "CPU_USAGE": 50.0, "NETWORK_TX_GB": 10.0}}
    window = {"api": {"MEMORY_USAGE_GB": 100.0, "CPU_USAGE": 50.0, "NETWORK_TX_GB": 10.0}}
    deltas = watchdog.reconcile_hourly_vs_window(hourly, window)
    assert deltas == {"ram": 0.0, "cpu": 0.0, "egress": 0.0}


def test_reconciliation_computes_percent_delta_per_measurement():
    hourly = {"api": {"MEMORY_USAGE_GB": 100.0, "CPU_USAGE": 50.0, "NETWORK_TX_GB": 10.0}}
    # egress under-reported by the single-window query: 9.0 vs 10.0 hourly.
    window = {"api": {"MEMORY_USAGE_GB": 100.0, "CPU_USAGE": 50.0, "NETWORK_TX_GB": 9.0}}
    deltas = watchdog.reconcile_hourly_vs_window(hourly, window)
    assert deltas["ram"] == pytest.approx(0.0)
    assert deltas["cpu"] == pytest.approx(0.0)
    assert deltas["egress"] == pytest.approx(10.0)


def test_reconciliation_handles_both_sides_being_zero():
    hourly: dict = {}
    window: dict = {}
    deltas = watchdog.reconcile_hourly_vs_window(hourly, window)
    assert deltas == {"ram": 0.0, "cpu": 0.0, "egress": 0.0}


def test_reconciliation_reports_full_delta_when_hourly_side_measured_nothing():
    hourly = {"api": {"MEMORY_USAGE_GB": 0.0}}
    window = {"api": {"MEMORY_USAGE_GB": 5.0}}
    deltas = watchdog.reconcile_hourly_vs_window(hourly, window)
    assert deltas["ram"] == 100.0


def test_main_warns_when_reconciliation_exceeds_two_percent(monkeypatch, tmp_path):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj-1")
    monkeypatch.setattr(watchdog, "_read_token", lambda: "tok")
    monkeypatch.setattr(watchdog, "fetch_service_names", lambda token, pid: {"svc-1": "api"})
    monkeypatch.setattr(watchdog, "LOG_PATH", tmp_path / "watchdog.log")

    hourly_totals = {"api": {"MEMORY_USAGE_GB": 100.0, "CPU_USAGE": 50.0, "NETWORK_TX_GB": 10.0}}
    # egress single-window total is 3% off the hourly sum -> over the 2% ceiling.
    window_totals = {"api": {"MEMORY_USAGE_GB": 100.0, "CPU_USAGE": 50.0, "NETWORK_TX_GB": 9.7}}

    def _fake_fetch_last_24h(token, pid, service_names):
        return hourly_totals, [], "2026-09-06 00:00Z..2026-09-07 00:00Z", window_totals

    monkeypatch.setattr(watchdog, "fetch_last_24h", _fake_fetch_last_24h)

    exit_code = watchdog.main()

    assert exit_code == 2  # alerts present
    logged = (tmp_path / "watchdog.log").read_text(encoding="utf-8")
    assert "hourly_sum_vs_window_delta_pct=" in logged
    assert "ALERT RECONCILE" in logged


def test_main_has_no_reconcile_alert_when_within_the_ceiling(monkeypatch, tmp_path):
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "proj-1")
    monkeypatch.setattr(watchdog, "_read_token", lambda: "tok")
    monkeypatch.setattr(watchdog, "fetch_service_names", lambda token, pid: {"svc-1": "api"})
    monkeypatch.setattr(watchdog, "LOG_PATH", tmp_path / "watchdog.log")

    totals = {"api": {"MEMORY_USAGE_GB": 100.0, "CPU_USAGE": 50.0, "NETWORK_TX_GB": 10.0}}

    def _fake_fetch_last_24h(token, pid, service_names):
        return totals, [], "2026-09-06 00:00Z..2026-09-07 00:00Z", totals

    monkeypatch.setattr(watchdog, "fetch_last_24h", _fake_fetch_last_24h)

    exit_code = watchdog.main()

    assert exit_code == 0
    logged = (tmp_path / "watchdog.log").read_text(encoding="utf-8")
    assert "hourly_sum_vs_window_delta_pct=cpu:0.00%,ram:0.00%,egress:0.00%" in logged
    assert "ALERT RECONCILE" not in logged


# --- egress measurement is queried ---------------------------------------------


def test_usage_query_requests_network_tx_gb_for_egress():
    assert "NETWORK_TX_GB" in watchdog.USAGE_QUERY


def test_measurement_labels_cover_cpu_ram_and_egress():
    assert set(watchdog.MEASUREMENT_LABELS.values()) == {"cpu", "ram", "egress"}
