"""`ScrapydDispatchClient.list_job_states` -- which bucket each run is in (2026-09-29, E2).

`list_jobs` answers "does the node know this run"; the ended-run reaper and
stall recovery need "is it still live" -- pending/running vs finished. Same
contract on failure: an unreachable node raises, never collapses into "no
runs", because "no runs" would authorize reverting live work.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app_shared.scrapyd.client import ScrapydDispatchClient
from app_shared.scrapyd.errors import ScrapydDispatchError


class _Resp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Session:
    def __init__(self, response: _Resp | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, *, params: dict, auth: Any, timeout: float) -> _Resp:
        self.calls.append((url, params))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _client(session: _Session) -> ScrapydDispatchClient:
    settings = SimpleNamespace(SCRAPYD_USERNAME="u", SCRAPYD_PASSWORD="p")
    return ScrapydDispatchClient(settings=settings, redis_client=object(), session=session)


def test_each_run_is_reported_with_its_bucket() -> None:
    session = _Session(
        _Resp(
            200,
            {
                "pending": [{"id": "a"}],
                "running": [{"id": "b"}],
                "finished": [{"id": "c"}, {"nope": 1}],
            },
        )
    )
    states = _client(session).list_job_states("http://node:6800/", "price_monitor_browser")
    assert states == {"a": "pending", "b": "running", "c": "finished"}
    assert session.calls == [
        ("http://node:6800/listjobs.json", {"project": "price_monitor_browser"})
    ]


def test_list_jobs_is_still_the_set_of_known_ids() -> None:
    session = _Session(_Resp(200, {"pending": [{"id": "a"}], "finished": [{"id": "c"}]}))
    assert _client(session).list_jobs("http://node:6800") == {"a", "c"}


@pytest.mark.parametrize(
    "response", [ConnectionError("down"), _Resp(500, {}), _Resp(200, ["not", "an", "object"])]
)
def test_no_useful_answer_raises_instead_of_meaning_no_runs(response: Any) -> None:
    with pytest.raises(ScrapydDispatchError):
        _client(_Session(response)).list_job_states("http://node:6800", "p")
