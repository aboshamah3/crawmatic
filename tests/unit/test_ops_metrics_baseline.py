"""Baseline OUTCOME metrics (EPA A5, deep dive §5).

Everything `OpsSnapshot` already measures is *machinery*: partitions
exist, the outbox drains, the breaker is closed, attempts are being made.
None of it measures the outcome the product is sold on — how many matches
actually got a fresh price, and what was spent in attempts per one. The
2026-09-03 review found production had done ZERO proxied scraping for
four days while every existing gauge read healthy; these seven gauges are
the half that was missing.

What this suite protects:

* **The names.** They are a contract consumed by B9 (alerting) and D5
  (the readiness gates). A rename here is a silently broken dashboard
  there, so the exact strings are asserted, not merely the values.
* **`None` is not `0`.** Every gauge distinguishes "we could not measure
  this" from "this is zero", and the renderer emits NO sample for the
  former. `cost_model_drift_ratio` is the sharpest case: with no imported
  provider figure a `0` would read as *perfect agreement* on exactly the
  days nobody reconciled anything.
* **One blind metric does not blind the rest.** Production has run behind
  the code's migration head before (`snapshot.py`'s own module docstring
  documents it), so a missing table degrades one group and leaves the
  others readable.

Run against a fake session rather than a live database (the
`tests/unit/test_ops_metrics_snapshot.py` convention): what is under test
here is the collector's contract — which SQL runs, how its result is
turned into a gauge, and what happens when it fails — not PostgreSQL's
`percentile_cont`.
"""

from __future__ import annotations

from typing import Any

import pytest

from app_shared.opsmetrics import emit as emit_mod
from app_shared.opsmetrics.emit import (
    ATTEMPTS_PER_VALID_FRESH_24H_SQL,
    COST_MODEL_DRIFT_RATIO_SQL,
    DISPATCH_AMBIGUOUS_INTENTS_SQL,
    FRESH_UNIQUE_MATCHES_24H_SQL,
    PERSISTENCE_FAILURES_1H_SQL,
    QUEUE_OLDEST_PENDING_SECONDS_SQL,
    TARGET_PHASE_P95_SECONDS_SQL,
    TARGET_PHASES,
    BaselineMetrics,
    collect_baseline_metrics,
    render_baseline_metrics_prometheus,
)

#: The seven gauge names. This tuple IS the contract B9/D5 consume.
_GAUGE_NAMES = (
    "crawmatic_fresh_unique_matches_24h",
    "crawmatic_attempts_per_valid_fresh_24h",
    "crawmatic_persistence_failures_1h",
    "crawmatic_dispatch_ambiguous_intents",
    "crawmatic_queue_oldest_pending_seconds",
    "crawmatic_cost_model_drift_ratio",
    "crawmatic_target_phase_p95_seconds",
)


class _Row:
    """Attribute-access row, the shape `Result.one()` returns."""

    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class _Result:
    def __init__(self, row: Any) -> None:
        self._row = row

    def one(self) -> Any:
        if isinstance(self._row, Exception):
            raise self._row
        return self._row


class _FakeSession:
    """Returns a canned row per SQL, matched on a distinctive fragment.

    A value that is an ``Exception`` instance is raised instead of
    returned, which is how the degrade paths are exercised.
    """

    def __init__(self, rows: dict[str, Any]) -> None:
        self.rows = rows
        self.executed: list[str] = []
        self.rollbacks = 0

    def execute(self, clause: Any) -> _Result:
        sql = str(clause)
        self.executed.append(sql)
        for fragment, row in self.rows.items():
            if fragment in sql:
                return _Result(row)
        raise AssertionError(f"unexpected SQL: {sql}")

    def rollback(self) -> None:
        self.rollbacks += 1


def _healthy_rows(**overrides: Any) -> dict[str, Any]:
    rows: dict[str, Any] = {
        "attempts_per_valid_fresh_24h": _Row(
            attempts_24h=900,
            fresh_unique_matches_24h=300,
            attempts_per_valid_fresh_24h=3.0,
        ),
        "persistence_failures_1h": _Row(persistence_failures_1h=0),
        "dispatch_ambiguous_intents": _Row(dispatch_ambiguous_intents=2),
        "queue_oldest_pending_seconds": _Row(queue_oldest_pending_seconds=41.5),
        "cost_model_drift_ratio": _Row(
            ledger_bytes_24h=1_200_000,
            provider_bytes_24h=1_000_000,
            cost_model_drift_ratio=1.2,
        ),
        "due_to_dispatch": _Row(
            due_to_dispatch=12.0,
            dispatch_to_first_network=3.5,
            first_network_to_persisted=8.25,
        ),
    }
    rows.update(overrides)
    return rows


# --- the SQL itself -----------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "must_contain"),
    [
        (
            FRESH_UNIQUE_MATCHES_24H_SQL,
            ("COUNT(DISTINCT match_id)", "match_current_prices", "24 hours"),
        ),
        (
            ATTEMPTS_PER_VALID_FRESH_24H_SQL,
            ("request_attempts", "match_current_prices", "NULLIF"),
        ),
        (
            PERSISTENCE_FAILURES_1H_SQL,
            ("request_attempts", "price_observations", "1 hour", "NOT EXISTS"),
        ),
        (
            DISPATCH_AMBIGUOUS_INTENTS_SQL,
            ("dispatch_intents", "'POSTED'", "5 minutes"),
        ),
        (
            QUEUE_OLDEST_PENDING_SECONDS_SQL,
            ("scrape_job_targets", "'PENDING'", "MIN(created_at)"),
        ),
        (
            COST_MODEL_DRIFT_RATIO_SQL,
            ("network_operations", "provider_usage_records", "NULLIF", "'PROXY'"),
        ),
        (
            TARGET_PHASE_P95_SECONDS_SQL,
            ("percentile_cont(0.95)", "scrape_job_targets", "FILTER"),
        ),
    ],
)
def test_sql_reads_the_tables_the_plan_names(sql: str, must_contain: tuple[str, ...]) -> None:
    for fragment in must_contain:
        assert fragment in sql, f"missing {fragment!r} in:\n{sql}"


def test_ratio_sql_divides_by_nullif_not_by_a_bare_denominator() -> None:
    """`NULLIF(x, 0)` is what turns "no signal" into NULL instead of a
    division error or, worse, a fabricated number. Both ratio gauges must
    use it — this is the single most load-bearing character in the file."""
    for sql in (ATTEMPTS_PER_VALID_FRESH_24H_SQL, COST_MODEL_DRIFT_RATIO_SQL):
        assert "NULLIF(" in sql
        assert "/ NULLIF(" in sql


def test_every_phase_p95_is_filtered_to_rows_with_both_boundaries() -> None:
    """A target that never reached a boundary has no duration for that
    phase — it is not a zero-length one. Without the `FILTER`, an outage
    (nothing dispatched, so no `dispatched_at`) would read as a
    suspiciously fast p95, i.e. the exact failure this gauge exists to
    catch would make it look healthy."""
    assert TARGET_PHASE_P95_SECONDS_SQL.count("FILTER") == len(TARGET_PHASES)
    assert TARGET_PHASE_P95_SECONDS_SQL.count("IS NOT NULL") >= len(TARGET_PHASES)


def test_phase_labels_are_the_plans_three() -> None:
    assert TARGET_PHASES == (
        "due_to_dispatch",
        "dispatch_to_first_network",
        "first_network_to_persisted",
    )


# --- collection ---------------------------------------------------------


def test_collect_reads_every_gauge() -> None:
    session = _FakeSession(_healthy_rows())

    metrics = collect_baseline_metrics(session)

    assert metrics.fresh_unique_matches_24h == 300
    assert metrics.attempts_24h == 900
    assert metrics.attempts_per_valid_fresh_24h == 3.0
    assert metrics.persistence_failures_1h == 0
    assert metrics.dispatch_ambiguous_intents == 2
    assert metrics.queue_oldest_pending_seconds == 41.5
    assert metrics.cost_model_drift_ratio == 1.2
    assert metrics.target_phase_p95_seconds == {
        "due_to_dispatch": 12.0,
        "dispatch_to_first_network": 3.5,
        "first_network_to_persisted": 8.25,
    }
    assert metrics.unavailable_reasons == {}
    assert len(session.executed) == 6


def test_zero_is_reported_as_zero_not_dropped() -> None:
    """`persistence_failures_1h == 0` is a real, good measurement and must
    survive as `0` — the `None` convention is for absence only. Getting
    this backwards would make a healthy hour indistinguishable from an
    unreadable one."""
    session = _FakeSession(
        _healthy_rows(persistence_failures_1h=_Row(persistence_failures_1h=0))
    )

    metrics = collect_baseline_metrics(session)

    assert metrics.persistence_failures_1h == 0
    assert "crawmatic_persistence_failures_1h 0.0" in render_baseline_metrics_prometheus(metrics)


def test_cost_model_drift_is_null_when_no_provider_figure_was_imported() -> None:
    """The acceptance criterion, exactly: NULL, never 0.

    No provider export imported for the window means we CANNOT say
    whether the cost model is drifting. A `0` here would render as
    perfect agreement precisely on the days nobody reconciled anything —
    a number that lies in the direction of comfort.
    """
    session = _FakeSession(
        _healthy_rows(
            cost_model_drift_ratio=_Row(
                ledger_bytes_24h=1_200_000,
                provider_bytes_24h=None,
                cost_model_drift_ratio=None,
            )
        )
    )

    metrics = collect_baseline_metrics(session)

    assert metrics.ledger_bytes_24h == 1_200_000
    assert metrics.provider_bytes_24h is None
    assert metrics.cost_model_drift_ratio is None
    assert "crawmatic_cost_model_drift_ratio" not in render_baseline_metrics_prometheus(metrics)


def test_cost_model_drift_is_null_when_the_provider_figure_is_zero() -> None:
    """Belt to the SQL's `NULLIF` braces: a provider row that reports zero
    bytes is still no signal, and must not divide into a ratio."""
    session = _FakeSession(
        _healthy_rows(
            cost_model_drift_ratio=_Row(
                ledger_bytes_24h=5_000,
                provider_bytes_24h=0,
                cost_model_drift_ratio=0.0,
            )
        )
    )

    assert collect_baseline_metrics(session).cost_model_drift_ratio is None


def test_a_missing_table_degrades_only_its_own_group() -> None:
    """Production has run behind the code's migration head. One
    unreadable table must not blind the other six gauges — and the
    failure must be NAMED, not swallowed into a silent `None`."""

    class _UndefinedTable(Exception):
        pass

    session = _FakeSession(
        _healthy_rows(dispatch_ambiguous_intents=_UndefinedTable("no such table"))
    )

    metrics = collect_baseline_metrics(session)

    assert metrics.dispatch_ambiguous_intents is None
    assert metrics.unavailable_reasons == {"dispatch_ambiguity": "_UndefinedTable"}
    # ... and everything else still came through.
    assert metrics.fresh_unique_matches_24h == 300
    assert metrics.queue_oldest_pending_seconds == 41.5


def test_a_failed_statement_rolls_back_before_the_next_one() -> None:
    """A failed statement poisons the transaction in PostgreSQL. Without
    the rollback, every SUBSEQUENT group would fail too and report a
    misleading `InFailedSqlTransaction` — one broken table would look
    like six."""

    class _Boom(Exception):
        pass

    session = _FakeSession(_healthy_rows(persistence_failures_1h=_Boom("boom")))

    metrics = collect_baseline_metrics(session)

    assert session.rollbacks == 1
    assert metrics.persistence_failures_1h is None
    assert metrics.dispatch_ambiguous_intents == 2


def test_collect_never_raises_even_when_everything_fails() -> None:
    class _Boom(Exception):
        pass

    session = _FakeSession({key: _Boom("boom") for key in _healthy_rows()})

    metrics = collect_baseline_metrics(session)

    assert len(metrics.unavailable_reasons) == 6
    assert metrics.as_dict()["fresh_unique_matches_24h"] is None


# --- rendering ----------------------------------------------------------


def test_every_gauge_name_has_a_help_entry() -> None:
    for name in _GAUGE_NAMES:
        assert name in emit_mod._PROM_HELP
        kind, help_text = emit_mod._PROM_HELP[name]
        assert kind == "gauge"
        assert help_text


def test_render_emits_the_contract_names() -> None:
    metrics = collect_baseline_metrics(_FakeSession(_healthy_rows()))

    text = render_baseline_metrics_prometheus(metrics)

    for name in _GAUGE_NAMES:
        assert f"# TYPE {name} gauge" in text
    for phase in TARGET_PHASES:
        assert f'crawmatic_target_phase_p95_seconds{{phase="{phase}"}}' in text


def test_render_emits_each_family_exactly_once() -> None:
    """The exposition format requires one contiguous block per family
    preceded by a single HELP/TYPE pair; a duplicated pair is rejected by
    strict parsers (the same trap `render_prometheus` buffers per name to
    avoid)."""
    metrics = collect_baseline_metrics(_FakeSession(_healthy_rows()))

    text = render_baseline_metrics_prometheus(metrics)

    for name in _GAUGE_NAMES:
        assert text.count(f"# TYPE {name} gauge") == 1


def test_render_omits_unmeasured_gauges_entirely() -> None:
    """No sample at all, rather than a `0` sample — a scraper must not be
    able to average an absence into a healthy-looking series."""
    assert render_baseline_metrics_prometheus(BaselineMetrics()) == ""


def test_render_omits_only_the_unmeasured_phase() -> None:
    metrics = BaselineMetrics(
        target_phase_p95_seconds={
            "due_to_dispatch": 9.0,
            "dispatch_to_first_network": None,
            "first_network_to_persisted": 4.0,
        }
    )

    text = render_baseline_metrics_prometheus(metrics)

    assert 'phase="due_to_dispatch"' in text
    assert 'phase="first_network_to_persisted"' in text
    assert 'phase="dispatch_to_first_network"' not in text


def test_baseline_metrics_are_json_serialisable_for_the_snapshot_line() -> None:
    import json

    metrics = collect_baseline_metrics(_FakeSession(_healthy_rows()))

    payload = json.loads(json.dumps(metrics.as_dict(), default=str))

    assert payload["attempts_per_valid_fresh_24h"] == 3.0
    assert payload["target_phase_p95_seconds"]["due_to_dispatch"] == 12.0


def test_thresholds_are_stated_and_ordered() -> None:
    """B9 wires these; D5 gates on them. A min above its max would be an
    alert that can never clear."""
    assert emit_mod.COST_MODEL_DRIFT_RATIO_MIN < emit_mod.COST_MODEL_DRIFT_RATIO_MAX
    assert emit_mod.ATTEMPTS_PER_VALID_FRESH_MAX > 1
    assert emit_mod.DISPATCH_AMBIGUOUS_INTENTS_MAX == 0
    assert emit_mod.PERSISTENCE_FAILURES_1H_MAX >= 1
