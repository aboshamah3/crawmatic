"""Framework-agnostic access-control core (SPEC-10).

Pure business logic + SQLAlchemy-only query helpers turning a resolved
access policy + attempt history into the next transport decision, a
proxy assignment, and the Redis-backed monthly budget / rate-ceiling /
cooldown gates: the effective-policy precedence chain (``resolution``),
the next-``AccessMethod``/proxy-assignment engine (``engine``), the
dual-scope ``proxy_providers``/``access_policies`` query helpers
(``repository``), and the Redis budget/ceiling/cooldown counters
(``budget``). No FastAPI, no Scrapy/Twisted/Playwright (see
``tests/unit/test_import_boundaries.py``). Empty package init for now —
the individual modules land in later phases of this spec.
"""

from __future__ import annotations

__all__: list[str] = []
