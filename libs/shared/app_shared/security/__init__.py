"""Framework-agnostic authentication/authorization primitives.

Per the SPEC-03 scope boundary: password hashing, tokens, JWT, API-key
crypto, scope vocabulary, rate-limiting, and status-cache helpers all
live under this package. Pure Python + ``argon2-cffi``/``pyjwt``/
``redis`` — no ``fastapi``/``scrapy``/``twisted``/``playwright`` import
anywhere under here (asserted by ``tests/unit/test_import_boundaries.py``).
The FastAPI dependency + routers that consume these primitives live only
in ``apps/api``.

Package marker only in Phase 1/2 — individual primitive modules
(``passwords``, ``tokens``, ``jwt``, ``api_keys``, ``scopes``,
``rate_limit``, ``last_used``, ``status_cache``) land in later phases.
"""

from __future__ import annotations
