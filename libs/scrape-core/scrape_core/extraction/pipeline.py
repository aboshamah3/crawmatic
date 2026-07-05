"""Ordered extraction orchestrator (contracts/extraction.md "Ordered chain").

``extract(html, profile)`` tries each strategy in :data:`_STRATEGIES`,
in order, first hit wins: JSON-LD -> CSS -> regex (which itself falls
back to the single-number heuristic internally, contracts/extraction.md).
``_STRATEGIES`` is the single extension point so growing the chain never
touches the loop itself.

SPEC-12 US2 (`contracts/consumption.md` step 3, D6): an optional
``preferred_method`` keyword reorders the chain to try the learned
domain's winning strategy first, falling back to the full order only if
it misses -- never a *narrower* chain, so an unconfirmed/still-learning
page never loses coverage. ``REGEX``/``SINGLE_NUMBER`` share one
underlying strategy function (:func:`~scrape_core.extraction.regex.extract_regex`
picks between them internally), so either preferred value simply
prioritizes that one function.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app_shared.enums import ExtractionMethod

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

#: `ExtractionMethod` -> the strategy function that can produce it (SPEC-12
#: US2). `REGEX` and `SINGLE_NUMBER` both map to `extract_regex`, which
#: decides between them internally -- there is no narrower entry point.
_METHOD_TO_STRATEGY: dict[ExtractionMethod, _Strategy] = {
    ExtractionMethod.JSON_LD: extract_jsonld,
    ExtractionMethod.CSS: extract_css,
    ExtractionMethod.REGEX: extract_regex,
    ExtractionMethod.SINGLE_NUMBER: extract_regex,
}


def _ordered_strategies(preferred_method: ExtractionMethod | None) -> tuple[_Strategy, ...]:
    """The chain to try, `preferred_method`'s strategy first if recognized.

    An unrecognized/forward-compat `preferred_method` (e.g. a later-spec
    method this pipeline doesn't implement yet, `PLATFORM_JSON`/
    `EMBEDDED_JSON`/`XPATH`/`PLAYWRIGHT`) is a no-op -- the unmodified
    default order runs, never an error.
    """
    if preferred_method is None:
        return _STRATEGIES
    preferred_fn = _METHOD_TO_STRATEGY.get(preferred_method)
    if preferred_fn is None:
        return _STRATEGIES
    rest = tuple(strategy for strategy in _STRATEGIES if strategy is not preferred_fn)
    return (preferred_fn, *rest)


def extract(
    html: str, profile: Any = None, *, preferred_method: ExtractionMethod | None = None
) -> ExtractionCandidate | None:
    """Return the first-hit :class:`ExtractionCandidate`, or ``None``.

    ``None`` means no strategy in the chain found a price — the caller
    (the spider's ``parse``) records a ``success=false`` observation
    with ``error_code=PRICE_NOT_FOUND`` (contracts/errors.md); it is
    never raised as an exception.

    ``preferred_method`` (SPEC-12 US2, learned-domain start, D6) tries
    that strategy first; on a miss, falls back to the full default order
    (never a narrower chain) so a learned domain never loses a price it
    would otherwise have found via a different strategy.
    """
    for strategy in _ordered_strategies(preferred_method):
        candidate = strategy(html, profile=profile)
        if candidate is not None:
            return candidate
    return None
