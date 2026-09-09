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

Exempt paths: `/health` (liveness must never depend on Redis),
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

CONFIG NOTE: `API_RATE_LIMIT_MAX_KEYS` is read from `os.environ`
(default `100_000`) rather than `app_shared.config.Settings` — B7 ran
in parallel with another worker holding `config.py`. It belongs there
as a typed setting; this is a placeholder until that consolidation
lands (see `reports/B7.md`).
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

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXEMPT_PREFIXES = ("/health", "/v1/auth", "/v1/admin")
_BEARER = "Bearer "

#: Bound on distinct unauthenticated (IP-keyed) admission buckets. A
#: caller rotating source IPs cannot grow the Redis key space past this
#: many live keys — collisions just mean two IPs briefly share a budget,
#: which is strictly safer than an unbounded key space under attack.
#: See the CONFIG NOTE in the module docstring for why this is read from
#: `os.environ` rather than `Settings`.
API_RATE_LIMIT_MAX_KEYS = int(os.environ.get("API_RATE_LIMIT_MAX_KEYS", "100000"))

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

#: Atomic INCR + EXPIRE-on-first-hit. One round trip, one script call
#: (asserted by the fake Redis double in tests/unit/test_rate_limit_async.py)
#: instead of two separate commands that could race under concurrent
#: callers sharing a key.
_INCR_AND_EXPIRE_ON_FIRST_HIT_LUA = """
local current = redis.call('INCR', KEYS[1])
if tonumber(current) == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
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


def _client_ip(request: object) -> str:
    """Best-effort client IP for the unauthenticated admission bucket.

    Reads `request.client.host` (the ASGI transport's peer address).
    Does not honour `X-Forwarded-For`/`X-Real-Ip` -- see Notes for
    reviewer in `reports/B7.md`: this is an admission-control bucket
    key, not a security identity, so a spoofable header here only lets
    an attacker choose which bucket their own unauthenticated traffic
    lands in, not bypass the limiter or attribute traffic to someone
    else.
    """
    client = getattr(request, "client", None)
    host = getattr(client, "host", None) if client is not None else None
    return host or "unknown"


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


def _admission_key(request: Request, settings: object) -> tuple[str, str]:
    """Resolve the (bucket_kind, key_fragment) pair used to build the Redis key.

    - No bearer credential: bounded-cardinality IP bucket.
    - An API-key credential (`ck_...`): unchanged pre-F15 behaviour --
      `sha256(credential)` -- see the module docstring for why this is
      NOT resolved to a workspace here.
    - Any other (JWT) credential: the verified `workspace_id` claim when
      present, else the same credential-hash fallback.
    """
    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith(_BEARER):
        return ("ip", _ip_bucket(_client_ip(request)))

    credential = authorization[len(_BEARER) :].strip()
    if not credential:
        return ("ip", _ip_bucket(_client_ip(request)))

    try:
        from app_shared.security.api_keys import API_KEY_PREFIX
    except Exception:  # noqa: BLE001 - fail-open, see module docstring
        API_KEY_PREFIX = "ck_"  # noqa: N806 - local fallback constant

    identity = rate_limit_identity(request)
    if credential.startswith(API_KEY_PREFIX) or identity is None:
        return ("credential", identity or _ip_bucket(_client_ip(request)))

    workspace_id = _workspace_from_verified_jwt(credential, settings)
    if workspace_id is not None:
        return ("workspace", workspace_id)
    return ("credential", identity)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window admission limiter. 429 + `Retry-After` on refusal.

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

    async def _incr_and_expire(self, key: str, ttl_seconds: int) -> int:
        """One atomic INCR+EXPIRE-on-first-hit call, deadline-bounded."""
        redis_client = self._redis_factory()
        script = _get_incr_and_expire_script(redis_client)
        coro = script(keys=[key], args=[ttl_seconds])
        return await asyncio.wait_for(coro, timeout=_REDIS_ADMISSION_DEADLINE_SECONDS)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        enabled, read_limit, write_limit, settings = self._limits()
        path = request.url.path

        if not enabled or path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        _kind, key_fragment = _admission_key(request, settings)

        write = is_write(request.method)
        limit = write_limit if write else read_limit
        bucket = "w" if write else "r"
        now = int(time.time())
        window = now // WINDOW_SECONDS
        key = f"rl:api:{bucket}:{key_fragment}:{window}"
        retry_after = WINDOW_SECONDS - (now % WINDOW_SECONDS) or WINDOW_SECONDS

        try:
            count = await self._incr_and_expire(key, WINDOW_SECONDS * 2)
        except Exception:  # noqa: BLE001 - fail-open, see module docstring
            logger.warning("rate limiter unavailable; allowing request", exc_info=True)
            return await call_next(request)

        if count > limit:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={
                    "error": {
                        "code": "RATE_LIMITED",
                        "message": (
                            f"Rate limit exceeded: {limit} "
                            f"{'write' if write else 'read'} requests per minute."
                        ),
                    },
                    "detail": {
                        "error": {
                            "code": "RATE_LIMITED",
                            "message": "Too many requests.",
                        }
                    },
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(max(0, limit - count))
        return response
