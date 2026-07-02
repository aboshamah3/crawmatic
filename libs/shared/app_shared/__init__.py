"""Shared cross-cutting library: env config, DB engine/session helpers,
and Celery task-name constants.

Import-light by design: importing ``app_shared`` must never construct a
database engine, open a socket, or pull in scrapy/twisted/playwright.
Submodules (``config``, ``database``, ``task_names``) are imported
explicitly by callers as needed.
"""

__all__: list[str] = []
