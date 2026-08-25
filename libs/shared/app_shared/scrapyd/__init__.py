"""Authenticated, idempotent Scrapyd dispatch (framework-agnostic).

This subpackage owns the HTTP client used to schedule spiders on a Scrapyd
node. It deliberately depends only on ``requests``, ``redis``, and other
``app_shared`` modules — never on scrapy/twisted — so it stays importable by
Celery workers and unit-testable without booting a scraping framework (the
import-boundary test enforces this).
"""

from __future__ import annotations

from app_shared.scrapyd.client import (
    ScrapydAuthError,
    ScrapydDispatchClient,
    ScrapydDispatchError,
    dispatch_key,
)
from app_shared.scrapyd.errors import StaleCancellationGenerationError
from app_shared.scrapyd.identity import (
    PENDING_SENTINEL,
    CommittedDispatch,
    DispatchIdentity,
    DispatchIntentAuthority,
    build_dispatch_identity,
    get_committed_dispatch,
)
from app_shared.scrapyd.reconcile import reconcile_inflight

__all__ = [
    "PENDING_SENTINEL",
    "CommittedDispatch",
    "DispatchIdentity",
    "DispatchIntentAuthority",
    "ScrapydAuthError",
    "ScrapydDispatchClient",
    "ScrapydDispatchError",
    "StaleCancellationGenerationError",
    "build_dispatch_identity",
    "dispatch_key",
    "get_committed_dispatch",
    "reconcile_inflight",
]
