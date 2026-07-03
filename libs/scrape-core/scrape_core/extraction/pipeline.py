"""Ordered extraction orchestrator (contracts/extraction.md "Ordered chain").

``extract(html, profile)`` tries each strategy in :data:`_STRATEGIES`,
in order, first hit wins: JSON-LD -> CSS -> regex (which itself falls
back to the single-number heuristic internally, contracts/extraction.md).
``_STRATEGIES`` is the single extension point so growing the chain never
touches the loop itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from scrape_core.extraction.css import extract_css
from scrape_core.extraction.jsonld import extract_jsonld
from scrape_core.extraction.regex import extract_regex
from scrape_core.extraction.result import ExtractionCandidate

__all__ = ["extract"]

_Strategy = Callable[..., ExtractionCandidate | None]

# Ordered JSON-LD -> CSS -> regex chain (contracts/extraction.md). Regex
# itself falls back to the SINGLE_NUMBER heuristic internally when no
# configured price_regex matches (scrape_core.extraction.regex).
_STRATEGIES: tuple[_Strategy, ...] = (extract_jsonld, extract_css, extract_regex)


def extract(html: str, profile: Any = None) -> ExtractionCandidate | None:
    """Return the first-hit :class:`ExtractionCandidate`, or ``None``.

    ``None`` means no strategy in the chain found a price — the caller
    (the spider's ``parse``) records a ``success=false`` observation
    with ``error_code=PRICE_NOT_FOUND`` (contracts/errors.md); it is
    never raised as an exception.
    """
    for strategy in _STRATEGIES:
        candidate = strategy(html, profile=profile)
        if candidate is not None:
            return candidate
    return None
