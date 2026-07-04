"""Effective rate/concurrency-limit resolution (contracts/rate-limiter.md;
FR-006/FR-008, D4).

Pure stdlib — no Redis/Scrapy/Twisted/FastAPI import. Reads the
**already-resolved, Redis-cached** SPEC-10 ``DomainAccessRule``/
``AccessPolicy`` objects the spider's ``load_targets`` already holds
(``SpiderTarget.domain_rule``/``SpiderTarget.access_policy``) — no new
query, no new column (Principle IV).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["EffectiveLimits", "resolve_limits"]


@dataclass(frozen=True)
class EffectiveLimits:
    """The resolved (per_minute, concurrency, cooldown_seconds) triple for
    one domain + access method. ``per_minute``/``concurrency`` are always
    >= 1 (a safe floor); ``cooldown_seconds`` is >= 0 and is **consumed**
    downstream as the post-denial backoff floor (T013) — not a dead value.
    """

    per_minute: int
    concurrency: int
    cooldown_seconds: int


def resolve_limits(*, domain_rule: Any, access_policy: Any, settings: Any) -> EffectiveLimits:
    """Resolve effective limits with precedence (D4, FR-008):

    enabled matching ``DomainAccessRule`` override -> resolved
    ``AccessPolicy.max_requests_per_minute`` -> ``Settings``
    defaults (``RATE_LIMIT_DEFAULT_PER_MINUTE`` /
    ``RATE_LIMIT_DEFAULT_CONCURRENCY``).

    ``domain_rule`` only ever carries an override for ``per_minute``,
    ``concurrency`` (``max_concurrent_requests``), and
    ``cooldown_seconds`` — ``AccessPolicy`` has no concurrency/cooldown
    column, so those two always fall through domain-rule-or-default.
    """
    if domain_rule is not None and getattr(domain_rule, "enabled", True):
        per_minute = domain_rule.max_requests_per_minute
        concurrency = domain_rule.max_concurrent_requests
        cooldown_seconds = domain_rule.cooldown_seconds
    else:
        per_minute = (
            access_policy.max_requests_per_minute
            if access_policy is not None and access_policy.max_requests_per_minute is not None
            else settings.RATE_LIMIT_DEFAULT_PER_MINUTE
        )
        concurrency = settings.RATE_LIMIT_DEFAULT_CONCURRENCY
        cooldown_seconds = 0

    return EffectiveLimits(
        per_minute=max(1, int(per_minute)),
        concurrency=max(1, int(concurrency)),
        cooldown_seconds=max(0, int(cooldown_seconds)),
    )
