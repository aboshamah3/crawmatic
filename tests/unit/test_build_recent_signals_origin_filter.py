"""`build_recent_signals` must filter to `origin='scrape'` (Task 2.3,
proxy-cost-reduction plan §2.3, safety prerequisite for §3.3).

`build_recent_signals` (`app_shared.strategy.rediscovery`) assembles the
per-attempt-outcome signal source (conditions 3, 5, 6, 7, 8,
`contracts/rediscovery.md`) from the last-N `request_attempts` rows for a
profile's preferred access method. Discovery probes now also write
`request_attempts` rows (Task 2.3 step 2, `origin='discovery'`) -- a
probe's deliberately multi-method, deliberately noisy ladder must never
be misread by `evaluate_rediscovery` as a real scrape degrading (the
Task 3.3 prerequisite), so this query must exclude them.

`build_recent_signals` is DB-touching (a real join across
`request_attempts`/`competitor_product_matches`/`price_observations`/
`scrape_profiles`) and has no live-Postgres unit coverage in this repo
today (`test_rediscovery.py`'s docstring explicitly defers it to a
DB-backed integration suite) -- so this is a pure *query-shape* test: it
captures the `Select` `build_recent_signals` hands to `session.execute`
and asserts the compiled SQL carries the `origin = 'scrape'` predicate,
without needing a live database.
"""

from __future__ import annotations

import uuid

from app_shared.enums import AccessMethod
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.strategy.rediscovery import build_recent_signals


class _EmptyResult:
    def all(self) -> list[object]:
        return []


class _CapturingSession:
    """Fake `Session` whose only job is to remember the `Select` it was
    asked to execute -- never touches a real database."""

    def __init__(self) -> None:
        self.captured_stmt: object | None = None

    def execute(self, stmt: object) -> _EmptyResult:
        self.captured_stmt = stmt
        return _EmptyResult()


def _profile() -> DomainStrategyProfile:
    return DomainStrategyProfile(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        competitor_id=uuid.uuid4(),
        domain="shop.example.com",
        url_pattern="shop.example.com/p/*",
        url_pattern_version=1,
        preferred_access_method=AccessMethod.DIRECT_HTTP,
    )


def test_build_recent_signals_filters_to_scrape_origin() -> None:
    session = _CapturingSession()

    build_recent_signals(session, _profile())

    assert session.captured_stmt is not None, "build_recent_signals never called session.execute"
    compiled = session.captured_stmt.compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    assert "request_attempts.origin" in sql, sql
    assert "'scrape'" in sql, sql
