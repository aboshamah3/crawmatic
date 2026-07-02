"""Shared scraping-side library imported by both Scrapyd app members.

Package marker only — no scraping logic yet. May import ``app_shared``;
``app_shared`` must never import this package (one-way dependency
boundary).
"""

__all__: list[str] = []
