"""Pure (parsel/stdlib-only, no reactor) price-extraction strategies.

Per ``contracts/extraction.md``: each strategy module (``jsonld.py`` in
this US1 slice; ``css.py``/``regex.py`` in US3) is a pure function over
an HTML body returning an :class:`~scrape_core.extraction.result.ExtractionCandidate`
or ``None``. ``pipeline.py`` orders them into a single ``extract()``
entry point the spider calls.
"""

from __future__ import annotations

from scrape_core.extraction.result import ExtractionCandidate

__all__ = ["ExtractionCandidate"]
