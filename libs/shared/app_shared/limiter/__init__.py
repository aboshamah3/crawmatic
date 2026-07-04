"""Distributed rate-limiting & in-flight match-lock primitives (SPEC-11).

Pure Redis logic — stdlib + an injected ``redis.Redis``-shaped client
only, **no** Scrapy/Twisted/FastAPI import — the direct sibling of
``app_shared.access.budget`` (contracts/rate-limiter.md,
contracts/match-lock.md). The reactor-safe orchestration that wraps
these functions off the Twisted reactor lives in the scraping-runtime
library's own ``limiter`` module (Constitution V), never here — this
package must never import (or reference by dotted name) that library.

This is an empty package marker for now; ``keys``/``limits``/``bucket``/
``locks`` modules land in later tasks and are re-exported here once
they exist (T034).
"""

from __future__ import annotations
