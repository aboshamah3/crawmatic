"""Version-guarded consumption resolver + profile get-or-create seam.

Per ``contracts/consumption.md`` (D5/D6, FR-013..FR-015, US2): the pure
``resolve_strategy_start`` decides, from an already-loaded
:class:`~app_shared.models.strategy.DomainStrategyProfile` (or ``None``),
whether a fetch should start from the learned ``(access, extraction)``
pair or fall back to the caller's default escalation ladder.
``resolve_or_create_strategy_profile`` is the "get-or-create" half of the
seam the spider's group-resolution (``generic_price_spider.load_targets``)
calls once per ``(competitor_id, url_pattern)`` group: look the profile
up by its unique key, or insert a fresh ``DISCOVERY_REQUIRED`` row and
enqueue automatic discovery (D5, FR-016) when the key has never been seen.

Both halves are framework-agnostic (SQLAlchemy + stdlib only — no Scrapy/
Twisted/FastAPI import, Constitution I/V): ``resolve_strategy_start``
touches no I/O at all, and ``resolve_or_create_strategy_profile`` accepts
an already-open ``Session`` (never opens/commits its own transaction) so
the caller's ``workspace_txn`` remains the single commit/rollback
boundary — the same convention every other function in
``app_shared/strategy/`` and ``app_shared/repository.py`` follows.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app_shared.enums import AccessMethod, ExtractionMethod, StrategyStatus
from app_shared.messaging import enqueue
from app_shared.models.strategy import DomainStrategyProfile
from app_shared.strategy.repository import resolve_profile
from app_shared.task_names import STRATEGY_DISCOVERY_RUN
from app_shared.url_pattern import URL_PATTERN_ALGORITHM_VERSION

__all__ = ["StrategyStart", "resolve_strategy_start", "resolve_or_create_strategy_profile"]

logger = logging.getLogger(__name__)

#: `STRATEGY_DISCOVERY_RUN` runs on its own queue, distinct from `maintenance`
#: (data-model.md §8, contracts/discovery.md).
_DISCOVERY_QUEUE = "strategy_discovery"

#: Statuses `resolve_strategy_start` will use a preference from -- `ACTIVE`
#: unconditionally, `LEARNING` only when a preferred method is already set
#: (contracts/consumption.md). Every other status (`DISCOVERY_REQUIRED`,
#: `DEGRADED`, `DISABLED`) falls through to `None` -- the caller's default
#: ladder -- even if a stale preferred method still happens to be set on a
#: `DEGRADED` row (US2 contract: "not DEGRADED-without-preference" is
#: automatically satisfied here since DEGRADED is never eligible at all).
_ACTIVE_STATUSES: tuple[StrategyStatus, ...] = (StrategyStatus.ACTIVE,)


@dataclass(frozen=True)
class StrategyStart:
    """The learned start for one attempt (contracts/consumption.md).

    ``extraction_method`` may be ``None`` even when ``access_method`` is
    set -- access and extraction are learned/promoted independently
    (US1 AS5), so a profile can have a confirmed access method with no
    extraction preference yet (or vice versa, though the resolver only
    ever surfaces `StrategyStart` when `preferred_access_method` is set).
    """

    access_method: AccessMethod
    extraction_method: ExtractionMethod | None


def resolve_strategy_start(
    profile: DomainStrategyProfile | None,
    *,
    algorithm_version: int,
) -> StrategyStart | None:
    """Return the learned `(access, extraction)` start, or `None` (contracts/consumption.md).

    `None` means "no usable learned start" -- the caller uses the default
    escalation ladder (SPEC-10 `access.engine.next_attempt` for access,
    the SPEC-06/07 extraction pipeline order for extraction). Returns a
    `StrategyStart` **iff all** hold:

    - `profile is not None` (US2 AS2);
    - `profile.status == ACTIVE` or (`profile.status == LEARNING` and a
      preferred access method is already set) (FR-013, US2 AS1) -- every
      other status (including `DEGRADED`, `DISABLED`, `DISCOVERY_REQUIRED`)
      is excluded outright, so a `DEGRADED` profile never resumes its
      (possibly broken) preferred method just because one is still stored
      (US2 AS3, FR-014);
    - `profile.url_pattern_version == algorithm_version` -- a row stamped
      by a stale pattern-derivation algorithm is loaded but never used
      (FR-005/FR-015, US2 AS4): version-guarding lives here, not in the
      DB query, so a version bump can never silently mix patterns.

    Pure -- no I/O, no Scrapy/Twisted/FastAPI import (Constitution I/V).
    """
    if profile is None:
        return None
    if profile.url_pattern_version != algorithm_version:
        return None

    has_preference = profile.preferred_access_method is not None
    eligible = profile.status in _ACTIVE_STATUSES or (
        profile.status == StrategyStatus.LEARNING and has_preference
    )
    if not eligible or profile.preferred_access_method is None:
        return None

    return StrategyStart(
        access_method=profile.preferred_access_method,
        extraction_method=profile.preferred_extraction_method,
    )


def resolve_or_create_strategy_profile(
    session: Session,
    redis: object,
    *,
    workspace_id: uuid.UUID | str,
    competitor_id: uuid.UUID | str,
    domain: str,
    url_pattern: str,
) -> DomainStrategyProfile:
    """Get-or-create the profile for one `(workspace, competitor, domain, url_pattern)` key.

    `url_pattern` is the caller-derived lookup pattern -- a manual
    `domain_access_rules.url_pattern_override` when one matched, else a
    fresh `derive_url_pattern(url)` at the *current*
    `URL_PATTERN_ALGORITHM_VERSION` (never the group's possibly-stale
    stored `competitor_product_matches.url_pattern`, contracts/
    consumption.md step 1, FR-006).

    On a hit, returns the existing row unchanged. On a miss, inserts a
    fresh `DISCOVERY_REQUIRED` profile stamped with the current
    `URL_PATTERN_ALGORITHM_VERSION` and enqueues `STRATEGY_DISCOVERY_RUN`
    on the `strategy_discovery` queue (`app_shared.messaging.enqueue`,
    `triggered_by="AUTO"`, no `sample_urls` yet -- the task itself selects
    a sample from `competitor_product_matches` for the key, contracts/
    discovery.md) -- the automatic discovery trigger (D5, FR-016). Emits
    `strategy_profile_seeded` (source=AUTO).

    The insert races other concurrent group-resolutions (this spider
    process or another) for the exact same key via a `SAVEPOINT`
    (`session.begin_nested()`): a losing insert's `IntegrityError` (the
    `uq_dsp_ws_competitor_domain_pattern` unique constraint) rolls back
    only that savepoint, then re-reads the now-committed winner's row --
    never a duplicate profile, never a duplicate enqueue, and the
    caller's own outer transaction/`workspace_txn` is left untouched
    either way.

    `redis` is accepted (unused today) for signature symmetry with the
    spider's other per-group resolvers (`load_targets`'s Redis-cached
    profile/access-policy resolution) and as the natural extension point
    for a future resolution cache -- this lookup is a single indexed
    query per group (never per-match, Principle IV), so no cache is
    needed yet.

    **Blocking** (one SELECT, and on a miss one INSERT + one Celery
    producer call) -- must only ever be called from an already off-reactor
    context (the caller's `load_targets`, itself only ever invoked via
    `run_in_thread`), never on the reactor thread.
    """
    profile = resolve_profile(session, workspace_id, competitor_id, domain, url_pattern)
    if profile is not None:
        return profile

    candidate = DomainStrategyProfile(
        workspace_id=workspace_id,
        competitor_id=competitor_id,
        domain=domain,
        url_pattern=url_pattern,
        url_pattern_version=URL_PATTERN_ALGORITHM_VERSION,
        status=StrategyStatus.DISCOVERY_REQUIRED,
    )
    try:
        with session.begin_nested():
            session.add(candidate)
            session.flush()
    except IntegrityError:
        existing = resolve_profile(session, workspace_id, competitor_id, domain, url_pattern)
        if existing is None:
            # Not a uniqueness race after all (an unexpected constraint
            # violation) -- surface it rather than silently swallowing a
            # real error.
            raise
        return existing

    enqueue(
        STRATEGY_DISCOVERY_RUN,
        queue=_DISCOVERY_QUEUE,
        kwargs={
            "workspace_id": str(workspace_id),
            "competitor_id": str(competitor_id),
            "domain": domain,
            "url_pattern": url_pattern,
            "sample_urls": [],
            "triggered_by": "AUTO",
        },
    )
    logger.info(
        "app_shared.strategy.resolution: strategy_profile_seeded workspace_id=%s "
        "competitor_id=%s domain=%s url_pattern=%s source=AUTO",
        workspace_id,
        competitor_id,
        domain,
        url_pattern,
    )
    return candidate
