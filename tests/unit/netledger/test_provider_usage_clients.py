"""Scheduled provider-usage evidence (2026-09-29, plan E7.1/E7.6).

Nothing had ever been imported into `provider_usage_records` in production:
the only importer was a manual script fed a hand-exported file. The
DataImpulse client below implements the provider's DOCUMENTED gateway
endpoint -- `GET https://gw.dataimpulse.com:777/api/stats_with_history`,
HTTP basic auth with the plan's login/password, response
`traffic_history[] {group_date, inbound_traffic, outgoing_traffic,
total_traffic, requests_count, errors}` (DataImpulse User API, Postman
collection 7041120/2sAY4rGRZC). The payload below is that documentation's
own example response. No network in these tests.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from app_shared.models.provider_usage import ProviderUsageGranularity
from app_shared.netledger.provider_usage_clients import (
    DataImpulseUsageClient,
    ProviderUsageFetchError,
    configured_usage_clients,
)

DOC_EXAMPLE = {
    "traffic_history": [
        {"group_date": "2024-09-25T00:00:00Z", "inbound_traffic": 3602, "outgoing_traffic": 1067,
         "total_traffic": 4669, "requests_count": 1, "errors": 0},
        {"group_date": "2024-09-26T00:00:00Z", "inbound_traffic": 634954, "outgoing_traffic": 282895,
         "total_traffic": 917849, "requests_count": 259, "errors": 0},
        {"group_date": "2024-10-07T00:00:00Z", "inbound_traffic": 2078543, "outgoing_traffic": 166468,
         "total_traffic": 2245011, "requests_count": 23, "errors": 0},
    ],
    "total_traffic": 32212254720,
    "traffic_used": 24109104947,
    "traffic_left": 8103149773,
    "used_threads": 0,
    "login": "examplelogin",
    "status": "ok",
    "message": None,
    "elapsed": "00:00:00.0023375",
}


class _Resp:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _client(payload: Any = DOC_EXAMPLE, status: int = 200, calls: list | None = None):
    def get(url: str, *, auth: Any, timeout: float) -> _Resp:
        if calls is not None:
            calls.append((url, auth))
        return _Resp(status, payload)

    return DataImpulseUsageClient(login="L", password="P-SECRET", http_get=get)


def test_each_completed_day_becomes_one_daily_host_less_window() -> None:
    calls: list = []
    sources = _client(calls=calls).fetch(
        since=date(2024, 9, 25), until=date(2024, 10, 7), now=datetime(2024, 10, 7, 9, tzinfo=timezone.utc)
    )
    # 10-07 is today (incomplete): never imported as if it were final.
    assert [s.window_start.date() for s in sources] == [date(2024, 9, 25), date(2024, 9, 26)]
    day = sources[1]
    assert day.provider == "dataimpulse"
    assert day.granularity is ProviderUsageGranularity.DAILY
    assert (day.window_end - day.window_start).days == 1
    (row,) = day.rows
    assert row.target_host is None
    assert row.total_bytes == 917849
    assert row.request_count == 259
    assert row.bytes_down == 634954 and row.bytes_up == 282895
    assert calls == [("https://gw.dataimpulse.com:777/api/stats_with_history", ("L", "P-SECRET"))]


def test_the_same_day_twice_is_the_same_evidence() -> None:
    now = datetime(2024, 10, 8, tzinfo=timezone.utc)
    a = _client().fetch(since=date(2024, 9, 25), until=date(2024, 10, 7), now=now)
    b = _client().fetch(since=date(2024, 9, 25), until=date(2024, 10, 7), now=now)
    assert [s.raw_bytes for s in a] == [s.raw_bytes for s in b]


@pytest.mark.parametrize(
    ("status", "payload"),
    [(401, {}), (500, {}), (200, {"status": "error", "message": "bad"}), (200, ["x"])],
)
def test_an_unusable_answer_raises_and_never_leaks_the_password(status: int, payload: Any) -> None:
    with pytest.raises(ProviderUsageFetchError) as info:
        _client(payload, status).fetch(
            since=date(2024, 9, 25), until=date(2024, 9, 26), now=datetime(2024, 10, 1, tzinfo=timezone.utc)
        )
    assert "P-SECRET" not in str(info.value)


def test_no_credentials_means_no_client() -> None:
    class _S:
        DATAIMPULSE_USAGE_API_LOGIN = None
        DATAIMPULSE_USAGE_API_PASSWORD = None
        DATAIMPULSE_USAGE_API_BASE_URL = "https://gw.dataimpulse.com:777"

    assert configured_usage_clients(_S()) == []

    class _T(_S):
        DATAIMPULSE_USAGE_API_LOGIN = "L"
        DATAIMPULSE_USAGE_API_PASSWORD = "P"

    (client,) = configured_usage_clients(_T())
    assert isinstance(client, DataImpulseUsageClient)
