"""Per-target defer-cycle budget (2026-08-03).

``DEFERRED`` is non-terminal by design: a rate-limited target is handed
back to ``scrape_dispatch`` and re-attempted later. Nothing bounded how
many times that could happen, so a domain that is *persistently*
unavailable — noon while re-blocked, S-Tech while it throttles the
Railway IP — cycles defer → re-dispatch → defer forever and its job can
never finalize. That is what left the 2026-08-02 Cohort B run with ~21
targets churning for three hours, and what would now happen to S-Tech's
90 deferred links, since the retry-ceiling fix converts their terminal
failures into defers.

This module is the bound: each defer of a ``(job, match)`` increments a
Redis counter, and once the budget is spent the caller marks the target
``FAILED`` instead — a real, terminal outcome that lets the job finalize
and shows up honestly in the per-site rate.

Fail-open on any Redis error (returns "keep deferring"): a counter we
cannot read must never turn a transient rate-limit into a hard failure.
The counter is keyed per job so a later job re-attempts with a fresh
budget, and carries a TTL well past any single job's lifetime.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["defer_budget_key", "consume_defer_budget"]

#: Comfortably longer than a job's lifetime; the key is job-scoped, so it
#: only needs to outlive the job that owns it.
_KEY_TTL_SECONDS = 86_400


def defer_budget_key(scrape_job_id: uuid.UUID | str, match_id: uuid.UUID | str) -> str:
    """Redis key holding how many times this target has been deferred."""
    return f"defercycles:{scrape_job_id}:{match_id}"


def consume_defer_budget(
    redis: Any,
    *,
    scrape_job_id: uuid.UUID | str,
    match_id: uuid.UUID | str,
    max_cycles: int,
) -> bool:
    """``True`` -> defer this target; ``False`` -> the budget is spent, fail it.

    Increments the target's defer counter. Returns ``False`` only once the
    count has passed ``max_cycles``, so a target gets ``max_cycles`` genuine
    re-attempts before it is called a failure. Any Redis error fails open
    (``True``) — see the module docstring.
    """
    if max_cycles <= 0:
        return False
    key = defer_budget_key(scrape_job_id, match_id)
    try:
        count = redis.incr(key)
        if count == 1:
            redis.expire(key, _KEY_TTL_SECONDS)
    except Exception:  # noqa: BLE001 - fail open, never harden a transient limit into a failure
        logger.warning("defer_budget: redis unavailable for %s; deferring anyway", key, exc_info=True)
        return True
    if count > max_cycles:
        logger.info(
            "defer_budget: %s exhausted after %d defers (max %d) -- failing the target",
            key,
            count,
            max_cycles,
        )
        return False
    return True
