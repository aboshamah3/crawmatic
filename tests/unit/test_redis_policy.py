"""`app_shared/redis_policy.py` unit tests (audit 2026-08-15 risk H2).

The behaviour under test is the three-way COMPLIANT/VIOLATION/UNKNOWN
split, and specifically that UNKNOWN is never fatal -- that is what lets
`PROXY_REDIS_REQUIRE_NOEVICTION` default to True without breaking local
dev, fakeredis, or a managed Redis that ACL-blocks `CONFIG`.

Fake clients only; no live Redis required (mirrors
`tests/unit/test_access_budget.py`'s `_FakeRedis`/`_BrokenRedis`).
"""

from __future__ import annotations

import pytest

from app_shared.redis_policy import (
    RedisPolicyStatus,
    RedisPolicyViolation,
    check_redis_memory_policy,
    enforce_redis_memory_policy,
    last_report,
    reset_policy_check,
)


class _DictReplyRedis:
    """`decode_responses=True` shape: `CONFIG GET` returns a dict."""

    def __init__(self, policy: str, maxmemory: str = "0") -> None:
        self._values = {"maxmemory-policy": policy, "maxmemory": maxmemory}
        self.calls: list[str] = []

    def config_get(self, name: str) -> dict[str, str]:
        self.calls.append(name)
        return {name: self._values[name]}


class _ListReplyRedis:
    """Raw shape: `CONFIG GET` returns a flat [name, value] sequence."""

    def __init__(self, policy: str, maxmemory: str = "0") -> None:
        self._values = {"maxmemory-policy": policy, "maxmemory": maxmemory}

    def config_get(self, name: str) -> list[str]:
        return [name, self._values[name]]


class _NoConfigRedis:
    """A server that refuses/does not implement `CONFIG GET`."""

    def config_get(self, name: str) -> dict[str, str]:
        raise RuntimeError("ERR unknown command 'CONFIG'")


class _EmptyReplyRedis:
    """`CONFIG GET` answers, but with nothing usable (fakeredis-ish)."""

    def config_get(self, name: str) -> dict[str, str]:
        return {}


@pytest.fixture(autouse=True)
def _clean_module_state() -> None:
    reset_policy_check()
    yield
    reset_policy_check()


# --- check_redis_memory_policy ----------------------------------------------


def test_noeviction_is_compliant() -> None:
    report = check_redis_memory_policy(_DictReplyRedis("noeviction"))
    assert report.status is RedisPolicyStatus.COMPLIANT
    assert report.compliant is True
    assert report.policy == "noeviction"


def test_policy_comparison_is_case_and_whitespace_insensitive() -> None:
    report = check_redis_memory_policy(_DictReplyRedis("  NoEviction "))
    assert report.status is RedisPolicyStatus.COMPLIANT


def test_flat_list_reply_shape_is_understood() -> None:
    report = check_redis_memory_policy(_ListReplyRedis("noeviction", "1048576"))
    assert report.status is RedisPolicyStatus.COMPLIANT
    assert report.maxmemory == 1048576


@pytest.mark.parametrize(
    "policy",
    ["allkeys-lru", "volatile-lru", "allkeys-random", "volatile-ttl", "allkeys-lfu"],
)
def test_every_eviction_capable_policy_is_a_violation(policy: str) -> None:
    report = check_redis_memory_policy(_DictReplyRedis(policy))
    assert report.status is RedisPolicyStatus.VIOLATION
    assert report.compliant is False
    assert policy in (report.detail or "")


def test_unreachable_config_get_is_unknown_not_violation() -> None:
    report = check_redis_memory_policy(_NoConfigRedis())
    assert report.status is RedisPolicyStatus.UNKNOWN


def test_empty_config_reply_is_unknown() -> None:
    report = check_redis_memory_policy(_EmptyReplyRedis())
    assert report.status is RedisPolicyStatus.UNKNOWN


def test_unparseable_maxmemory_does_not_break_the_probe() -> None:
    report = check_redis_memory_policy(_DictReplyRedis("noeviction", "not-a-number"))
    assert report.status is RedisPolicyStatus.COMPLIANT
    assert report.maxmemory is None


# --- enforce_redis_memory_policy --------------------------------------------


def test_enforce_raises_on_confirmed_violation() -> None:
    with pytest.raises(RedisPolicyViolation) as excinfo:
        enforce_redis_memory_policy(_DictReplyRedis("allkeys-lru"), require=True)
    # The message must name the escape hatch — this is what an operator
    # sees in a crash loop at 3am.
    assert "PROXY_REDIS_REQUIRE_NOEVICTION" in str(excinfo.value)


def test_enforce_does_not_raise_when_requirement_is_disabled() -> None:
    report = enforce_redis_memory_policy(_DictReplyRedis("allkeys-lru"), require=False)
    assert report.status is RedisPolicyStatus.VIOLATION


def test_enforce_never_raises_on_unknown_even_when_required() -> None:
    """The key local-dev/test/managed-Redis safety property."""
    report = enforce_redis_memory_policy(_NoConfigRedis(), require=True)
    assert report.status is RedisPolicyStatus.UNKNOWN


def test_enforce_is_silent_and_passing_on_a_compliant_server() -> None:
    report = enforce_redis_memory_policy(_DictReplyRedis("noeviction"), require=True)
    assert report.compliant is True


def test_enforce_probes_only_once_per_process() -> None:
    redis = _DictReplyRedis("noeviction")
    enforce_redis_memory_policy(redis, require=True)
    calls_after_first = len(redis.calls)
    enforce_redis_memory_policy(redis, require=True)
    assert len(redis.calls) == calls_after_first


def test_once_false_forces_a_reprobe() -> None:
    redis = _DictReplyRedis("noeviction")
    enforce_redis_memory_policy(redis, require=True)
    calls_after_first = len(redis.calls)
    enforce_redis_memory_policy(redis, require=True, once=False)
    assert len(redis.calls) > calls_after_first


def test_last_report_exposes_state_for_a_health_check() -> None:
    assert last_report() is None
    enforce_redis_memory_policy(_DictReplyRedis("volatile-lru"), require=False)
    report = last_report()
    assert report is not None
    assert report.status is RedisPolicyStatus.VIOLATION
