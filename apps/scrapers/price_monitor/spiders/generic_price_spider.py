"""``generic_price_spider`` — the SPEC-07 US1 MVP HTTP spider.

Per ``contracts/spider-args.md``: parses ``workspace_id``/
``scrape_job_id``/``match_ids``/``mode`` from Scrapyd ``schedule.json``
kwargs, loads the matching ``competitor_product_matches`` rows scoped
to ``workspace_id`` (a match not in the workspace is simply absent, no
cross-read), resolves each match's scrape profile via the SPEC-06
resolution chain **once per (competitor_id, url_pattern) group** —
consuming the same Redis resolution cache SPEC-06 already populates,
never re-walking the chain per match — issues one ``DIRECT_HTTP``
request per match, and in ``parse`` runs extraction + validation and
yields a :class:`~scrape_core.items.ScrapeResult` for both success and
failure. The spider stops at persistence: it never computes alerts,
variant price states, or a ``price_analysis`` task (FR-020).

The profile-resolution helpers below duplicate (rather than import) the
**bounded-load** shape of ``apps/api/app/services/profile_resolution.py``
because ``apps/scrapers`` may depend on ``libs/scrape-core`` +
``libs/shared/app_shared`` only (never on another ``apps/*`` member,
`plan.md` "apps -> libs only") — but they read/write the *exact same*
Redis cache key (``app_shared.profiles.resolution.resolution_cache_key``)
that orchestrator populates, so a warm cache is genuinely reused, not
re-derived under a different key.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

import scrapy
from scrapy.http import Response
from sqlalchemy import select

from app_shared.enums import AccessMethod, RobotsPolicy
from app_shared.models.competitors_matches import Competitor, CompetitorProductMatch
from app_shared.models.identity import Workspace
from app_shared.models.scrape_profiles import ScrapeProfile
from app_shared.profiles.confidence import resolve_confidence_rules
from app_shared.profiles.repository import GLOBAL_DEFAULT_PROFILE_NAME, profile_visibility_map
from app_shared.profiles.resolution import (
    ResolutionResult,
    ResolvedProfile,
    apply_match_override,
    decode_group_result,
    encode_group_result,
    group_matches,
    resolution_cache_key,
    resolve_group,
)
from app_shared.redis_client import get_redis_client
from app_shared.repository import scoped_select

from scrape_core.db import run_in_thread, workspace_txn
from scrape_core.errors import PRICE_NOT_FOUND, classify_exception, classify_http_status
from scrape_core.extraction.pipeline import extract
from scrape_core.items import ScrapeResult
from scrape_core.validation import Accepted, Rejected, validate_candidate

logger = logging.getLogger(__name__)

_DEFAULT_MODE = "HTTP"


@dataclass(frozen=True)
class SpiderTarget:
    """One match bundled with its resolved scrape profile (bounded load result)."""

    match_id: uuid.UUID
    product_id: uuid.UUID
    product_variant_id: uuid.UUID
    competitor_id: uuid.UUID
    url: str
    profile: ScrapeProfile | None
    robots_policy: RobotsPolicy


# --- match_ids arg parsing (contracts/spider-args.md) -----------------------


def _parse_match_ids(raw: Any) -> list[uuid.UUID]:
    """Accept a comma-separated string or a JSON list of UUID strings."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        text = str(raw).strip()
        if not text:
            return []
        if text.startswith("["):
            values = json.loads(text)
        else:
            values = [part.strip() for part in text.split(",") if part.strip()]
    return [uuid.UUID(str(v)) for v in values]


# --- Redis resolution-cache get/set (value codec shared via app_shared) -----
#
# The encode/decode codec itself (`encode_group_result`/`decode_group_result`)
# lives in `app_shared.profiles.resolution` (SPEC-07 tasks.md T055) — shared
# with `apps/api/app/services/profile_resolution.py`, the SPEC-06
# orchestrator that populates this same cache, so the two can never
# silently drift apart. This module only duplicates the **bounded-load**
# shape (see module docstring) — never this codec.


def _cache_get_group_result(redis: Any, cache_key: str) -> ResolutionResult | None:
    """``None`` = cache miss or a Redis read error — fail-open, re-walk the chain."""
    try:
        cached = redis.get(cache_key)
    except Exception:  # noqa: BLE001 - Redis must never be a hard dependency here
        return None
    if cached is None:
        return None
    if isinstance(cached, bytes):
        cached = cached.decode("utf-8")
    try:
        return decode_group_result(cached)
    except (ValueError, AttributeError):
        return None


def _cache_set_group_result(redis: Any, cache_key: str, result: ResolutionResult) -> None:
    try:
        from app_shared.config import get_settings

        ttl_seconds = get_settings().PROFILE_RESOLUTION_CACHE_TTL_SECONDS
        redis.set(cache_key, encode_group_result(result), ex=ttl_seconds)
    except Exception:  # noqa: BLE001 - best-effort repopulate only
        pass


# --- bounded profile resolution + target load (blocking; run_in_thread only) ---


def load_targets(workspace_id: uuid.UUID, match_ids: list[uuid.UUID]) -> list[SpiderTarget]:
    """Load the workspace-scoped matches + their resolved scrape profiles.

    **Blocking** — DB + Redis round trips. Must only ever be called
    inside :func:`scrape_core.db.run_in_thread`, never on the reactor
    thread. Bounded regardless of ``len(match_ids)`` (mirrors
    ``apps/api/app/services/profile_resolution.resolve_profiles_for_matches``):
    one query for matches, one for the workspace default, one ``IN``
    query for competitor defaults, one for the global default, one
    ``IN`` query for profile visibility, then one Redis-cached
    ``resolve_group`` walk per distinct ``(competitor_id, url_pattern)``
    group (never per match) plus one final ``IN`` load of the resolved
    profile rows themselves.

    A ``match_id`` not found in ``workspace_id`` is simply absent from
    the result (no cross-read, FR-002).
    """
    if not match_ids:
        return []

    with workspace_txn(workspace_id) as session:
        matches = (
            session.execute(
                scoped_select(CompetitorProductMatch, workspace_id).where(
                    CompetitorProductMatch.id.in_(match_ids)
                )
            )
            .scalars()
            .all()
        )
        if not matches:
            return []

        groups = group_matches(matches)
        competitor_ids = {competitor_id for competitor_id, _url_pattern in groups}

        workspace = session.get(Workspace, workspace_id)
        workspace_default_id = workspace.default_scrape_profile_id if workspace else None

        competitor_rows = (
            session.execute(scoped_select(Competitor, workspace_id).where(Competitor.id.in_(competitor_ids)))
            .scalars()
            .all()
        )
        competitor_default_by_id = {row.id: row.default_scrape_profile_id for row in competitor_rows}
        competitor_robots_policy_by_id = {row.id: row.robots_policy for row in competitor_rows}

        global_default_id = session.execute(
            select(ScrapeProfile.id).where(
                ScrapeProfile.workspace_id.is_(None),
                ScrapeProfile.name == GLOBAL_DEFAULT_PROFILE_NAME,
            )
        ).scalar_one_or_none()

        candidate_ids: set[uuid.UUID] = {
            cid
            for cid in (
                workspace_default_id,
                global_default_id,
                *competitor_default_by_id.values(),
                *(m.scrape_profile_id for m in matches),
            )
            if cid is not None
        }
        visibility = profile_visibility_map(session, workspace_id, candidate_ids) if candidate_ids else {}
        visible_ids = set(visibility.keys())

        redis = get_redis_client()
        group_results: dict[tuple[uuid.UUID, str], ResolutionResult] = {}
        for (competitor_id, url_pattern), _group in groups.items():
            cache_key = resolution_cache_key(workspace_id, competitor_id, url_pattern)
            cached_result = _cache_get_group_result(redis, cache_key)
            if cached_result is not None:
                group_results[(competitor_id, url_pattern)] = cached_result
                continue
            result = resolve_group(
                competitor_default_id=competitor_default_by_id.get(competitor_id),
                workspace_default_id=workspace_default_id,
                global_default_id=global_default_id,
                visible_ids=visible_ids,
            )
            _cache_set_group_result(redis, cache_key, result)
            group_results[(competitor_id, url_pattern)] = result

        resolved_profile_id_by_match: dict[uuid.UUID, uuid.UUID | None] = {}
        for (competitor_id, url_pattern), group in groups.items():
            group_result = group_results[(competitor_id, url_pattern)]
            for match in group:
                match_result = apply_match_override(group_result, match.scrape_profile_id, visible_ids)
                resolved_profile_id_by_match[match.id] = (
                    match_result.profile_id if isinstance(match_result, ResolvedProfile) else None
                )

        resolved_ids = {pid for pid in resolved_profile_id_by_match.values() if pid is not None}
        profiles_by_id: dict[uuid.UUID, ScrapeProfile] = {}
        if resolved_ids:
            rows = session.execute(select(ScrapeProfile).where(ScrapeProfile.id.in_(resolved_ids))).scalars().all()
            profiles_by_id = {row.id: row for row in rows}

        targets: list[SpiderTarget] = []
        for match in matches:
            profile_id = resolved_profile_id_by_match.get(match.id)
            profile = profiles_by_id.get(profile_id) if profile_id else None
            targets.append(
                SpiderTarget(
                    match_id=match.id,
                    product_id=match.product_id,
                    product_variant_id=match.product_variant_id,
                    competitor_id=match.competitor_id,
                    url=match.competitor_url,
                    profile=profile,
                    robots_policy=competitor_robots_policy_by_id.get(
                        match.competitor_id, RobotsPolicy.RESPECT
                    ),
                )
            )
        return targets


class GenericPriceSpider(scrapy.Spider):
    """Fetch each target's product page over ``DIRECT_HTTP``, extract, validate, persist."""

    name = "generic_price_spider"

    def __init__(
        self,
        workspace_id: str | None = None,
        scrape_job_id: str | None = None,
        match_ids: Any = None,
        mode: str | None = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        if not workspace_id:
            raise ValueError("generic_price_spider requires a workspace_id argument")
        parsed_match_ids = _parse_match_ids(match_ids)
        if not parsed_match_ids:
            raise ValueError("generic_price_spider requires a non-empty match_ids argument")

        self.workspace_id: uuid.UUID = uuid.UUID(str(workspace_id))
        self.scrape_job_id: uuid.UUID | None = uuid.UUID(str(scrape_job_id)) if scrape_job_id else None
        self.match_ids: list[uuid.UUID] = parsed_match_ids
        # `mode` is reserved/pass-through in this slice -- only "HTTP" is
        # honored (DIRECT_HTTP); other transport modes are later specs
        # (contracts/spider-args.md).
        self.mode: str = mode or _DEFAULT_MODE
        self._targets_by_match_id: dict[uuid.UUID, SpiderTarget] = {}

    async def start(self) -> AsyncIterator[scrapy.Request]:
        targets = await run_in_thread(load_targets, self.workspace_id, self.match_ids)
        for target in targets:
            self._targets_by_match_id[target.match_id] = target
            yield self._request_for(target)

    def _request_for(self, target: SpiderTarget) -> scrapy.Request:
        """Build the one ``DIRECT_HTTP`` request for `target`.

        Carries the resolved per-competitor ``robots_policy`` on
        ``request.meta`` (SPEC-07 tasks.md T054, FR-006) so
        ``RobotsPolicyMiddleware.process_request`` honors it instead of
        silently falling through to its conservative ``RESPECT`` default
        for every request.
        """
        return scrapy.Request(
            url=target.url,
            callback=self.parse,
            errback=self.errback,
            dont_filter=True,
            meta={
                "match_id": target.match_id,
                "download_slot": str(target.match_id),
                "robots_policy": target.robots_policy,
            },
        )

    def parse(self, response: Response, **kwargs: Any) -> Any:
        target = self._targets_by_match_id[response.meta["match_id"]]
        now = datetime.now(UTC)

        status_error_code = classify_http_status(response.status)
        if status_error_code is not None:
            yield self._build_result(
                target,
                response.url,
                now,
                status_code=response.status,
                success=False,
                error_code=status_error_code,
                error_message=f"HTTP {response.status}",
            )
            return

        candidate = extract(response.text, target.profile)
        if candidate is None:
            yield self._build_result(
                target,
                response.url,
                now,
                status_code=response.status,
                success=False,
                error_code=PRICE_NOT_FOUND,
                error_message="no extraction strategy matched a price",
            )
            return

        profile_confidence_rules = target.profile.confidence_rules if target.profile else None
        confidence_cfg = resolve_confidence_rules(profile_confidence_rules)
        validation_rules = (target.profile.validation_rules if target.profile else None) or {}
        outcome = validate_candidate(candidate, validation_rules, confidence_cfg)

        if isinstance(outcome, Rejected):
            yield self._build_result(
                target,
                response.url,
                now,
                status_code=response.status,
                success=False,
                error_code=outcome.error_code,
                error_message=outcome.message,
                candidate_extras=candidate,
            )
            return

        assert isinstance(outcome, Accepted)
        yield self._build_result(
            target,
            response.url,
            now,
            status_code=response.status,
            success=True,
            comparable=outcome.comparable,
            price=outcome.price,
            candidate_extras=candidate,
        )

    def errback(self, failure: Any) -> Any:
        match_id = failure.request.meta.get("match_id")
        target = self._targets_by_match_id.get(match_id)
        if target is None:
            logger.error("generic_price_spider: fetch failure with no known target: %s", failure)
            return None
        now = datetime.now(UTC)
        hostname = urlsplit(failure.request.url).hostname
        error_code = classify_exception(failure.value, hostname=hostname)
        yield self._build_result(
            target,
            failure.request.url,
            now,
            status_code=None,
            success=False,
            error_code=error_code,
            error_message=str(failure.value),
        )

    def _build_result(
        self,
        target: SpiderTarget,
        url: str,
        scraped_at: datetime,
        *,
        status_code: int | None,
        success: bool,
        error_code: Any = None,
        error_message: str | None = None,
        comparable: bool = True,
        price: Decimal | None = None,
        candidate_extras: Any = None,
    ) -> ScrapeResult:
        kwargs: dict[str, Any] = {}
        if candidate_extras is not None:
            kwargs.update(
                currency=candidate_extras.currency,
                stock_status=candidate_extras.stock,
                raw_title=candidate_extras.raw_title,
                extraction_method=candidate_extras.method,
                extraction_confidence=Decimal(str(candidate_extras.confidence)),
                selector_used=candidate_extras.selector_used,
            )
        return ScrapeResult(
            workspace_id=self.workspace_id,
            match_id=target.match_id,
            product_id=target.product_id,
            product_variant_id=target.product_variant_id,
            competitor_id=target.competitor_id,
            scrape_job_id=self.scrape_job_id,
            url=url,
            access_method=AccessMethod.DIRECT_HTTP,
            status_code=status_code,
            scraped_at=scraped_at,
            price=price,
            success=success,
            comparable=comparable,
            error_code=error_code,
            error_message=error_message,
            **kwargs,
        )
