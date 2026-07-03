"""Unit tests for the authenticated, idempotent Scrapyd dispatch client (T040).

``app_shared.scrapyd.client.ScrapydDispatchClient`` — exercised with a fake
Scrapyd (a stub ``requests.post``) and a hand-rolled fake Redis supporting the
``SET ... NX`` guard (no real Scrapyd/Redis/network). Covers US4/SC-005:

1. Correct creds + args -> ``schedule.json`` called with HTTP basic auth, the
   spider args forwarded unchanged -> jobid.
2. Missing/wrong creds -> Scrapyd 401 -> the client raises and NOTHING is
   scheduled AND no Redis key is left behind (a failed dispatch must not poison
   the idempotency key).
3. ``SET NX`` guard: a second dispatch of the same ``(scrape_job_id,
   batch_index)`` is a no-op returning the same jobid with no second POST.
4. A network failure likewise leaves no poisoned key, so a legitimate retry can
   still proceed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import requests

from app_shared.scrapyd.client import (
    ScrapydAuthError,
    ScrapydDispatchClient,
    ScrapydDispatchError,
    dispatch_key,
)

# --- test doubles ----------------------------------------------------------


@dataclass
class _FakeSettings:
    """Duck-typed stand-in for ``app_shared.config.Settings`` (dispatch subset)."""

    SCRAPYD_HTTP_URLS: list[str] = field(
        default_factory=lambda: ["http://scrapers:6800"]
    )
    SCRAPYD_USERNAME: str = "scrapyd"
    SCRAPYD_PASSWORD: str = "correct-horse"


class _FakeRedis:
    """In-memory Redis supporting the ``SET ... NX`` / GET / DELETE used here."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(
        self, name: str, value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        if nx and name in self.store:
            return None  # SET NX fails when the key already exists
        self.store[name] = value
        return True

    def get(self, name: str) -> str | None:
        return self.store.get(name)

    def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            if self.store.pop(name, None) is not None:
                removed += 1
        return removed


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeScrapyd:
    """Stub ``requests.post`` recording every call and returning a canned resp."""

    def __init__(
        self,
        *,
        expected_auth: tuple[str, str],
        status_code: int = 200,
        jobid: str = "jobid-abc123",
        raise_exc: Exception | None = None,
    ) -> None:
        self._expected_auth = expected_auth
        self._status_code = status_code
        self._jobid = jobid
        self._raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    def post(
        self,
        url: str,
        *,
        data: dict[str, Any],
        auth: tuple[str, str],
        timeout: float,
    ) -> _FakeResponse:
        self.calls.append(
            {"url": url, "data": data, "auth": auth, "timeout": timeout}
        )
        if self._raise_exc is not None:
            raise self._raise_exc
        # Simulate Scrapyd basic-auth enforcement: wrong creds -> 401.
        if auth != self._expected_auth or self._status_code == 401:
            return _FakeResponse(401, {"status": "error"})
        return _FakeResponse(
            self._status_code, {"status": "ok", "jobid": self._jobid}
        )


def _make_client(
    scrapyd: _FakeScrapyd,
    redis: _FakeRedis,
    settings: _FakeSettings | None = None,
) -> ScrapydDispatchClient:
    session = requests.Session()
    session.post = scrapyd.post  # type: ignore[assignment]
    return ScrapydDispatchClient(
        settings=settings or _FakeSettings(),  # type: ignore[arg-type]
        redis_client=redis,
        session=session,
    )


_ARGS = {
    "workspace_id": "11111111-1111-1111-1111-111111111111",
    "scrape_job_id": "22222222-2222-2222-2222-222222222222",
    "match_ids": "33333333-3333-3333-3333-333333333333,44444444-4444-4444-4444-444444444444",
    "mode": "HTTP",
    "batch_index": 0,
}


# --- tests -----------------------------------------------------------------


def test_schedule_sends_basic_auth_and_args_returns_jobid() -> None:
    settings = _FakeSettings()
    scrapyd = _FakeScrapyd(
        expected_auth=(settings.SCRAPYD_USERNAME, settings.SCRAPYD_PASSWORD),
        jobid="job-777",
    )
    redis = _FakeRedis()
    client = _make_client(scrapyd, redis, settings)

    jobid = client.schedule("price_monitor", "generic_price_spider", **_ARGS)

    assert jobid == "job-777"
    assert len(scrapyd.calls) == 1
    call = scrapyd.calls[0]
    # basic auth from SCRAPYD_USERNAME/SCRAPYD_PASSWORD
    assert call["auth"] == ("scrapyd", "correct-horse")
    assert call["url"] == "http://scrapers:6800/schedule.json"
    # spider args forwarded UNCHANGED (plus project/spider)
    assert call["data"]["project"] == "price_monitor"
    assert call["data"]["spider"] == "generic_price_spider"
    for key, value in _ARGS.items():
        if key == "batch_index":
            continue  # batch_index is a dispatch-key input, not a spider arg
        assert call["data"][key] == value
    # jobid persisted as the durable idempotency backstop
    assert redis.get(dispatch_key(_ARGS["scrape_job_id"], _ARGS["batch_index"])) == "job-777"


def test_wrong_credentials_raise_401_and_schedule_nothing() -> None:
    settings = _FakeSettings(SCRAPYD_PASSWORD="wrong-password")
    scrapyd = _FakeScrapyd(
        # Scrapyd only accepts the *correct* password; the client sends "wrong".
        expected_auth=("scrapyd", "correct-horse"),
    )
    redis = _FakeRedis()
    client = _make_client(scrapyd, redis, settings)

    with pytest.raises(ScrapydAuthError):
        client.schedule("price_monitor", "generic_price_spider", **_ARGS)

    # It POSTed once (and got 401) but no run was accepted...
    assert len(scrapyd.calls) == 1
    # ...and crucially left NO poisoned idempotency key behind.
    key = dispatch_key(_ARGS["scrape_job_id"], _ARGS["batch_index"])
    assert redis.get(key) is None
    assert redis.store == {}


def test_set_nx_guard_second_dispatch_is_noop_same_jobid() -> None:
    settings = _FakeSettings()
    scrapyd = _FakeScrapyd(
        expected_auth=(settings.SCRAPYD_USERNAME, settings.SCRAPYD_PASSWORD),
        jobid="job-idem",
    )
    redis = _FakeRedis()
    client = _make_client(scrapyd, redis, settings)

    first = client.schedule("price_monitor", "generic_price_spider", **_ARGS)
    second = client.schedule("price_monitor", "generic_price_spider", **_ARGS)

    assert first == second == "job-idem"
    # The guard short-circuited the second dispatch: only ONE POST happened.
    assert len(scrapyd.calls) == 1


def test_network_failure_leaves_no_poisoned_key_and_retry_can_proceed() -> None:
    settings = _FakeSettings()
    redis = _FakeRedis()
    key = dispatch_key(_ARGS["scrape_job_id"], _ARGS["batch_index"])

    # First attempt: the network call blows up.
    failing = _FakeScrapyd(
        expected_auth=(settings.SCRAPYD_USERNAME, settings.SCRAPYD_PASSWORD),
        raise_exc=requests.ConnectionError("boom"),
    )
    client = _make_client(failing, redis, settings)
    with pytest.raises(requests.ConnectionError):
        client.schedule("price_monitor", "generic_price_spider", **_ARGS)

    # The failed attempt released its claim — no poisoned key.
    assert redis.get(key) is None

    # A legitimate retry now succeeds and schedules exactly once.
    ok = _FakeScrapyd(
        expected_auth=(settings.SCRAPYD_USERNAME, settings.SCRAPYD_PASSWORD),
        jobid="job-retry",
    )
    retry_client = _make_client(ok, redis, settings)
    jobid = retry_client.schedule("price_monitor", "generic_price_spider", **_ARGS)
    assert jobid == "job-retry"
    assert len(ok.calls) == 1
    assert redis.get(key) == "job-retry"


def test_concurrent_in_flight_claim_is_not_double_scheduled() -> None:
    """A key still holding the pending sentinel must not trigger a second POST."""
    settings = _FakeSettings()
    scrapyd = _FakeScrapyd(
        expected_auth=(settings.SCRAPYD_USERNAME, settings.SCRAPYD_PASSWORD),
    )
    redis = _FakeRedis()
    # Simulate another dispatch having claimed the slot but not yet committed.
    key = dispatch_key(_ARGS["scrape_job_id"], _ARGS["batch_index"])
    redis.set(key, "__dispatch_pending__")
    client = _make_client(scrapyd, redis, settings)

    with pytest.raises(ScrapydDispatchError):
        client.schedule("price_monitor", "generic_price_spider", **_ARGS)

    assert len(scrapyd.calls) == 0
