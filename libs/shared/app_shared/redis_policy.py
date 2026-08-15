"""Redis server-policy assertion (audit 2026-08-15 risk **H2**).

The same Redis instance carries the Celery broker **and** every
correctness/cost-critical key family in this codebase: dispatch
sentinels (``dispatched:*``), in-flight match locks (``lock:scrape:*``),
webhook dedup, token buckets/semaphores (``rate:*``/``semaphore:*``),
per-domain rate ceilings (``ratelimit:*``), cooldowns (``cooldown:*``),
the monthly proxy budget ledger (``proxybudget:*``), defer budgets
(``defercycles:*``), strategy stats buffers (``strat*``) and the
resolution caches. ``app_shared.access.budget`` states in its own
docstring that these live on a "correctness-critical ``noeviction``
Redis" -- but until this module nothing in the repository *checked*
that, so a silently-misconfigured ``allkeys-lru`` instance could evict
budget counters and match locks under memory pressure and simultaneously
duplicate paid work, erase the spend ledger and drop throttles.

This module is that check. It is deliberately tiny and dependency-free
(stdlib + an injected ``redis.Redis``-shaped client, mirroring
``access/budget.py`` and ``limiter/bucket.py``) so it can run from any
process -- API, worker, scheduler or spider -- at first Redis contact.

## Why "confirmed violation" is fatal but "unknown" is not

``CONFIG GET`` is not universally available: managed Redis offerings and
hardened deployments rename or ACL-block the ``CONFIG`` command, and
in-process test doubles/``fakeredis`` may not implement it at all. Three
outcomes are therefore distinguished (:class:`RedisPolicyStatus`):

* ``COMPLIANT``  -- the server reported the required policy. Silent.
* ``VIOLATION``  -- the server reported a *different, eviction-capable*
  policy. This is a real, actionable misconfiguration: log loudly and,
  when enforcement is on, raise :class:`RedisPolicyViolation`.
* ``UNKNOWN``    -- ``CONFIG GET`` raised, was refused, or returned an
  unparseable/empty reply. **Never fatal**, only a warning. Refusing to
  boot because we could not *ask* would convert a permissions quirk into
  a total outage, which is a worse failure than the one being guarded.

That split is what lets ``PROXY_REDIS_REQUIRE_NOEVICTION`` default to
``True`` without breaking local dev or the test suite: stock Redis
already ships ``maxmemory-policy noeviction`` (so a real local server is
COMPLIANT), and anything that cannot answer is UNKNOWN rather than
VIOLATION.

## Choice of failure mode: refuse to start

For a *confirmed* violation the check raises, i.e. the process refuses
to start, rather than merely degrading a health endpoint. Rationale: the
policy is a static deployment property, not a transient runtime
condition -- it cannot repair itself, so a process that keeps serving
while knowingly running on an evicting correctness store just extends
the window in which locks and paid-spend counters can vanish. A crash
loop is loud, is visible in Railway's deploy status without any
dashboard wiring, and is trivially reversible with the documented escape
hatch below. :func:`last_report` is exported so a health endpoint can
*also* surface the state (including ``UNKNOWN``) without duplicating the
probe.

Escape hatch: set ``PROXY_REDIS_REQUIRE_NOEVICTION=false`` to downgrade
every outcome to a warning. Also note ``maxmemory=0`` (no limit) means
the policy is inert -- no eviction can occur at all -- which is reported
in :class:`RedisPolicyReport.maxmemory` for operators, but is *not* on
its own a pass: an operator who later sets ``maxmemory`` must not
silently inherit an evicting policy.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "REQUIRED_MAXMEMORY_POLICY",
    "RedisPolicyReport",
    "RedisPolicyStatus",
    "RedisPolicyViolation",
    "check_redis_memory_policy",
    "enforce_redis_memory_policy",
    "last_report",
    "reset_policy_check",
]

#: The only eviction policy under which this codebase's Redis key
#: families are safe. Every other policy can silently drop a match lock,
#: a dispatch sentinel or a ``proxybudget:*`` counter.
REQUIRED_MAXMEMORY_POLICY = "noeviction"


class RedisPolicyStatus(str, Enum):
    """Outcome of one :func:`check_redis_memory_policy` probe."""

    COMPLIANT = "COMPLIANT"
    VIOLATION = "VIOLATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RedisPolicyReport:
    """What the connected Redis reported (or why we could not tell).

    ``maxmemory`` is the raw ``CONFIG GET maxmemory`` value as an int
    when it could be parsed (``0`` = unlimited, i.e. the eviction policy
    is currently inert), else ``None``. ``detail`` carries the reason for
    an ``UNKNOWN``.
    """

    status: RedisPolicyStatus
    policy: str | None = None
    maxmemory: int | None = None
    detail: str | None = None

    @property
    def compliant(self) -> bool:
        return self.status is RedisPolicyStatus.COMPLIANT


class RedisPolicyViolation(RuntimeError):
    """Connected Redis reports an eviction-capable ``maxmemory-policy``."""


#: Last probe result, for health endpoints / diagnostics. ``None`` until
#: the first :func:`enforce_redis_memory_policy` or explicit check.
_last_report: RedisPolicyReport | None = None
#: One-shot guard so the probe costs two ``CONFIG GET`` round trips per
#: process, not two per Redis call.
_checked = False


def last_report() -> RedisPolicyReport | None:
    """The most recent probe result (``None`` if never probed).

    Exposed so a ``/health`` handler can report ``UNKNOWN``/``VIOLATION``
    without re-probing -- see the module docstring's failure-mode note.
    """
    return _last_report


def reset_policy_check() -> None:
    """Clear the one-shot guard and cached report (tests, fork-safety)."""
    global _checked, _last_report
    _checked = False
    _last_report = None


def _config_get(redis: Any, name: str) -> str | None:
    """``CONFIG GET name`` -> the value, or ``None`` if unavailable.

    Tolerates both reply shapes (``{name: value}`` with
    ``decode_responses=True``, or a flat ``[name, value]`` sequence) and
    swallows every error -- an unreachable/ACL-blocked ``CONFIG`` is an
    ``UNKNOWN``, never an exception into the caller.
    """
    try:
        reply = redis.config_get(name)
    except Exception:  # noqa: BLE001 - any failure => UNKNOWN, see docstring
        return None
    if isinstance(reply, dict):
        value = reply.get(name)
    elif isinstance(reply, (list, tuple)) and len(reply) >= 2:
        value = reply[1]
    else:
        return None
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    return str(value)


def check_redis_memory_policy(
    redis: Any, *, required_policy: str = REQUIRED_MAXMEMORY_POLICY
) -> RedisPolicyReport:
    """Probe the connected server's eviction policy. Never raises.

    Returns ``COMPLIANT`` when the server reports ``required_policy``,
    ``VIOLATION`` when it reports something else, and ``UNKNOWN`` when
    ``CONFIG GET`` could not answer (see the module docstring for why
    ``UNKNOWN`` is deliberately not a failure).
    """
    policy = _config_get(redis, "maxmemory-policy")
    if policy is None:
        return RedisPolicyReport(
            status=RedisPolicyStatus.UNKNOWN,
            detail="CONFIG GET maxmemory-policy unavailable (unsupported, ACL-blocked or unreachable)",
        )

    raw_maxmemory = _config_get(redis, "maxmemory")
    try:
        maxmemory = int(raw_maxmemory) if raw_maxmemory is not None else None
    except (TypeError, ValueError):
        maxmemory = None

    normalised = policy.strip().lower()
    if normalised == required_policy:
        return RedisPolicyReport(
            status=RedisPolicyStatus.COMPLIANT, policy=normalised, maxmemory=maxmemory
        )
    return RedisPolicyReport(
        status=RedisPolicyStatus.VIOLATION,
        policy=normalised,
        maxmemory=maxmemory,
        detail=f"expected maxmemory-policy {required_policy!r}, server reports {normalised!r}",
    )


def enforce_redis_memory_policy(
    redis: Any,
    *,
    require: bool = True,
    required_policy: str = REQUIRED_MAXMEMORY_POLICY,
    once: bool = True,
) -> RedisPolicyReport:
    """Probe once per process, log structurally, and raise on a violation.

    ``require=False`` downgrades a confirmed violation to a warning (the
    ``PROXY_REDIS_REQUIRE_NOEVICTION`` escape hatch). ``once=False``
    forces a re-probe even if this process already checked -- used by
    tests and by an explicit health-check refresh.

    Emits ``redis.policy.violation`` / ``redis.policy.unknown`` as
    single-line JSON (the `contracts/observability.md` convention) so the
    alerting workstream can count them without a metrics client.
    """
    global _checked, _last_report

    if once and _checked and _last_report is not None:
        return _last_report

    report = check_redis_memory_policy(redis, required_policy=required_policy)
    _last_report = report
    _checked = True

    if report.status is RedisPolicyStatus.UNKNOWN:
        logger.warning(
            json.dumps(
                {
                    "event": "redis.policy.unknown",
                    "required_policy": required_policy,
                    "detail": report.detail,
                }
            )
        )
        return report

    if report.status is RedisPolicyStatus.VIOLATION:
        logger.error(
            json.dumps(
                {
                    "event": "redis.policy.violation",
                    "required_policy": required_policy,
                    "reported_policy": report.policy,
                    "maxmemory": report.maxmemory,
                    "enforced": require,
                    "detail": report.detail,
                }
            )
        )
        if require:
            raise RedisPolicyViolation(
                f"Redis {report.detail}. This instance holds correctness-critical keys "
                f"(match locks, dispatch sentinels, proxybudget:* spend counters); an "
                f"eviction-capable policy can silently drop them. Fix the server config "
                f"(CONFIG SET maxmemory-policy {required_policy} + persist it), or set "
                f"PROXY_REDIS_REQUIRE_NOEVICTION=false to override during an incident."
            )
        return report

    logger.info(
        json.dumps(
            {
                "event": "redis.policy.ok",
                "policy": report.policy,
                "maxmemory": report.maxmemory,
            }
        )
    )
    return report
