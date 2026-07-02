"""Live login rate-limit: per-account + per-source backoff, fail-safe deny
when Redis is down (SPEC-03 T027) — ⏸ DEFERRED.

FR-007/SC-009.

Needs a reachable Redis (the ``noeviction`` instance per §4). Not
runnable in the no-Docker-daemon build environment used to author this
feature — SKIPS cleanly whenever ``Settings``/``REDIS_URL`` isn't usable
or a real connection attempt fails.
"""

from __future__ import annotations

import uuid

import pytest


def _redis_reachable() -> bool:
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception:
        return False

    if not settings.REDIS_URL:
        return False

    try:
        from app_shared.redis_client import get_redis_client

        get_redis_client().ping()
    except Exception:
        return False

    return True


pytestmark = pytest.mark.skipif(
    not _redis_reachable(),
    reason="No reachable Redis (REDIS_URL) configured in this environment",
)


@pytest.fixture()
def redis_client():
    from app_shared.redis_client import get_redis_client

    client = get_redis_client()
    yield client


def test_per_account_backoff_engages_after_threshold(redis_client) -> None:
    from app_shared.security.rate_limit import check_and_increment_login

    email = f"rl-acct-{uuid.uuid4().hex}@example.com"
    max_attempts = 5

    results = [
        check_and_increment_login(
            redis_client,
            email=email,
            source_ip=f"198.51.100.{i}",  # distinct source per attempt
            max_attempts=max_attempts,
            window_seconds=60,
        )
        for i in range(max_attempts + 1)
    ]

    assert all(r.allowed for r in results[:max_attempts])
    assert results[max_attempts].allowed is False


def test_per_source_backoff_engages_independently_of_account(redis_client) -> None:
    from app_shared.security.rate_limit import check_and_increment_login

    source_ip = f"203.0.113.{uuid.uuid4().int % 255}"
    max_attempts = 5

    results = [
        check_and_increment_login(
            redis_client,
            email=f"user-{i}@example.com",  # distinct account per attempt
            source_ip=source_ip,
            max_attempts=max_attempts,
            window_seconds=60,
        )
        for i in range(max_attempts + 1)
    ]

    assert all(r.allowed for r in results[:max_attempts])
    assert results[max_attempts].allowed is False


def test_fail_safe_denies_when_redis_instance_is_down() -> None:
    from app_shared.security.rate_limit import check_and_increment_login

    class _UnreachableRedis:
        def incr(self, key: str) -> int:
            raise ConnectionError("simulated redis outage")

        def expire(self, key: str, seconds: int) -> None:  # pragma: no cover
            raise ConnectionError("simulated redis outage")

        def ttl(self, key: str) -> int:  # pragma: no cover
            raise ConnectionError("simulated redis outage")

        def set(self, key: str, value: int, ex: int) -> None:  # pragma: no cover
            raise ConnectionError("simulated redis outage")

    result = check_and_increment_login(
        _UnreachableRedis(),
        email="anyone@example.com",
        source_ip="127.0.0.1",
        max_attempts=5,
        window_seconds=60,
    )

    assert result.allowed is False


# Note: an end-to-end check that POST /v1/auth/login itself returns 429
# once throttled additionally needs a reachable Postgres (AUTH_DATABASE_URL)
# for the credential-lookup path on every non-throttled attempt -- that
# full-stack assertion lives in tests/integration/test_auth_flow.py (T026),
# which gates on both Postgres and Redis reachability together.
