"""Labeled offer-truth benchmark corpus (Task W3.3, READY-012).

See ``CORPUS.md`` in this directory for provenance/labeling governance
(styled after ``tests/fixtures/noon_labeled/FIXTURES.md`` and the B5/B5b
certification fixtures). :mod:`schema` loads a case file into a
:class:`BenchmarkCase`; :mod:`scripts.run_offer_benchmark` (repo root)
is the scorer that consumes them.
"""

from __future__ import annotations
