"""Shared support for the SPEC-14 US1 live-stack browser-spider
integration tests (``test_browser_spider_*_live.py``).

**Not a test module** — its filename deliberately does not match
pytest's default ``test_*.py`` collection pattern (mirrors
``tests/integration/_scrapyd_spider_live_support.py``, which this module
reuses for Postgres/Redis reachability + fixture-page serving + workspace
seeding rather than duplicating that machinery).

Adds the two things unique to the browser spider:

1. :func:`chromium_reachable` — a best-effort probe that Playwright's
   Chromium binary is actually installed (``playwright install``) in
   this environment; this build environment has no container engine and
   no downloaded browser binary, so this reliably returns ``False`` here
   (`env reality` — never faked).
2. :func:`run_generic_browser_price_spider_subprocess` — runs
   ``generic_browser_price_spider`` via ``scrapy.crawler.CrawlerProcess``
   in its own OS process (Twisted's reactor can only start once per
   process, same rationale as the HTTP runner). Unlike the HTTP runner,
   no ``DNS_RESOLVER`` override is threaded through: ``scrapy-playwright``
   does not consult Scrapy's ``DNS_RESOLVER`` at all (`plan.md` —
   Chromium does its own OS-level resolution), so a fixture test simply
   points the seeded match's URL at ``127.0.0.1`` directly instead of an
   injectable-resolver hostname.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Iterable, Mapping

from ._scrapyd_spider_live_support import live_stack_reachable

__all__ = [
    "chromium_reachable",
    "live_browser_stack_reachable",
    "run_generic_browser_price_spider_subprocess",
]


def chromium_reachable() -> bool:
    """Best-effort probe: Playwright's Chromium binary launches successfully.

    Returns ``False`` (never raises) whenever ``playwright`` isn't
    importable, no browser binary is installed
    (``playwright install`` was never run), or launch fails for any
    other reason — exactly the no-container-engine build environment
    this feature was authored in.
    """
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
    except Exception:
        return False
    return True


def live_browser_stack_reachable(required_tables: Iterable[str] = ()) -> bool:
    """Postgres(+tables)/Redis reachable (reused HTTP-spider probe) AND
    Chromium launchable AND the browser Scrapy project importable."""
    if not live_stack_reachable(required_tables):
        return False
    if not chromium_reachable():
        return False
    try:
        import price_monitor_browser.spiders.generic_browser_price_spider  # noqa: F401
    except Exception:
        return False
    return True


_BROWSER_RUNNER_TEMPLATE = """
{commit_hook_source}
from scrapy.crawler import CrawlerProcess
from scrapy.settings import Settings

import price_monitor_browser.settings as _base_settings
from price_monitor_browser.spiders.generic_browser_price_spider import GenericBrowserPriceSpider

settings = Settings()
settings.setmodule(_base_settings, priority="project")
for _k, _v in {extra_settings!r}.items():
    settings.set(_k, _v, priority="cmdline")

process = CrawlerProcess(settings, install_root_handler=False)
process.crawl(
    GenericBrowserPriceSpider,
    workspace_id={workspace_id!r},
    scrape_job_id={scrape_job_id!r},
    match_ids={match_ids_arg!r},
    mode="BROWSER",
)
process.start()
"""


def run_generic_browser_price_spider_subprocess(
    *,
    workspace_id: object,
    scrape_job_id: object,
    match_ids: Iterable[object],
    extra_settings: Mapping[str, object] | None = None,
    commit_log_path: str | None = None,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``generic_browser_price_spider`` end-to-end in a fresh subprocess.

    No ``DNS_RESOLVER`` override is threaded through (see module
    docstring) — a caller pointing at a loopback fixture server should
    use ``http://127.0.0.1:{port}/...`` directly as the seeded match URL,
    never a custom hostname.

    ``commit_log_path``, if given, has one line appended to it for every
    DB transaction commit the crawl performs (batched-flush commit-count
    proof, mirrors the HTTP runner). Never raises on a non-zero exit —
    the caller asserts on ``returncode``/``stderr``.
    """
    commit_hook_source = ""
    if commit_log_path:
        commit_hook_source = textwrap.dedent(
            f"""
            from sqlalchemy import event as _event
            from app_shared.database import get_engine as _get_engine

            def _on_commit(_conn):
                with open({commit_log_path!r}, "a") as _f:
                    _f.write("1\\n")

            _event.listen(_get_engine(), "commit", _on_commit)
            """
        )

    script = _BROWSER_RUNNER_TEMPLATE.format(
        commit_hook_source=commit_hook_source,
        extra_settings=dict(extra_settings or {}),
        workspace_id=str(workspace_id),
        scrape_job_id=str(scrape_job_id),
        match_ids_arg=",".join(str(m) for m in match_ids),
    )

    fd, script_path = tempfile.mkstemp(suffix="_generic_browser_price_spider_runner.py")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(script)
        return subprocess.run(  # noqa: S603 - fixed interpreter, generated script, test-only
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=os.environ.copy(),
        )
    finally:
        os.unlink(script_path)
