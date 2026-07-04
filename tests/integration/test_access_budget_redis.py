"""Live Redis budget/ceiling/cooldown test (SPEC-10 US2 T031,
`contracts/budget-ceilings.md` Acceptance, FR-010/FR-011) — DEFERRED.

Exercises `app_shared.access.budget` against a real Redis (no Postgres
needed): the same behaviors `tests/unit/test_access_budget.py` proves
with a fake client, here against the genuine `redis.Redis` wire
protocol -- monthly rollover, TTL-to-end-of-month, independent
per-window ceiling counters, the `SET NX EX` cooldown gate, and
fail-open on a simulated Redis error (pointed at an unreachable
host:port).

Needs a reachable Redis (`REDIS_URL`). Not runnable in the
no-Docker-daemon build environment used to author this feature — SKIPS
cleanly whenever `Settings`/`REDIS_URL` isn't usable or a real `PING`
fails.

Author now; leave unchecked (DEFERRED — needs a Redis-capable host).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import datetime, timezone

import pytest


def _live_redis_reachable() -> bool:
    """Best-effort probe: True only if `REDIS_URL` is configured and answers PING."""
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
    not _live_redis_reachable(),
    reason="No reachable Redis (REDIS_URL) configured in this environment",
)


@pytest.fixture()
def redis_client():
    from app_shared.redis_client import get_redis_client

    return get_redis_client()


@pytest.fixture()
def cleanup_keys(redis_client) -> Iterator[list[str]]:
    """Collect keys used by a test and delete them afterward."""
    keys: list[str] = []
    yield keys
    if keys:
        redis_client.delete(*keys)


# --- incr_and_check_monthly_budget ------------------------------------------


def test_live_monthly_budget_rollover_and_ttl(redis_client, cleanup_keys) -> None:
    from app_shared.access.budget import incr_and_check_monthly_budget

    provider_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    key = f"proxybudget:{provider_id}:{now:%Y_%m}"
    cleanup_keys.append(key)

    last = None
    for _ in range(4):
        last = incr_and_check_monthly_budget(redis_client, provider_id=provider_id, limit=3, now=now)
    assert last is not None
    assert last.allowed is False
    assert last.used == 4

    ttl = redis_client.ttl(key)
    assert ttl > 0  # end-of-month expiry set on first hit

    # A different %Y_%m key resets the counter (rollover -- distinct key,
    # no cross-month contamination). Built with day=1 to sidestep any
    # day-of-month overflow (e.g. Jan 31 -> Feb 31 doesn't exist).
    next_year = now.year + (1 if now.month == 12 else 0)
    next_month_num = (now.month % 12) + 1
    next_month = datetime(next_year, next_month_num, 1, tzinfo=timezone.utc)
    next_key = f"proxybudget:{provider_id}:{next_month:%Y_%m}"
    cleanup_keys.append(next_key)
    result = incr_and_check_monthly_budget(redis_client, provider_id=provider_id, limit=3, now=next_month)
    assert result.allowed is True
    assert result.used == 1


# --- check_rate_ceilings ------------------------------------------------------


def test_live_rate_ceilings_per_window_counters(redis_client, cleanup_keys) -> None:
    from app_shared.access.budget import check_rate_ceilings

    policy_id = uuid.uuid4()
    domain = f"live-budget-{uuid.uuid4().hex[:8]}.example.com"
    cleanup_keys.extend(
        [
            f"ratelimit:{policy_id}:{domain}:60",
            f"ratelimit:{policy_id}:{domain}:3600",
            f"ratelimit:{policy_id}:{domain}:86400",
        ]
    )

    decisions = [
        check_rate_ceilings(redis_client, policy_id=policy_id, domain=domain, per_minute=2, per_hour=None, per_day=None)
        for _ in range(3)
    ]
    assert [d.allowed for d in decisions] == [True, True, False]
    assert decisions[-1].retry_after_seconds > 0


# --- check_domain_cooldown ----------------------------------------------------


def test_live_domain_cooldown_gate(redis_client, cleanup_keys) -> None:
    from app_shared.access.budget import check_domain_cooldown

    domain = f"live-cooldown-{uuid.uuid4().hex[:8]}.example.com"
    cleanup_keys.append(f"cooldown:{domain}")

    assert check_domain_cooldown(redis_client, domain=domain, cooldown_seconds=1) is True
    assert check_domain_cooldown(redis_client, domain=domain, cooldown_seconds=1) is False

    time.sleep(1.2)
    assert check_domain_cooldown(redis_client, domain=domain, cooldown_seconds=1) is True


# --- fail-open on a genuine Redis connection error --------------------------


def test_live_budget_functions_fail_open_on_unreachable_redis() -> None:
    import redis as redis_lib

    from app_shared.access.budget import check_domain_cooldown, check_rate_ceilings, incr_and_check_monthly_budget

    # A client pointed at a port nothing listens on -- a real connection
    # error, not a mock -- proving the fail-open contract end-to-end.
    unreachable = redis_lib.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, socket_timeout=0.2)

    budget_result = incr_and_check_monthly_budget(
        unreachable, provider_id=uuid.uuid4(), limit=1, now=datetime.now(timezone.utc)
    )
    assert budget_result.allowed is True

    rate_decision = check_rate_ceilings(
        unreachable, policy_id=uuid.uuid4(), domain="example.com", per_minute=1, per_hour=1, per_day=1
    )
    assert rate_decision.allowed is True

    assert check_domain_cooldown(unreachable, domain="example.com", cooldown_seconds=30) is True
