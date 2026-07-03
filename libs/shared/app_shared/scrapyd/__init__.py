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

__all__ = [
    "ScrapydAuthError",
    "ScrapydDispatchClient",
    "ScrapydDispatchError",
    "dispatch_key",
]
