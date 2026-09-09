"""Per-credential rate limiting for the public API (PLAN §7.4, risk P5).

A fixed window, not a token bucket: the contract we publish is "60 reads
and 10 writes per minute", and a fixed window is the one algorithm whose
429 a customer can reason about without reading our source. The window
key embeds the wall-clock minute, so the counter expires by itself and
there is no sweeper.

**Identity** is `sha256(credential)`, never the credential — rate-limit
keys reach Redis, logs, and metrics, and an API key in any of those is
an API key leak.

F15 (EPA core-production-readiness, 2026-09-07) reworked two things:

1. **Non-blocking Redis.** The counter used to go through the
   process-wide *synchronous* `redis.Redis` (`app_shared.redis_client`)
   inside an `async def dispatch` — a blocking socket call on the
   event loop that can stall every other in-flight request while Redis
   is slow. This module now owns its own `redis.asyncio.Redis`,
   connected straight from `REDIS_URL` with a tight
   `socket_connect_timeout`/`socket_timeout` (0.25s each — this is an
   admission check, not a durable write, so waiting is strictly worse
   than admitting), and bounds the *whole* Redis round trip with
   `asyncio.wait_for` so a wedged connection fails open within 300ms
   instead of hanging the request (and the event loop) indefinitely.
2. **Bounded-cardinality admission for every caller, not just
   credentialed ones.** Unauthenticated requests used to be exempted
   outright ("about to fail auth anyway"). They still fail auth, but an
   unauthenticated caller can still cheaply hammer an expensive route
   before the 401 is even resolved, so they are now admission-controlled
   by client IP, hashed into a fixed-size bucket space
   (`API_RATE_LIMIT_MAX_KEYS`, default 100_000) so an attacker rotating
   IPs cannot make the Redis key space grow without bound. Authenticated
   traffic with a verifiable workspace (a JWT whose signature checks out
   and carries a `workspace_id` claim) is keyed by that workspace, so
   every credential a workspace holds shares one budget rather than each
   getting its own. An API-key credential's *actual* workspace is only
   known after the full DB-backed lookup in `app.deps` — duplicating
   that lookup here, on every request, purely for admission control
   would add a mandatory DB round trip to the hot path and a second,
   divergence-prone place that verifies credentials, so API-key traffic
   keeps the pre-F15 behaviour of one budget per credential hash (a
   strictly *smaller*, not weaker, bucket than "per workspace" would be:
   several keys in one workspace get separate budgets instead of a
   shared one). See `reports/B7.md` for the fuller rationale.

**Fail-open on Redis errors.** The login limiter in
`app_shared.security.rate_limit` fails *closed* because it guards
credentials, where refusing everyone briefly is the safe default. This
one guards **cost**, and the real cost guards are elsewhere (the
per-workspace domain limit, the per-product protected-link cap, and the
direct-HTTP default for unknown domains — PLAN §7.4). Failing closed
here would turn a Redis blip into a total outage of a paid API, which is
a strictly worse failure than a minute of unmetered reads. The same
fail-open posture applies when `Settings` itself can't be constructed
(e.g. a test process with no `.env`): a middleware that guards cost has
no business turning a missing dev-config file into a 500 on every route
in the shared `app` object, so an unconstructable `Settings` disables
the limiter rather than raising through `dispatch`.

B4 (review + bounty, 2026-09-09) added the piece F15 was missing — **an
origin bound that every attempt pays BEFORE any credential is trusted**:

1. **A shape is not a possession.** F15 keyed a credentialed request by
   `sha256(credential)` *before anything had been looked up*, so a caller
   who could type `ck_` minted a fresh, empty, private bucket on every
   attempt. The reviewer drove 300 invented but well-formed bearer values
   from one address and got 300 buckets: the limiter was counting
   perfectly and bounding nothing, which is rate-limit-counter and
   authentication-work amplification (each of those requests still costs
   a full `app.deps` credential lookup before its 401).
   Every non-exempt request now first increments the bounded origin
   bucket (`ip:<n>`), and only *then* — if it presented a credential —
   its per-credential/workspace bucket. Both counters move in ONE Lua
   call (`KEYS` is a list now), so this is still one round trip bounded
   by one deadline — and the script STOPS at the first counter over its
   limit, so an attempt the origin bound already refused never creates
   the credential key it named. Refusing 300 invented values while still
   minting 300 counters would only move the amplification from the rate
   to the key space.
2. **The origin ceiling is DERIVED, never configured separately**:
   `limit * API_RATE_LIMIT_IP_FANOUT`. One number, one place to get
   wrong. The fan-out exists because one address may legitimately front
   several tenants (an agency host, a NAT), so bounding an origin at one
   tenant's budget would 429 honest paying traffic; at the shipped
   defaults (60 reads/min, fan-out 4) an origin is refused at 241, so the
   review's 300-value probe is bounded instead of admitted, and the most
   credential buckets it can mint per window collapses from unbounded to
   the ceiling.
3. **Anonymous traffic keeps the tighter allowance.** A caller with no
   credential has proven nothing, so the *same* origin counter is judged
   against the plain tenant limit for them — unchanged from F15 — and a
   refusal at the origin layer never names the ceiling (naming it hands a
   prober a free measurement); a refusal at the credential layer still
   names the limit, because that caller at least holds a credential.
4. **The origin address comes from `app.client_ip`**, the trusted-proxy
   resolver this codebase already has (W5.5-L1), NOT the transport peer.
   Behind Railway's edge the peer is the *edge*, which would have folded
   the whole internet into one origin bucket; reading the leftmost
   `X-Forwarded-For` entry instead would let a caller pick a fresh bucket
   per attempt. That module counts back from the RIGHT by the trusted hop
   count and falls back to the socket. Do not add a second IP-extraction
   path here.

Exempt paths: the probe endpoints by EXACT path (`/health`,
`/health/scraping`, `/live`, `/ready`) — liveness/readiness must never
depend on Redis, and probes must not consume a tenant's or an origin's
budget. Exact, not prefix: `_EXEMPT_PREFIXES` used to carry `/health`,
which also exempted every `/health…`-shaped 404 an attacker cared to
invent, i.e. an unmetered surface. The two remaining prefixes stay
prefixes because they are whole route trees:
`/v1/auth/*` (already limited, per-account and per-IP, by
`app_shared.security.rate_limit.check_and_increment_login`), and
`/v1/admin` (the SaaS control plane, gated by the shared
`SAAS_SERVICE_TOKEN` secret in `app.service_auth` rather than a
per-tenant credential -- it is not a customer surface, so this
tenant-cost limiter does not apply to it. Without this exemption the
SaaS's own billing/metering export (`GET /v1/admin/usage`) and
provisioning calls would be throttled at the same 60-read/10-write
budget as a single customer, and a backfill or a busy month would 429
the metering feed -- a silent revenue-loss path).

CONFIG NOTE: `API_RATE_LIMIT_MAX_KEYS` and `API_RATE_LIMIT_IP_FANOUT`
are read from `os.environ` (defaults `100_000` and `4`) rather than
`app_shared.config.Settings` — B7 ran in parallel with another worker
holding `config.py`, and B4 kept the same shape rather than editing a
file outside its packet. Both belong there as typed settings; this is a
placeholder until that consolidation lands (see `reports/B7.md`).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from collections.abc import Awaitable, Callable

import redis.asyncio as redis_asyncio
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.client_ip import client_ip as resolve_client_ip

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Whole route trees that this tenant-cost limiter does not police -- see
#: the module docstring. Prefixes, deliberately: every path under them is
#: a real route with its own gate.
_EXEMPT_PREFIXES = ("/v1/auth", "/v1/admin")

#: Liveness/readiness probes, matched EXACTLY (a trailing slash aside).
#: Exact rather than by prefix so that `/health<anything>` -- a 404 that
#: still costs a route lookup -- is metered like any other traffic
#: instead of being a free, unmetered surface. Keep in sync with
#: `apps/api/app/main.py` (`/health`), `routers/health.py` (`/live`,
#: `/health/scraping`) and `routers/ready.py` (`/ready`).
_EXEMPT_EXACT_PATHS = frozenset({"/health", "/health/scraping", "/live", "/ready"})

_BEARER = "Bearer "

#: Bound on distinct unauthenticated (IP-keyed) admission buckets. A
#: caller rotating source IPs cannot grow the Redis key space past this
#: many live keys — collisions just mean two IPs briefly share a budget,
#: which is strictly safer than an unbounded key space under attack.
#: See the CONFIG NOTE in the module docstring for why this is read from
#: `os.environ` rather than `Settings`.
API_RATE_LIMIT_MAX_KEYS = int(os.environ.get("API_RATE_LIMIT_MAX_KEYS", "100000"))

#: How much more traffic ONE ORIGIN may send in a window than one
#: credential may (B4). The origin ceiling is DERIVED from the tenant
#: limit -- `limit * API_RATE_LIMIT_IP_FANOUT` -- rather than configured
#: separately, so the two can never drift apart and a bad value degrades
#: one number, not the relationship between two.
#:
#: Four, not one and not a hundred. High enough that an address
#: legitimately fronting a few tenants (an agency host, a NAT, a
#: multi-store server) is never touched while each of them stays inside
#: its own budget; low enough that inventing credentials is not free --
#: at the shipped 60 reads/minute an origin is refused at 241, so the
#: review's 300-invented-value probe is bounded rather than admitted. A
#: single tenant saturating its own budget always hits its per-credential
#: bucket first, so this ceiling only ever bites a caller spreading
#: traffic across many credentials from one address, which is the shape
#: of the attack and not the shape of a customer.
#: Floored at 1: a fan-out below 1 would make the origin bound TIGHTER
#: than the tenant limit and 429 a single honest tenant.
API_RATE_LIMIT_IP_FANOUT = max(1, int(os.environ.get("API_RATE_LIMIT_IP_FANOUT", "4")))

#: Per admission check: connect/socket deadlines for the middleware's own
#: `redis.asyncio.Redis` (deliberately tighter than the general-purpose
#: client in `app_shared.redis_client` — this is an admission check that
#: must fail open fast, not a durable write).
_REDIS_CONNECT_TIMEOUT_SECONDS = 0.25
_REDIS_SOCKET_TIMEOUT_SECONDS = 0.25

#: Upper bound on the whole admission round trip (script call included).
#: Comfortably above the two socket deadlines above (which apply per
#: TCP operation, not to the logical call as a whole) so a slow-but-not-
#: dead connection still gets one full retry's worth of budget before
#: the request fails open.
_REDIS_ADMISSION_DEADLINE_SECONDS = 0.3

#: Atomic INCR + EXPIRE-on-first-hit, for EVERY key of the admission
#: check. One round trip, one script call (asserted by the fake Redis
#: double in tests/unit/test_rate_limit_async.py) instead of separate
#: commands that could race under concurrent callers sharing a key.
#:
#: B4 made `KEYS` a list: a credentialed request now moves two counters
#: (the origin bound, then its own credential/workspace bucket). Doing
#: that in one script keeps the "one round trip, one deadline" property
#: F15 established -- two sequential awaits would double the worst-case
#: latency this middleware can add and give the two counters two chances
#: to disagree about whether the request happened.
#:
#: **The limits are evaluated HERE, not only in Python, and the loop
#: stops at the first key over its limit.** That short-circuit is the
#: half of the fix that bounds the KEY SPACE rather than the request
#: rate: if the origin bound has already refused this attempt, touching
#: the caller's credential key would still CREATE it, and an attacker
#: sending 300 invented credentials would still mint 300 Redis keys —
#: refused, but present, which is the counter amplification the finding
#: is about. A refused attempt must leave no trace it chose the name of.
#:
#: `ARGV[1]` is the TTL; `ARGV[1 + i]` is the limit for `KEYS[i]`. The
#: reply is one count per key EVALUATED, so a short-circuited call
#: returns fewer counts than keys (see `_counts_from_script_result`). A
#: missing limit means "do not short-circuit here", never "refuse".
#:
#: SINGLE-INSTANCE REDIS. Passing two keys to one script is a CROSSSLOT
#: error on a Redis Cluster; this deployment's Redis is a single Railway
#: instance, and if that ever changes the two keys need a shared hash tag.
#: The failure mode meanwhile is the safe one: the error is caught by
#: `dispatch`'s fail-open handler and logged on every request.
_INCR_AND_EXPIRE_ON_FIRST_HIT_LUA = """
local ttl = tonumber(ARGV[1])
local counts = {}
for i = 1, #KEYS do
    local current = redis.call('INCR', KEYS[i])
    if tonumber(current) == 1 then
        redis.call('EXPIRE', KEYS[i], ttl)
    end
    counts[i] = current
    local limit = tonumber(ARGV[i + 1])
    if limit and current > limit then
        break
    end
end
return counts
"""

_async_redis_client: redis_asyncio.Redis | None = None


def get_async_admission_redis_client() -> redis_asyncio.Redis:
    """The per-process async Redis client used ONLY for admission control.

    Lazily built from `Settings.REDIS_URL` on first use (mirrors the
    lazy-singleton pattern in `app_shared.redis_client`/`app_shared.database`
    -- never at import time, never per request) with the tight timeouts
    documented in the module docstring.
    """
    global _async_redis_client
    if _async_redis_client is None:
        from app_shared.config import get_settings

        settings = get_settings()
        _async_redis_client = redis_asyncio.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=_REDIS_CONNECT_TIMEOUT_SECONDS,
            socket_timeout=_REDIS_SOCKET_TIMEOUT_SECONDS,
        )
    return _async_redis_client


def _get_incr_and_expire_script(redis_client):
    """Return the atomic INCR+EXPIRE script bound to `redis_client`.

    `register_script` never talks to the network in redis-py — the
    Script object it returns resolves EVALSHA/EVAL lazily at call time
    — so calling it per request is cheap. Deliberately NOT cached at
    module scope: a module-level cache would bind the script to
    whichever `redis_client` first created it (production's one
    long-lived client, but a *fresh* fake/double per test), silently
    reusing test A's fake inside test B.
    """
    return redis_client.register_script(_INCR_AND_EXPIRE_ON_FIRST_HIT_LUA)


def is_write(method: str) -> bool:
    """Budget class for an HTTP method."""
    return method.upper() in _WRITE_METHODS


def rate_limit_identity(request: object) -> str | None:
    """`sha256` of the bearer credential, or `None` if there is none.

    Never returns the credential itself — see the module docstring.
    Also consumed by `app.abuse_limit` for its own, independent,
    fail-closed per-credential keying; keep this function's contract
    (hash-or-None, no DB/Redis lookup) stable for that caller.
    """
    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith(_BEARER):
        return None
    credential = authorization[len(_BEARER) :].strip()
    if not credential:
        return None
    return hashlib.sha256(credential.encode()).hexdigest()[:32]


def _origin_bucket(request: object) -> str:
    """The bounded origin bucket this request is admitted against.

    B4: the address comes from `app.client_ip.client_ip`, the ONE
    trusted-proxy resolver in this codebase (W5.5-L1) -- not the ASGI
    transport peer this used to read. Two reasons, and B7's "it is only
    a bucket key, not an identity" answers neither of them now that the
    origin bound is the only thing standing between an attacker and an
    unbounded number of credential buckets:

    * the transport peer behind Railway's edge is the EDGE, so every
      caller on the internet would share one origin bucket and the bound
      would either be useless or refuse everyone;
    * reading the caller-written (leftmost) `X-Forwarded-For` entry
      instead would let an attacker pick a fresh origin bucket per
      attempt -- the exact bypass, one layer up.

    `client_ip` counts back from the RIGHT by the trusted hop count and
    falls back to the socket when the chain is absent or too short, so a
    spoofed prefix buys nothing. Do not add a second resolver here.
    """
    return _ip_bucket(resolve_client_ip(request))


def _ip_bucket(ip: str) -> str:
    """Hash `ip` into a fixed-size bucket space (`API_RATE_LIMIT_MAX_KEYS`).

    Bounds the Redis key space regardless of how many distinct source
    IPs an attacker rotates through -- a collision just means two IPs
    briefly share a budget.
    """
    digest = hashlib.sha256(ip.encode()).hexdigest()
    bucket = int(digest[:16], 16) % API_RATE_LIMIT_MAX_KEYS
    return f"ip:{bucket}"


def _workspace_from_verified_jwt(credential: str, settings: object) -> str | None:
    """A verified JWT's `workspace_id` claim, or `None`.

    Reuses `app_shared.security.jwt.decode_access_token` -- signature +
    `exp` verification, no DB/Redis call -- so this is safe and cheap to
    run on the admission-control hot path. Returns `None` on any
    decode/verification failure or a null `workspace_id` claim (e.g. a
    SUPER_ADMIN token not yet bound to a workspace); the caller falls
    back to the credential-hash bucket in that case.
    """
    try:
        from app_shared.security.jwt import decode_access_token

        claims = decode_access_token(
            credential, secret=settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM
        )
    except Exception:  # noqa: BLE001 - any decode failure just means "no claim"
        return None
    workspace_id = claims.get("workspace_id")
    return str(workspace_id) if workspace_id else None


def _admission_key(
    request: Request, settings: object, origin_fragment: str | None = None
) -> tuple[str, str]:
    """Resolve the (bucket_kind, key_fragment) pair used to build the Redis key.

    `origin_fragment` is the caller's already-resolved origin bucket, so
    `dispatch` -- which needs it anyway for the pre-auth bound -- does not
    pay for the proxy-chain walk twice. Resolved here when omitted.

    - No bearer credential: bounded-cardinality IP bucket.
    - An API-key credential (`ck_...`): unchanged pre-F15 behaviour --
      `sha256(credential)` -- see the module docstring for why this is
      NOT resolved to a workspace here.
    - Any other (JWT) credential: the verified `workspace_id` claim when
      present, else the same credential-hash fallback.
    """
    origin = _origin_bucket(request) if origin_fragment is None else origin_fragment

    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith(_BEARER):
        return ("ip", origin)

    credential = authorization[len(_BEARER) :].strip()
    if not credential:
        return ("ip", origin)

    try:
        from app_shared.security.api_keys import API_KEY_PREFIX
    except Exception:  # noqa: BLE001 - fail-open, see module docstring
        API_KEY_PREFIX = "ck_"  # noqa: N806 - local fallback constant

    identity = rate_limit_identity(request)
    if credential.startswith(API_KEY_PREFIX) or identity is None:
        return ("credential", identity or origin)

    workspace_id = _workspace_from_verified_jwt(credential, settings)
    if workspace_id is not None:
        return ("workspace", workspace_id)
    return ("credential", identity)


def is_exempt(path: str) -> bool:
    """Is `path` outside this limiter entirely? See the module docstring.

    Probe paths match EXACTLY (a single trailing slash tolerated, because
    that is what Starlette redirects rather than a route of its own);
    the two service route trees match by prefix.
    """
    normalised = path.rstrip("/") or "/"
    if path in _EXEMPT_EXACT_PATHS or normalised in _EXEMPT_EXACT_PATHS:
        return True
    return path.startswith(_EXEMPT_PREFIXES)


def _counts_from_script_result(result: object, expected: int) -> list[int]:
    """Normalise the Lua reply into one count per key, in key order.

    The script returns a Lua table (a list) with one count per key it
    actually evaluated -- FEWER than `expected` when it short-circuited
    on a key that was already over its limit. A scalar is still accepted
    so a test double written against the pre-B4 single-key protocol does
    not silently mis-report, and any count we did NOT get back is
    reported as 0 -- i.e. "no evidence", which admits. Safe in both
    cases: a short-circuit is always reported by an EARLIER count that is
    over its limit, so the padded zeros are never what decides the
    verdict, and otherwise this is the same fail-open posture the whole
    module takes -- a limiter that guards cost must not turn a surprising
    Redis reply into a 429 for a paying tenant.
    """
    if isinstance(result, (list, tuple)):
        counts = [int(value) for value in result]
    else:
        counts = [int(result)]  # type: ignore[arg-type]
    if len(counts) < expected:
        counts.extend([0] * (expected - len(counts)))
    return counts


def _rate_limited_response(
    *, limit: int | None, retry_after: int, write: bool
) -> JSONResponse:
    """The 429 body. `limit=None` means "do not disclose the ceiling".

    Disclosure follows what the caller has proven (B4). A refusal at the
    origin layer is anonymous -- retry-after only -- because naming the
    ceiling to a caller who has presented nothing hands them a free
    measurement of the bound they are probing. A refusal at the
    credential layer names the limit, unchanged: that caller holds a
    credential and needs to know its budget to back off correctly.
    """
    message = (
        "Too many requests."
        if limit is None
        else (
            f"Rate limit exceeded: {limit} "
            f"{'write' if write else 'read'} requests per minute."
        )
    )
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(retry_after)},
        content={
            "error": {"code": "RATE_LIMITED", "message": message},
            "detail": {
                "error": {
                    "code": "RATE_LIMITED",
                    "message": "Too many requests.",
                }
            },
        },
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window admission limiter. 429 + `Retry-After` on refusal.

    Two layers, in this order (B4): the bounded ORIGIN bound that every
    attempt pays before any credential is trusted, then the
    per-credential/workspace bucket for a caller that presented one. See
    the module docstring for why the order is the fix.

    Non-blocking: the Redis round trip runs on `redis.asyncio.Redis` and
    is bounded end-to-end by `asyncio.wait_for`, so a stalled Redis
    connection cannot stall this or any concurrent request past
    `_REDIS_ADMISSION_DEADLINE_SECONDS`.
    """

    def __init__(
        self,
        app,
        *,
        redis_factory: Callable[[], object] = get_async_admission_redis_client,
        read_per_minute: int | None = None,
        write_per_minute: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        super().__init__(app)
        self._redis_factory = redis_factory
        self._read_per_minute = read_per_minute
        self._write_per_minute = write_per_minute
        self._enabled = enabled

    def _limits(self) -> tuple[bool, int, int, object | None]:
        """Resolve (enabled, read_limit, write_limit, settings-or-None).

        Explicit constructor args win (tests). Otherwise fall back to
        `Settings` — and if `Settings` can't even be constructed (no
        `.env` in this process, a common shape for unit tests that
        import the shared `app` object without full config), treat the
        limiter as disabled rather than raising: see the module
        docstring's fail-open rationale.
        """
        if self._enabled is not None and self._read_per_minute is not None:
            return (
                self._enabled,
                self._read_per_minute,
                self._write_per_minute or self._read_per_minute,
                None,
            )
        try:
            from app_shared.config import get_settings

            settings = get_settings()
            return (
                settings.API_RATE_LIMIT_ENABLED,
                settings.API_RATE_LIMIT_READ_PER_MINUTE,
                settings.API_RATE_LIMIT_WRITE_PER_MINUTE,
                settings,
            )
        except Exception:  # noqa: BLE001 - fail-open, see module docstring
            logger.warning(
                "rate limiter settings unavailable; disabling limiter",
                exc_info=True,
            )
            return (False, 0, 0, None)

    async def _incr_and_expire(
        self, keys: list[str], limits: list[int], ttl_seconds: int
    ) -> list[int]:
        """One atomic INCR+EXPIRE-on-first-hit call for ALL keys.

        Still ONE script call and ONE deadline however many counters the
        request moves, and the script stops at the first key over its
        limit so a refused attempt never creates the keys after it --
        see `_INCR_AND_EXPIRE_ON_FIRST_HIT_LUA`.
        """
        redis_client = self._redis_factory()
        script = _get_incr_and_expire_script(redis_client)
        coro = script(keys=list(keys), args=[ttl_seconds, *limits])
        result = await asyncio.wait_for(coro, timeout=_REDIS_ADMISSION_DEADLINE_SECONDS)
        return _counts_from_script_result(result, len(keys))

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        enabled, read_limit, write_limit, settings = self._limits()
        path = request.url.path

        if not enabled or is_exempt(path):
            return await call_next(request)

        write = is_write(request.method)
        limit = write_limit if write else read_limit
        bucket = "w" if write else "r"
        now = int(time.time())
        window = now // WINDOW_SECONDS
        retry_after = WINDOW_SECONDS - (now % WINDOW_SECONDS) or WINDOW_SECONDS

        def _key(fragment: str) -> str:
            return f"rl:api:{bucket}:{fragment}:{window}"

        origin_fragment = _origin_bucket(request)
        kind, key_fragment = _admission_key(request, settings, origin_fragment)

        # LAYER 1 -- the pre-auth origin bound, paid by EVERY attempt
        # (missing header, junk, well-formed-but-invented, or the real
        # thing). This is the one an attacker cannot vary: the address
        # comes from the trusted-proxy resolver, and the bucket space is
        # bounded, so no amount of credential- or header-fiddling mints a
        # fresh bucket. A caller who has proven nothing is judged against
        # the plain tenant limit; a caller carrying a credential gets the
        # derived origin ceiling, because one address may legitimately
        # front several tenants.
        anonymous = kind == "ip"
        keys = [_key(origin_fragment)]
        limits = [limit if anonymous else limit * API_RATE_LIMIT_IP_FANOUT]
        # `disclosed`: may the refusal at this layer name its limit?
        disclosed = [False]

        # LAYER 2 -- the per-credential/workspace bucket, for a caller
        # that presented a credential. Unchanged from F15, and still the
        # bound that bites a busy honest tenant first. Skipped when it
        # would resolve to the origin key itself, so no request is ever
        # counted twice against one counter.
        if not anonymous and key_fragment != origin_fragment:
            keys.append(_key(key_fragment))
            limits.append(limit)
            disclosed.append(True)

        try:
            counts = await self._incr_and_expire(keys, limits, WINDOW_SECONDS * 2)
        except Exception:  # noqa: BLE001 - fail-open, see module docstring
            logger.warning("rate limiter unavailable; allowing request", exc_info=True)
            return await call_next(request)

        for count, layer_limit, names_limit in zip(counts, limits, disclosed):
            if count > layer_limit:
                return _rate_limited_response(
                    limit=layer_limit if names_limit else None,
                    retry_after=retry_after,
                    write=write,
                )

        response = await call_next(request)
        # The headers describe the caller's OWN budget -- the last layer,
        # which is their credential bucket when they have one. Reporting
        # the shared origin ceiling to a tenant would tell them a number
        # that other traffic can move underneath them.
        response.headers["X-RateLimit-Limit"] = str(limits[-1])
        response.headers["X-RateLimit-Remaining"] = str(max(0, limits[-1] - counts[-1]))
        return response
