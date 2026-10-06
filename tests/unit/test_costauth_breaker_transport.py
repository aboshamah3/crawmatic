"""An OPEN spend breaker stops PAID work only (2026-10-05).

The proxy circuit breaker measures proxied requests and exists to stop
proxy spend. The cost gate used to deny every transport while it was
OPEN, so one false trip (2026-10-04 21:21Z, ``VELOCITY_1H``) refused the
nightly jobs of direct sites that cost nothing -- pcpalace, extra,
crawmatic.com -- alongside the proxied ones. Stale or missing breaker
evidence still denies every transport: that says the brake itself has
stopped reporting, which is a reason to start nothing at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app_shared.costauth.service import (
    CostAuthorizationDenied,
    CostAuthorizationService,
    DenialReason,
)
from app_shared.models.proxy_breaker import ProxyBreakerState, ProxyBreakerTrip

_NOW = datetime(2026, 10, 4, 21, 30, tzinfo=timezone.utc)


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object:
        return self._row


class _Session:
    def __init__(self, row: object) -> None:
        self._row = row

    def execute(self, *_args: object, **_kwargs: object) -> _Result:
        return _Result(self._row)


def _row(state: ProxyBreakerState, *, age_seconds: int = 30) -> SimpleNamespace:
    return SimpleNamespace(
        state=state,
        trip_reason=ProxyBreakerTrip.VELOCITY_1H if state is ProxyBreakerState.OPEN else None,
        evaluated_at=_NOW - timedelta(seconds=age_seconds),
    )


def _check(row: object, transport: str) -> str:
    service = CostAuthorizationService(breaker_max_evidence_age_seconds=900)
    return service._check_breaker(_Session(row), _NOW, transport)


@pytest.mark.parametrize(
    "transport", ["DIRECT", "DIRECT_HTTP", "DIRECT_HTTP_RETRY", "PLAYWRIGHT_DIRECT"]
)
def test_an_open_breaker_still_authorizes_work_that_spends_no_proxy(transport: str) -> None:
    """2026-10-06 (A9): decided by ACCESS METHOD, not budget rung.
    PLAYWRIGHT_DIRECT bills the BROWSER rung (browser-seconds) but sends
    no proxied request, which is all the breaker measures."""
    assert _check(_row(ProxyBreakerState.OPEN), transport) == "OPEN"


@pytest.mark.parametrize("transport", ["PROXY", "PROXY_HTTP", "BROWSER", "PLAYWRIGHT_PROXY"])
def test_an_open_breaker_denies_every_transport_that_can_be_paid(transport: str) -> None:
    """A bare ``BROWSER`` rung could be either browser method, so it
    stays denied: only a known access method proves "no proxy"."""
    with pytest.raises(CostAuthorizationDenied) as excinfo:
        _check(_row(ProxyBreakerState.OPEN), transport)
    assert excinfo.value.reason == DenialReason.BREAKER_OPEN
    assert "VELOCITY_1H" in str(excinfo.value)


@pytest.mark.parametrize("transport", ["DIRECT", "PROXY", "BROWSER"])
def test_stale_breaker_evidence_denies_every_transport(transport: str) -> None:
    with pytest.raises(CostAuthorizationDenied) as excinfo:
        _check(_row(ProxyBreakerState.CLOSED, age_seconds=3600), transport)
    assert excinfo.value.reason == DenialReason.BREAKER_EVIDENCE_STALE


@pytest.mark.parametrize("transport", ["DIRECT", "PROXY"])
def test_a_missing_breaker_row_denies_every_transport(transport: str) -> None:
    with pytest.raises(CostAuthorizationDenied) as excinfo:
        _check(None, transport)
    assert excinfo.value.reason == DenialReason.BREAKER_EVIDENCE_STALE


@pytest.mark.parametrize("transport", ["DIRECT", "PROXY", "BROWSER"])
def test_a_closed_breaker_authorizes_every_transport(transport: str) -> None:
    assert _check(_row(ProxyBreakerState.CLOSED), transport) == "CLOSED"


def test_an_unknown_transport_is_treated_as_paid() -> None:
    """Fail closed: a transport the gate cannot classify is not free."""
    with pytest.raises(CostAuthorizationDenied) as excinfo:
        _check(_row(ProxyBreakerState.OPEN), "CARRIER_PIGEON")
    assert excinfo.value.reason == DenialReason.BREAKER_OPEN


# --- authorize() hands the gate the access method when it has one ---------


class _StopAfterBreaker(Exception):
    pass


def _authorize_with(row: object, *, transport: str, access_method: str | None) -> None:
    import uuid
    from contextlib import contextmanager

    from app_shared.costauth.service import AuthorizationPurpose, AuthorizationRequest

    service = CostAuthorizationService(breaker_max_evidence_age_seconds=900)

    @contextmanager
    def _session(_workspace_id: object):
        yield _Session(row)

    def _domain(*_args: object) -> None:
        raise _StopAfterBreaker

    service._tenant_session = _session  # type: ignore[method-assign]
    service._now = lambda: _NOW  # type: ignore[method-assign]
    service._check_entitlement = lambda *_a: 1  # type: ignore[method-assign]
    service._check_domain = _domain  # type: ignore[method-assign]
    service.authorize(
        AuthorizationRequest(
            workspace_id=uuid.uuid4(),
            domain="example.test",
            transport=transport,
            access_method=access_method,
            provider="browser",
            estimated_bytes=0,
            estimated_cost_micro_units=0,
            purpose=AuthorizationPurpose.REFRESH,
        )
    )


def test_authorize_lets_a_playwright_direct_browser_batch_past_an_open_breaker() -> None:
    with pytest.raises(_StopAfterBreaker):
        _authorize_with(
            _row(ProxyBreakerState.OPEN),
            transport="BROWSER",
            access_method="PLAYWRIGHT_DIRECT",
        )


@pytest.mark.parametrize("access_method", ["PLAYWRIGHT_PROXY", None])
def test_authorize_denies_a_proxied_or_unknown_browser_batch(access_method: str | None) -> None:
    with pytest.raises(CostAuthorizationDenied) as excinfo:
        _authorize_with(
            _row(ProxyBreakerState.OPEN), transport="BROWSER", access_method=access_method
        )
    assert excinfo.value.reason == DenialReason.BREAKER_OPEN
