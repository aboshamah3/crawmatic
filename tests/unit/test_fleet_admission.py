"""``app_shared/limiter/fleet.py`` + the fleet key builders (EPA B5, F10).

The gate under test is the one thing every pre-existing limiter primitive
is not: **workspace-independent**. ``rate_key``/``semaphore_key`` put the
``workspace_id`` first precisely so two tenants never share a bucket;
``fleet_rate_key``/``fleet_semaphore_key`` deliberately omit it, because
the host does not see tenants — it sees one fleet. Every assertion below
is written across MULTIPLE workspace ids for that reason: a test that
used one workspace would pass just as happily against the old
per-workspace semaphore and prove nothing.

`fakeredis` is not a dependency of this repo (see `pyproject.toml` /
`uv.lock`), so per the repo's established convention
(`tests/unit/test_rate_limiter.py`, `tests/unit/test_access_budget.py`)
these run against a small hand-rolled in-memory double that recognizes
`bucket.py`'s two production Lua scripts by their marker comments and
runs a faithful Python transliteration of the same algorithm against real
Redis command semantics. That exercises `admit_fleet`/`release_fleet`'s
real call contract end-to-end (register-once, explicit `client=`
override, KEYS/ARGV shape, server-clock refill, TTL-based slot reclaim)
with no Docker daemon and no live server. Set `CRAWMATIC_TEST_REDIS_URL`
to run the same assertions against a real ephemeral Redis.
"""

from __future__ import annotations

import math
import os
import uuid
from typing import Any

import pytest

from app_shared.enums import AccessMethod
from app_shared.limiter.fleet import (
    FleetLimits,
    FleetTransport,
    admit_fleet,
    fleet_snapshot,
    fleet_transport_for,
    prime_fleet_limits_cache,
    release_fleet,
    reset_fleet_limits_cache,
    resolve_fleet_limits,
)
from app_shared.limiter.keys import (
    fleet_rate_key,
    fleet_semaphore_key,
    rate_key,
    semaphore_key,
)

DOMAIN = "amazon.sa"


# --- test doubles ------------------------------------------------------------


def _script_method_name(script_src: str) -> str:
    if "SPEC-11 T010" in script_src:
        return "_run_token_bucket"
    if "SPEC-11 T011" in script_src:
        return "_run_semaphore_acquire"
    raise ValueError(f"unrecognized Lua script: {script_src[:60]!r}")


class _FakeScript:
    """Stand-in for `redis.commands.core.Script`, dispatching by name on
    whatever `client=` is passed at call time (redis-py's own `client`
    override, which `bucket.py`'s register-once cache relies on)."""

    def __init__(self, method_name: str) -> None:
        self._method_name = method_name

    def __call__(self, keys: Any = None, args: Any = None, client: Any = None) -> Any:
        if client is None:
            raise AssertionError("test double invoked without an explicit client= override")
        return getattr(client, self._method_name)(list(keys or []), list(args or []))


class _FakeRedis:
    """In-memory stand-in for the `redis.Redis` subset `bucket.py` uses:
    `register_script`, a hash per token bucket, a sorted set per
    semaphore, `zrem` (release) and `zcount` (snapshot). `advance`
    stands in for the Redis server clock the real Lua reads via `TIME`."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self._now = now
        self.hashes: dict[str, dict[str, str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.pexpire_ms: dict[str, int] = {}

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def register_script(self, script_src: str) -> _FakeScript:
        return _FakeScript(_script_method_name(script_src))

    def zrem(self, key: str, token: str) -> None:
        zset = self.zsets.get(key)
        if zset is not None:
            zset.pop(token, None)

    def zcount(self, key: str, minimum: Any, maximum: Any) -> int:
        zset = self.zsets.get(key, {})
        low = float(minimum)
        return sum(1 for score in zset.values() if score >= low)

    def time(self) -> tuple[int, int]:
        """The Redis server clock, `(seconds, microseconds)` — what the
        real `TIME` command returns and what the semaphore's member
        scores are written from."""
        return (int(self._now), int((self._now % 1) * 1_000_000))

    def _run_token_bucket(self, keys: list[Any], args: list[Any]) -> list[int]:
        key = keys[0]
        capacity = float(args[0])
        ttl_ms = int(args[1])
        now = self._now

        bucket = self.hashes.get(key)
        if bucket is None:
            tokens, ts = capacity, now
        else:
            tokens, ts = float(bucket["tokens"]), float(bucket["ts"])

        tokens = min(capacity, tokens + (now - ts) * capacity / 60)

        if tokens >= 1:
            tokens -= 1
            self.hashes[key] = {"tokens": str(tokens), "ts": str(now)}
            self.pexpire_ms[key] = ttl_ms
            return [1, 0]

        self.hashes[key] = {"tokens": str(tokens), "ts": str(now)}
        self.pexpire_ms[key] = ttl_ms
        return [0, math.ceil((1 - tokens) * 60 / capacity)]

    def _run_semaphore_acquire(self, keys: list[Any], args: list[Any]) -> int:
        key = keys[0]
        limit = int(args[0])
        token = args[1]
        slot_ttl_seconds = float(args[2])
        key_ttl_ms = int(args[3])
        now = self._now

        zset = self.zsets.setdefault(key, {})
        for member in [m for m, score in zset.items() if score <= now]:
            del zset[member]

        if len(zset) < limit:
            zset[token] = now + slot_ttl_seconds
            self.pexpire_ms[key] = key_ttl_ms
            return 1
        return 0


class _BrokenRedis:
    """Every script execution raises — a Redis outage during acquire."""

    def register_script(self, script_src: str) -> _FakeScript:
        return _FakeScript(_script_method_name(script_src))

    def zrem(self, key: str, token: str) -> None:
        raise ConnectionError("redis unavailable")

    def zcount(self, key: str, minimum: Any, maximum: Any) -> int:
        raise ConnectionError("redis unavailable")

    def time(self) -> tuple[int, int]:
        raise ConnectionError("redis unavailable")

    def _run_token_bucket(self, keys: list[Any], args: list[Any]) -> Any:
        raise ConnectionError("redis unavailable")

    def _run_semaphore_acquire(self, keys: list[Any], args: list[Any]) -> Any:
        raise ConnectionError("redis unavailable")


def _redis_client() -> Any:
    """The in-memory fake, unless a real Redis is EXPLICITLY offered via
    `CRAWMATIC_TEST_REDIS_URL` (deliberately not `REDIS_URL`, which
    `tests/conftest.py` points at a dead port for every unit session)."""
    url = os.environ.get("CRAWMATIC_TEST_REDIS_URL")
    if url:
        import redis as redis_pkg

        return redis_pkg.Redis.from_url(url)
    return _FakeRedis()


class _Settings:
    FLEET_HOST_CONCURRENCY_DEFAULT = 6
    FLEET_HOST_RATE_PER_MINUTE_DEFAULT = 90
    FLEET_LEASE_TTL_SECONDS = 120


# --- key shape ---------------------------------------------------------------


def test_fleet_keys_carry_no_workspace_segment() -> None:
    """The defining property: two workspaces build the SAME fleet key for
    one domain, while the tenant keys they also build stay distinct."""
    ws_a, ws_b = uuid.uuid4(), uuid.uuid4()

    assert fleet_rate_key(DOMAIN, FleetTransport.HTTP) == "fleet:rate:amazon.sa:HTTP"
    assert fleet_semaphore_key(DOMAIN, FleetTransport.HTTP) == "fleet:semaphore:amazon.sa:HTTP"

    for ws in (ws_a, ws_b):
        assert str(ws) not in fleet_rate_key(DOMAIN, FleetTransport.HTTP)
        assert str(ws) not in fleet_semaphore_key(DOMAIN, FleetTransport.HTTP)

    # ...and the pre-existing tenant keys are still per-workspace.
    assert rate_key(ws_a, DOMAIN, AccessMethod.DIRECT_HTTP) != rate_key(
        ws_b, DOMAIN, AccessMethod.DIRECT_HTTP
    )
    assert semaphore_key(ws_a, DOMAIN, AccessMethod.DIRECT_HTTP) != semaphore_key(
        ws_b, DOMAIN, AccessMethod.DIRECT_HTTP
    )


def test_a_string_transport_and_the_enum_address_the_same_key() -> None:
    assert fleet_rate_key(DOMAIN, "http") == fleet_rate_key(DOMAIN, FleetTransport.HTTP)
    assert fleet_semaphore_key(DOMAIN, "browser") == fleet_semaphore_key(
        DOMAIN, FleetTransport.BROWSER
    )


@pytest.mark.parametrize(
    ("access_method", "expected"),
    [
        (AccessMethod.DIRECT_HTTP, FleetTransport.HTTP),
        (AccessMethod.DIRECT_HTTP_RETRY, FleetTransport.HTTP),
        (AccessMethod.PROXY_HTTP, FleetTransport.HTTP),
        (AccessMethod.PLAYWRIGHT_DIRECT, FleetTransport.BROWSER),
        (AccessMethod.PLAYWRIGHT_PROXY, FleetTransport.BROWSER),
        (None, FleetTransport.HTTP),
    ],
)
def test_transport_classes_collapse_proxied_and_direct_http(
    access_method: Any, expected: FleetTransport
) -> None:
    """A host cannot tell DIRECT_HTTP from PROXY_HTTP, so they MUST share
    one ceiling — otherwise escalating transport doubles the fleet's own
    limit, which is the failure this gate exists to prevent."""
    assert fleet_transport_for(access_method) is expected


# --- concurrency: the fleet ceiling is shared across workspaces --------------


def test_third_workspace_is_refused_when_fleet_concurrency_is_two() -> None:
    """THE acceptance criterion: concurrency 2, three DIFFERENT workspaces,
    one domain -- the third simultaneous acquire returns ``None``."""
    redis = _redis_client()
    workspaces = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]
    leases = []

    for _workspace_id in workspaces:
        leases.append(
            admit_fleet(
                redis,
                domain=DOMAIN,
                transport=FleetTransport.HTTP,
                rate_per_minute=90,
                concurrency=2,
                lease_ttl=120,
            )
        )

    assert leases[0] is not None
    assert leases[1] is not None
    assert leases[2] is None, "the third workspace must be refused by the FLEET semaphore"
    assert leases[0].token != leases[1].token
    assert leases[0].semaphore_key == leases[1].semaphore_key


def test_releasing_one_lease_admits_the_waiting_workspace() -> None:
    redis = _redis_client()
    first = admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1, lease_ttl=120,
    )
    assert first is not None
    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1, lease_ttl=120,
    ) is None

    release_fleet(redis, first)

    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1, lease_ttl=120,
    ) is not None


def test_browser_and_http_hold_independent_fleet_ceilings() -> None:
    redis = _redis_client()
    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1, lease_ttl=120,
    ) is not None
    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1, lease_ttl=120,
    ) is None
    # A browser navigation is a different animal and a different key.
    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.BROWSER,
        rate_per_minute=90, concurrency=1, lease_ttl=120,
    ) is not None


def test_two_domains_never_share_a_fleet_ceiling() -> None:
    redis = _redis_client()
    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1, lease_ttl=120,
    ) is not None
    assert admit_fleet(
        redis, domain="noon.com", transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1, lease_ttl=120,
    ) is not None


# --- crash recovery: the lease expires ---------------------------------------


def test_a_lease_whose_holder_never_releases_frees_after_lease_ttl() -> None:
    """Crash recovery IS lease expiry -- no reaper, no heartbeat. The
    semaphore Lua purges expired members on the very next acquire."""
    redis = _redis_client()
    if not isinstance(redis, _FakeRedis):  # pragma: no cover - real-Redis opt-in
        pytest.skip("clock control requires the in-memory double")

    held = admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1, lease_ttl=30,
    )
    assert held is not None  # ...and it is then NEVER released (the holder crashed).

    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1, lease_ttl=30,
    ) is None

    redis.advance(31)

    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1, lease_ttl=30,
    ) is not None, "an abandoned lease must self-free after lease_ttl"


# --- rate: the 91st request in a minute is refused, whoever asks -------------


def test_the_91st_request_in_a_minute_is_refused_regardless_of_workspace() -> None:
    """Concurrency is set high and every lease released immediately, so the
    ONLY gate that can refuse here is the fleet token bucket. Each acquire
    is attributed to a different workspace to prove the bucket is shared."""
    redis = _redis_client()
    if not isinstance(redis, _FakeRedis):  # pragma: no cover - real-Redis opt-in
        pytest.skip("a frozen clock is required to pin the 91st request")

    granted = 0
    for _ in range(90):
        _workspace_id = uuid.uuid4()  # a different tenant every single time
        lease = admit_fleet(
            redis, domain=DOMAIN, transport=FleetTransport.HTTP,
            rate_per_minute=90, concurrency=1000, lease_ttl=120,
        )
        assert lease is not None
        granted += 1
        release_fleet(redis, lease)

    assert granted == 90
    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1000, lease_ttl=120,
    ) is None, "the 91st request in the same minute must be refused"

    # ...and the bucket refills on the server clock, so a later minute works.
    redis.advance(60)
    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=1000, lease_ttl=120,
    ) is not None


def test_a_rate_refusal_never_consumes_a_concurrency_slot() -> None:
    redis = _redis_client()
    if not isinstance(redis, _FakeRedis):  # pragma: no cover - real-Redis opt-in
        pytest.skip("a frozen clock is required")

    for _ in range(2):
        lease = admit_fleet(
            redis, domain=DOMAIN, transport=FleetTransport.HTTP,
            rate_per_minute=2, concurrency=10, lease_ttl=120,
        )
        assert lease is not None
        release_fleet(redis, lease)

    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=2, concurrency=10, lease_ttl=120,
    ) is None
    assert redis.zsets.get(fleet_semaphore_key(DOMAIN, FleetTransport.HTTP)) in (None, {})


# --- fail-closed + release hygiene -------------------------------------------


def test_a_redis_outage_refuses_admission_rather_than_raising() -> None:
    assert admit_fleet(
        _BrokenRedis(), domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=6, lease_ttl=120,
    ) is None


def test_release_is_none_tolerant_idempotent_and_never_raises() -> None:
    redis = _redis_client()
    release_fleet(redis, None)

    lease = admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=90, concurrency=6, lease_ttl=120,
    )
    assert lease is not None
    release_fleet(redis, lease)
    release_fleet(redis, lease)  # double release is a no-op

    release_fleet(_BrokenRedis(), lease)  # a Redis outage during release never raises


def test_a_floor_of_one_means_a_zero_override_is_never_unlimited() -> None:
    redis = _redis_client()
    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=0, concurrency=0, lease_ttl=0,
    ) is not None
    assert admit_fleet(
        redis, domain=DOMAIN, transport=FleetTransport.HTTP,
        rate_per_minute=0, concurrency=0, lease_ttl=0,
    ) is None


# --- snapshot (denial REASONS only) ------------------------------------------


def test_snapshot_counts_live_leases_and_survives_a_redis_outage() -> None:
    redis = _redis_client()
    for _ in range(2):
        assert admit_fleet(
            redis, domain=DOMAIN, transport=FleetTransport.HTTP,
            rate_per_minute=90, concurrency=6, lease_ttl=120,
        ) is not None

    snap = fleet_snapshot(redis, domain=DOMAIN, transport=FleetTransport.HTTP, concurrency=6)
    assert (snap.in_flight, snap.concurrency) == (2, 6)

    broken = fleet_snapshot(
        _BrokenRedis(), domain=DOMAIN, transport=FleetTransport.HTTP, concurrency=6
    )
    assert broken.in_flight == 0


# --- per-domain overrides from `domain_rules` --------------------------------


class _Row(tuple):
    pass


class _Result:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def first(self) -> tuple | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[tuple]:
        return list(self._rows)


class _FakeSession:
    """Counts queries so the cache's effect is observable."""

    def __init__(self, rows: list[tuple]) -> None:
        self.rows = rows
        self.queries = 0

    def execute(self, _statement: Any) -> _Result:
        self.queries += 1
        return _Result(self.rows)

    def __enter__(self) -> "_FakeSession":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False


@pytest.fixture(autouse=True)
def _clean_fleet_limits_cache() -> Any:
    reset_fleet_limits_cache()
    yield
    reset_fleet_limits_cache()


def test_no_session_factory_resolves_to_the_settings_defaults() -> None:
    limits = resolve_fleet_limits(DOMAIN, settings=_Settings())
    assert limits == FleetLimits(concurrency=6, rate_per_minute=90)


def test_a_domain_rules_row_overrides_the_defaults_and_is_then_cached() -> None:
    session = _FakeSession([(2, 30)])
    limits = resolve_fleet_limits(DOMAIN, settings=_Settings(), session_factory=lambda: session)
    assert limits == FleetLimits(concurrency=2, rate_per_minute=30)
    assert session.queries == 1

    again = resolve_fleet_limits(DOMAIN, settings=_Settings(), session_factory=lambda: session)
    assert again == limits
    assert session.queries == 1, "a cached generation must not re-query"


def test_null_columns_mean_use_the_setting_never_unlimited() -> None:
    session = _FakeSession([(None, None)])
    limits = resolve_fleet_limits(DOMAIN, settings=_Settings(), session_factory=lambda: session)
    assert limits == FleetLimits(concurrency=6, rate_per_minute=90)


def test_a_database_error_degrades_to_the_defaults_rather_than_raising() -> None:
    class _BrokenSession:
        def __enter__(self) -> "_BrokenSession":
            raise RuntimeError("database unavailable")

        def __exit__(self, *exc: Any) -> bool:
            return False

    limits = resolve_fleet_limits(
        DOMAIN, settings=_Settings(), session_factory=lambda: _BrokenSession()
    )
    assert limits == FleetLimits(concurrency=6, rate_per_minute=90)


def test_priming_warms_every_domain_in_one_query_including_the_unlisted_ones() -> None:
    """`load_targets` primes the cache so the reactor-thread hot path is a
    dict lookup. A domain with no `domain_rules` row must ALSO be cached
    (as the defaults), or it would re-query on every single request."""
    session = _FakeSession([(DOMAIN, 3, 45)])
    prime_fleet_limits_cache(session, {DOMAIN, "noon.com", ""}, settings=_Settings())
    assert session.queries == 1

    assert resolve_fleet_limits(DOMAIN, settings=_Settings()) == FleetLimits(
        concurrency=3, rate_per_minute=45
    )
    assert resolve_fleet_limits("noon.com", settings=_Settings()) == FleetLimits(
        concurrency=6, rate_per_minute=90
    )


def test_priming_with_no_domains_issues_no_query() -> None:
    session = _FakeSession([])
    prime_fleet_limits_cache(session, set(), settings=_Settings())
    assert session.queries == 0
