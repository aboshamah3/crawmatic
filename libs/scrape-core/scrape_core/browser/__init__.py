"""Browser (Playwright)-specific ``scrape_core`` helpers (SPEC-14).

Package marker only at this stage (T009) — the SSRF/variant/page-method
modules land in later phases of SPEC-14. Unlike ``scrape_core.targets``/
``scrape_core.result_builder`` (transport-agnostic, ``app_shared.*`` +
``scrape_core.*`` only), modules under this package may import
Scrapy/``scrapy-playwright``/Playwright — this is the Playwright-facing
half of the shared library (`contracts/shared-extraction.md`).
"""

__all__: list[str] = []
