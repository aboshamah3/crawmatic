"""``scrape_core.errors.classify_playwright_exception`` unit tests
(SPEC-14 T012, R3, `contracts/browser-spider.md`, quickstart.md scenario 3).

Pure/off-reactor -- no Chromium, no real browser, no reactor. Exercises
the browser-mode single-attempt failure classification: Playwright's own
``TimeoutError`` (a navigation/``wait_for_selector``/
``wait_for_load_state`` timeout) must classify as ``TIMEOUT``; any other
Playwright error (browser/context crash, protocol error, target-closed,
etc.) must classify as ``PLAYWRIGHT_FAILED`` -- both reuse existing
``ScrapeErrorCode`` members (SPEC-07 forward-compat placeholders), no
enum change.
"""

from __future__ import annotations

from app_shared.enums import ScrapeErrorCode

from scrape_core.errors import classify_playwright_exception


class _PlaywrightUnrelatedError(Exception):
    """Stands in for some other Playwright-raised error (browser/context
    crash, protocol error, target closed, etc.) -- not a timeout, no
    recognizable name substring."""


class _CustomNamedTimeoutError(Exception):
    """A differently-packaged timeout-like exception (e.g. a wrapper that
    doesn't preserve the real ``playwright`` type) -- exercises the
    duck-typed-by-name fallback, mirroring `classify_exception`'s own
    convention for cases where the concrete type isn't recognized."""


# --- Playwright's own TimeoutError -> TIMEOUT -------------------------------


def test_playwright_timeout_error_classifies_as_timeout() -> None:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    exc = PlaywrightTimeoutError("Timeout 30000ms exceeded.")

    assert classify_playwright_exception(exc) == ScrapeErrorCode.TIMEOUT


def test_playwright_sync_timeout_error_also_classifies_as_timeout() -> None:
    """`playwright.sync_api.TimeoutError` and `playwright.async_api.TimeoutError`
    are the same underlying `playwright._impl._errors.TimeoutError` class --
    confirm the async import this module uses still recognizes an instance
    raised via the sync alias."""
    from playwright.sync_api import TimeoutError as SyncPlaywrightTimeoutError

    exc = SyncPlaywrightTimeoutError("Timeout 5000ms exceeded.")

    assert classify_playwright_exception(exc) == ScrapeErrorCode.TIMEOUT


def test_duck_typed_timeout_named_exception_classifies_as_timeout() -> None:
    """Even when the concrete Playwright type isn't recognized (e.g. a
    wrapper re-raised a differently packaged exception), a `...TimeoutError`-
    named class still classifies as `TIMEOUT` (mirrors `classify_exception`'s
    own by-name fallback)."""
    exc = _CustomNamedTimeoutError("some wrapped timeout")

    assert classify_playwright_exception(exc) == ScrapeErrorCode.TIMEOUT


# --- any other Playwright error -> PLAYWRIGHT_FAILED ------------------------


def test_other_playwright_error_classifies_as_playwright_failed() -> None:
    from playwright.async_api import Error as PlaywrightError

    exc = PlaywrightError("Target page, context or browser has been closed")

    assert classify_playwright_exception(exc) == ScrapeErrorCode.PLAYWRIGHT_FAILED


def test_unrelated_exception_classifies_as_playwright_failed() -> None:
    """The browser path has no retry ladder (R4) -- any exception that
    isn't a recognized timeout is the catch-all `PLAYWRIGHT_FAILED`, never
    `UNKNOWN_ERROR` (that code is reserved for the HTTP path's
    `classify_exception`)."""
    exc = _PlaywrightUnrelatedError("browser crashed")

    assert classify_playwright_exception(exc) == ScrapeErrorCode.PLAYWRIGHT_FAILED


def test_plain_exception_classifies_as_playwright_failed() -> None:
    exc = Exception("some other failure")

    assert classify_playwright_exception(exc) == ScrapeErrorCode.PLAYWRIGHT_FAILED
