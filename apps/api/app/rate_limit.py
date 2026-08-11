"""Per-credential rate limiting for the public API (PLAN §7.4, risk P5).

A fixed window, not a token bucket: the contract we publish is "60 reads
and 10 writes per minute", and a fixed window is the one algorithm whose
429 a customer can reason about without reading our source. The window
key embeds the wall-clock minute, so the counter expires by itself and
there is no sweeper.

**Identity** is `sha256(credential)`, never the credential: rate-limit
keys reach Redis, logs, and metrics, and an API key in any of those is
an API key leak. Requests with no bearer credential are not limited here
— they are about to fail auth anyway, and spending a Redis round-trip on
them would make unauthenticated traffic cheaper to amplify, not harder.

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

Exempt paths: `/health` (liveness must never depend on Redis) and
`/v1/auth/*` (already limited, per-account and per-IP, by
`app_shared.security.rate_limit.check_and_increment_login`).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Awaitable, Callable

from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app_shared.redis_client import get_redis_client

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXEMPT_PREFIXES = ("/health", "/v1/auth")
_BEARER = "Bearer "


def is_write(method: str) -> bool:
    """Budget class for an HTTP method."""
    return method.upper() in _WRITE_METHODS


def rate_limit_identity(request: object) -> str | None:
    """`sha256` of the bearer credential, or `None` if there is none.

    Never returns the credential itself — see the module docstring.
    """
    authorization = request.headers.get("Authorization")
    if not authorization or not authorization.startswith(_BEARER):
        return None
    credential = authorization[len(_BEARER) :].strip()
    if not credential:
        return None
    return hashlib.sha256(credential.encode()).hexdigest()[:32]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-credential limiter. 429 + `Retry-After` on refusal."""

    def __init__(
        self,
        app,
        *,
        redis_factory: Callable[[], object] = get_redis_client,
        read_per_minute: int | None = None,
        write_per_minute: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        super().__init__(app)
        self._redis_factory = redis_factory
        self._read_per_minute = read_per_minute
        self._write_per_minute = write_per_minute
        self._enabled = enabled

    def _limits(self) -> tuple[bool, int, int]:
        """Resolve (enabled, read_limit, write_limit).

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
            )
        try:
            from app_shared.config import get_settings

            settings = get_settings()
            return (
                settings.API_RATE_LIMIT_ENABLED,
                settings.API_RATE_LIMIT_READ_PER_MINUTE,
                settings.API_RATE_LIMIT_WRITE_PER_MINUTE,
            )
        except Exception:  # noqa: BLE001 - fail-open, see module docstring
            logger.warning(
                "rate limiter settings unavailable; disabling limiter",
                exc_info=True,
            )
            return (False, 0, 0)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        enabled, read_limit, write_limit = self._limits()
        path = request.url.path

        if not enabled or path.startswith(_EXEMPT_PREFIXES):
            return await call_next(request)

        identity = rate_limit_identity(request)
        if identity is None:
            return await call_next(request)

        write = is_write(request.method)
        limit = write_limit if write else read_limit
        bucket = "w" if write else "r"
        now = int(time.time())
        window = now // WINDOW_SECONDS
        key = f"rl:api:{bucket}:{identity}:{window}"
        retry_after = WINDOW_SECONDS - (now % WINDOW_SECONDS) or WINDOW_SECONDS

        try:
            redis_client = self._redis_factory()
            count = redis_client.incr(key)
            if count == 1:
                redis_client.expire(key, WINDOW_SECONDS * 2)
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
