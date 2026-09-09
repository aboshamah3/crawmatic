"""Genuinely new Scrapy downloader middlewares (EPA A7, deep dive §8.2).

``scrape_core.limiter``, ``scrape_core.safety``, ``scrape_core.robots``,
``scrape_core.blocking`` and ``scrape_core.netledger_middleware`` predate
this package and stay where they are — this package is for downloader
middlewares that did not exist before A7 (starting with
:mod:`scrape_core.middlewares.wire_bytes`), not a relocation target for
the existing ones.
"""

from __future__ import annotations

__all__: list[str] = []
