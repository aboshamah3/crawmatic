"""B4 (review + bounty 2026-09-09): the pre-auth origin bound.

The bounty probe (`bounty-review-2026-09-09/core-admission-probe.json`)
drove 300 invented but correctly-formatted bearer values from a single
address at `apps/api/app/rate_limit.py` and got 300 private, empty
buckets: the limiter counted every request perfectly and bounded
nothing. Nothing had been *looked up* at that point in the request, so a
caller who could type `ck_` chose their own bucket.

What is pinned here:

* every attempt pays a bounded ORIGIN bound BEFORE any credential is
  trusted, so N invented credentials from one address share one bound
  and mint at most `ceiling` buckets rather than N;
* the credentials used are WELL FORMED (`API_KEY_PREFIX` +
  `secrets.token_urlsafe(32)`, exactly what `generate_api_key` mints) --
  a limiter that only resists junk strings resists nothing;
* the origin comes from the trusted-proxy resolver (`app.client_ip`), so
  a spoofed `X-Forwarded-For` prefix buys no new bucket;
* the probe paths are exempt EXACTLY, so a `/health…`-shaped 404 is
  metered like anything else;
* an authenticated tenant inside its own limits is untouched.

Same hand-rolled-fake convention as `tests/unit/test_rate_limit_async.py`
(`fakeredis` is not a dependency in this repo).
"""

from __future__ import annotations

import secrets
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.rate_limit as rate_limit_mod
from app.rate_limit import RateLimitMiddleware, is_exempt
from app_shared.security.api_keys import API_KEY_PREFIX


class FakeAsyncRedis:
    """Multi-key INCR+EXPIRE double.

    Mirrors the Lua contract exactly: keys in order, `args = [ttl,
    *limits]`, one count per key EVALUATED out, short-circuiting at the
    first key over its limit.
    """

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.script_calls = 0

    def register_script(self, _script_src: str):
        redis = self

        class _Script:
            async def __call__(self, keys=None, args=None):
                redis.script_calls += 1
                limits = [int(value) for value in list(args or [60])[1:]]
                counts = []
                for index, key in enumerate(keys or [""]):
                    count = redis.counters.get(key, 0) + 1
                    redis.counters[key] = count
                    counts.append(count)
                    # The script's short-circuit: a key already over its
                    # limit stops the loop, so the keys after it are
                    # never even created.
                    if index < len(limits) and count > limits[index]:
                        break
                return counts

        return _Script()


def _app(redis, *, read: int = 5, write: int = 5) -> FastAPI:
    application = FastAPI()
    application.add_middleware(
        RateLimitMiddleware,
        redis_factory=lambda: redis,
        read_per_minute=read,
        write_per_minute=write,
        enabled=True,
    )

    @application.get("/v1/things")
    def _read():
        return {"ok": True}

    for probe in ("/health", "/health/scraping", "/live", "/ready"):

        @application.get(probe)
        def _probe():
            return {"status": "ok"}

    @application.get("/healthz-not-a-probe")
    def _lookalike():
        return {"status": "ok"}

    return application


def _well_formed_credential() -> str:
    """Exactly the shape `app_shared.security.api_keys.generate_api_key` mints."""
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


@pytest.fixture()
def fanout_of_two(monkeypatch):
    """A small, explicit fan-out so the ceiling arithmetic is readable."""
    monkeypatch.setattr(rate_limit_mod, "API_RATE_LIMIT_IP_FANOUT", 2)
    return 2


# --- the bounty probe -------------------------------------------------------


def test_three_hundred_invented_credentials_share_one_origin_bound(fanout_of_two):
    """The reproduction, inverted into a regression test.

    300 distinct, well-formed, entirely invented bearer values from one
    address: before B4 all 300 were admitted and 300 buckets appeared.
    Now they share the origin bound, so admissions stop at the ceiling
    (`read * fan-out`) and the bucket count is bounded by it.
    """
    redis = FakeAsyncRedis()
    read = 5
    ceiling = read * fanout_of_two
    client = TestClient(_app(redis, read=read))

    statuses = [
        client.get(
            "/v1/things", headers={"Authorization": f"Bearer {_well_formed_credential()}"}
        ).status_code
        for _ in range(300)
    ]

    assert statuses.count(200) == ceiling
    assert statuses.count(429) == 300 - ceiling
    # One origin counter plus at most one credential bucket per ADMITTED
    # request -- nowhere near 300, which is the whole finding.
    assert len(redis.counters) <= ceiling + 1
    assert len(redis.counters) < 300


def test_origin_refusal_does_not_disclose_the_ceiling(fanout_of_two):
    """A caller that has proven nothing is not handed a measurement."""
    redis = FakeAsyncRedis()
    client = TestClient(_app(redis, read=2))

    statuses = []
    bodies = []
    for _ in range(12):
        resp = client.get(
            "/v1/things", headers={"Authorization": f"Bearer {_well_formed_credential()}"}
        )
        statuses.append(resp.status_code)
        bodies.append(resp.json())

    refusal = bodies[statuses.index(429)]
    assert refusal["error"]["code"] == "RATE_LIMITED"
    assert refusal["error"]["message"] == "Too many requests."


# --- the origin is the trusted-proxy resolver's answer, not the peer --------


def test_spoofed_forwarded_prefix_does_not_mint_a_new_origin_bucket(fanout_of_two):
    """`X-Forwarded-For` is caller-written up to the trusted hop.

    With `TRUSTED_PROXY_HOPS = 1` the real client is the RIGHTMOST entry
    (what our own edge appended). A caller varying everything to the left
    of it -- and varying their credential too -- must still land in the
    same origin bucket, which is exactly what `app.client_ip` guarantees
    and what reading the transport peer or the leftmost entry would not.
    """
    redis = FakeAsyncRedis()
    read = 3
    ceiling = read * fanout_of_two
    client = TestClient(_app(redis, read=read))

    statuses = []
    for i in range(ceiling + 4):
        statuses.append(
            client.get(
                "/v1/things",
                headers={
                    "Authorization": f"Bearer {_well_formed_credential()}",
                    "X-Forwarded-For": f"10.0.0.{i}, 198.51.100.7",
                },
            ).status_code
        )

    assert statuses.count(200) == ceiling
    assert 429 in statuses
    origin_keys = {k for k in redis.counters if ":ip:" in k}
    assert len(origin_keys) == 1


def test_different_real_clients_get_different_origin_buckets(fanout_of_two):
    redis = FakeAsyncRedis()
    client = TestClient(_app(redis, read=1))

    first = client.get("/v1/things", headers={"X-Forwarded-For": "198.51.100.7"})
    second = client.get("/v1/things", headers={"X-Forwarded-For": "203.0.113.9"})

    assert first.status_code == 200
    assert second.status_code == 200  # a different origin, so a different budget
    assert len({k for k in redis.counters if ":ip:" in k}) == 2


# --- probes are separated from tenant traffic -------------------------------


def test_exact_probe_paths_are_exempt():
    redis = FakeAsyncRedis()
    client = TestClient(_app(redis, read=1))

    for probe in ("/health", "/health/scraping", "/live", "/ready"):
        for _ in range(5):
            assert client.get(probe).status_code == 200
    assert redis.counters == {}  # probes never touch a tenant counter


def test_health_lookalike_paths_are_still_metered():
    """`/health` used to be a PREFIX, which made every `/health…`-shaped
    path a free, unmetered surface."""
    redis = FakeAsyncRedis()
    client = TestClient(_app(redis, read=1))

    statuses = [client.get("/healthz-not-a-probe").status_code for _ in range(3)]

    assert 429 in statuses


def test_is_exempt_matches_probes_exactly_and_service_trees_by_prefix():
    assert is_exempt("/health")
    assert is_exempt("/health/")
    assert is_exempt("/live")
    assert is_exempt("/ready")
    assert is_exempt("/health/scraping")
    assert not is_exempt("/healthz")
    assert not is_exempt("/health/scraping/extra")
    assert not is_exempt("/v1/products")
    assert is_exempt("/v1/auth/login")
    assert is_exempt("/v1/admin/usage")


# --- no change for an authenticated tenant inside its limits ---------------


class _Settings:
    JWT_SECRET = "test-secret"
    JWT_ALGORITHM = "HS256"


def _workspace_token(workspace_id: uuid.UUID) -> str:
    from app_shared.security.jwt import encode_access_token

    return encode_access_token(
        user_id=uuid.uuid4(),
        workspace_id=workspace_id,
        role="WORKSPACE_ADMIN",
        secret=_Settings.JWT_SECRET,
        algorithm=_Settings.JWT_ALGORITHM,
        ttl_seconds=3600,
    )


def test_tenant_inside_its_limit_is_untouched_and_still_sees_its_own_budget(
    fanout_of_two,
):
    redis = FakeAsyncRedis()
    read = 5
    client = TestClient(_app(redis, read=read))
    headers = {"Authorization": f"Bearer {_well_formed_credential()}"}

    responses = [client.get("/v1/things", headers=headers) for _ in range(read)]

    assert [r.status_code for r in responses] == [200] * read
    last = responses[-1]
    # The headers describe the caller's OWN budget, not the shared ceiling.
    assert last.headers["X-RateLimit-Limit"] == str(read)
    assert last.headers["X-RateLimit-Remaining"] == "0"
    assert redis.script_calls == read  # one round trip per request, still


def test_credential_bucket_still_bites_before_the_origin_ceiling(fanout_of_two):
    """One credential exhausting its own budget is refused at ITS limit,
    with the limit disclosed -- the F15 behaviour, unchanged."""
    redis = FakeAsyncRedis()
    read = 3
    client = TestClient(_app(redis, read=read))
    headers = {"Authorization": f"Bearer {_well_formed_credential()}"}

    for _ in range(read):
        assert client.get("/v1/things", headers=headers).status_code == 200
    refused = client.get("/v1/things", headers=headers)

    assert refused.status_code == 429
    assert str(read) in refused.json()["error"]["message"]  # limit disclosed


def test_two_tenants_behind_one_origin_both_stay_inside_their_budgets(fanout_of_two):
    """The fan-out exists for this shape: one address, several tenants,
    each inside its own limit, none refused."""
    redis = FakeAsyncRedis()
    read = 3
    client = TestClient(_app(redis, read=read))
    workspace_a = _workspace_token(uuid.uuid4())
    workspace_b = _workspace_token(uuid.uuid4())

    statuses = []
    for token in (workspace_a, workspace_b):
        for _ in range(read):
            statuses.append(
                client.get(
                    "/v1/things", headers={"Authorization": f"Bearer {token}"}
                ).status_code
            )

    assert statuses == [200] * (2 * read)
