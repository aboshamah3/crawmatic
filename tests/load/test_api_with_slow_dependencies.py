"""F15 (B7) load test: `-m load` (PLAN §7.4/F15, `apps/api/app/rate_limit.py`).

200 concurrent `/v1/products`-shaped requests against a Python "slow
Redis" stub (accepts every call, delays 3s before replying) and asserts:

- p95 latency stays under 2s (the middleware's own `asyncio.wait_for`
  deadline is 300ms, so 200 requests admitted CONCURRENTLY -- not
  serialized behind one blocking client -- should resolve in roughly
  one deadline's worth of wall-clock time, not 200x it);
- zero 5xx responses (an event-loop stall under a synchronous Redis
  client used to be able to starve unrelated requests into timeouts/
  errors; this middleware's Redis client is fully async).

Per the task's own note ("a local stub test; it must not touch a real
Redis or a real database"), this never opens a socket to anything: the
"slow Redis" is an in-process async double (same hand-rolled-fake
convention as `tests/unit/test_rate_limit_async.py` -- `fakeredis` is
not a dependency anywhere in this repo) and `/v1/products` is a
minimal, self-contained route standing in for the real router (which
needs a live Postgres) -- with `statement_timeout` conceptually
"forced" by mirroring it as a bounded async sleep well inside the 2s
budget, so a real, slow downstream call cannot mask a middleware-level
regression by making every path equally slow.

No `pytest-asyncio` in this environment (project convention, see
`tests/unit/test_rate_limiter.py`) -- the concurrent body runs under a
plain sync test via `asyncio.run`, using `httpx.AsyncClient` +
`ASGITransport` so requests are genuinely concurrent on one event loop
rather than serialized behind Starlette's synchronous `TestClient`.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.rate_limit import RateLimitMiddleware

pytestmark = pytest.mark.load

CONCURRENT_REQUESTS = 200
P95_BUDGET_SECONDS = 2.0
#: Stands in for "Postgres with statement_timeout forced": a bounded
#: async delay comfortably inside the p95 budget, so the route itself
#: never accounts for the bulk of the latency being measured.
_SIMULATED_DB_LATENCY_SECONDS = 0.05
#: The slow-Redis stub's reply delay -- well past the middleware's own
#: 300ms admission deadline, so every request must fail open to pass.
_SLOW_REDIS_DELAY_SECONDS = 3.0


class _SlowRedisScript:
    """Accepts every call; never resolves before the middleware's own
    `asyncio.wait_for` cancels it (asserts the *middleware*, not this
    stub, enforces the deadline)."""

    async def __call__(self, keys=None, args=None):
        await asyncio.sleep(_SLOW_REDIS_DELAY_SECONDS)
        return 1  # pragma: no cover - never reached before cancellation


class _SlowRedis:
    def register_script(self, _script_src: str) -> _SlowRedisScript:
        return _SlowRedisScript()


def _build_app() -> FastAPI:
    application = FastAPI()
    application.add_middleware(
        RateLimitMiddleware,
        redis_factory=lambda: _SlowRedis(),
        read_per_minute=CONCURRENT_REQUESTS * 10,  # not what this test measures
        write_per_minute=CONCURRENT_REQUESTS * 10,
        enabled=True,
    )

    @application.get("/v1/products")
    async def _products():
        # Stands in for a Postgres call under statement_timeout -- see
        # the module docstring.
        await asyncio.sleep(_SIMULATED_DB_LATENCY_SECONDS)
        return {"items": []}

    return application


async def _fire_concurrent_requests(app: FastAPI, count: int) -> list[tuple[int, float]]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:

        async def _one() -> tuple[int, float]:
            start = time.monotonic()
            response = await client.get(
                "/v1/products",
                headers={"Authorization": f"Bearer ck_load-{id(asyncio.current_task())}"},
            )
            return response.status_code, time.monotonic() - start

        return await asyncio.gather(*(_one() for _ in range(count)))


def _p95(latencies: list[float]) -> float:
    ordered = sorted(latencies)
    index = max(0, int(round(0.95 * (len(ordered) - 1))))
    return ordered[index]


def test_two_hundred_concurrent_requests_fail_open_under_p95_budget_no_5xx():
    results = asyncio.run(_fire_concurrent_requests(_build_app(), CONCURRENT_REQUESTS))

    statuses = [status for status, _elapsed in results]
    latencies = [elapsed for _status, elapsed in results]

    server_errors = [status for status in statuses if status >= 500]
    assert server_errors == [], f"{len(server_errors)} 5xx responses under load"

    p95_latency = _p95(latencies)
    assert p95_latency < P95_BUDGET_SECONDS, (
        f"p95 latency {p95_latency:.3f}s exceeded the {P95_BUDGET_SECONDS}s budget "
        "-- concurrent admission checks may be serializing rather than failing "
        "open independently"
    )
