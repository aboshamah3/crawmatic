"""Out-of-reactor process watchdog for Scrapyd crawl processes (2026-09-29, E1.3).

Why a thread, and why the runner
--------------------------------

`scrape_core.extensions.hard_deadline` (2026-09-23) is a
``reactor.callLater`` armed at ``spider_opened`` and cancelled at
``spider_closed``. That leaves three holes, all seen or provable:

* it runs ON the reactor, so anything that blocks the event loop (a
  synchronous call that never returns) also blocks the timer that should
  end it;
* it is armed only once the spider OPENS, so a process that hangs before
  that (the browser launch, a DB call in ``load_targets``) is never
  covered;
* it is cancelled at ``spider_closed``, but scrapy-playwright tears the
  browser down later, on ``engine_stopped`` -- and ``browser.close()`` /
  ``playwright.stop()`` have no bound (handler.py 407-411). A process that
  wedges there is past its own deadline's cancellation.

So the last line is a plain daemon thread started by the crawl RUNNER --
before Scrapy is imported -- that sleeps until the deadline and then kills
the whole process GROUP: the crawl, the Playwright driver and every
Chromium process. It is never cancelled; a process that ends normally takes
the daemon thread with it. ``os._exit`` alone (the old path) left Chromium
and the driver orphaned, which the image then had no init to reap.

The crawl makes itself a process-group leader first
(:func:`become_process_group_leader`); without that, killing "our group"
would kill Scrapyd itself, which spawned us into its own.

The deadline is ``max runtime + hard-kill grace + SCRAPE_PROCESS_WATCHDOG_EXTRA_SECONDS``
so the graceful ``CLOSESPIDER_TIMEOUT`` and the in-reactor hard deadline
always get their turn first, and it stays below the STARTED reaper horizon.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Callable

WATCHDOG_THREAD_NAME = "crawl-process-watchdog"

# Used only if the application settings cannot be read at runner start
# (the spider would then fail on its own); the largest configured deadline
# today (HTTP 1800 + 120 + 120), still below the 2100 s reaper horizon.
FALLBACK_DEADLINE_SECONDS = 2040


def become_process_group_leader() -> bool:
    """Put this process in a fresh process group (pgid = pid).

    Children spawned afterwards (Playwright driver, Chromium) inherit it,
    so one ``killpg`` reaches all of them and nothing else. Returns False
    when it is not possible (already a session leader), in which case the
    watchdog kills only this process.
    """
    try:
        os.setpgid(0, 0)
    except OSError:
        return os.getpgrp() == os.getpid()
    return True


def _kill_own_group() -> None:
    pgid = os.getpgrp()
    if pgid == os.getpid():
        os.killpg(pgid, signal.SIGKILL)
    else:
        # Never killpg a group we do not lead: it is Scrapyd's.
        os.kill(os.getpid(), signal.SIGKILL)


def arm_process_watchdog(
    deadline_seconds: float,
    *,
    kill: Callable[[], None] = _kill_own_group,
    label: str = "crawl",
) -> threading.Thread:
    """Start the watchdog thread; it kills the process group at the deadline."""

    def _run() -> None:
        # Event.wait, not time.sleep: identical here, but it keeps the
        # thread trivially testable and interrupt-safe.
        threading.Event().wait(deadline_seconds)
        # os.write, not logging: the thread that is wedged may hold the
        # logging lock, and this line must get out regardless.
        message = (
            f"CRITICAL process_watchdog: {label} pid={os.getpid()} still alive "
            f"{deadline_seconds:.0f}s after start; killing its process group "
            "(crawl + Playwright driver + Chromium) to free the Scrapyd slot\n"
        )
        try:
            os.write(sys.stderr.fileno(), message.encode())
        except Exception:  # noqa: BLE001 -- the kill must happen regardless
            pass
        kill()

    thread = threading.Thread(target=_run, name=WATCHDOG_THREAD_NAME, daemon=True)
    thread.start()
    return thread


def crawl_deadline_seconds(project: str) -> float:
    """The watchdog deadline for a crawl of `project` ("browser" or "http")."""
    try:
        from app_shared.config import get_settings

        settings = get_settings()
    except Exception as exc:  # noqa: BLE001 -- a crawl must never run unguarded
        os.write(
            sys.stderr.fileno(),
            f"WARNING process_watchdog: settings unreadable ({type(exc).__name__}); "
            f"using fallback deadline {FALLBACK_DEADLINE_SECONDS}s\n".encode(),
        )
        return float(FALLBACK_DEADLINE_SECONDS)
    runtime = (
        settings.SCRAPE_BROWSER_SPIDER_MAX_RUNTIME_SECONDS
        if project == "browser"
        else settings.SCRAPE_SPIDER_MAX_RUNTIME_SECONDS
    )
    return float(
        runtime
        + settings.SCRAPE_SPIDER_HARD_KILL_GRACE_SECONDS
        + settings.SCRAPE_PROCESS_WATCHDOG_EXTRA_SECONDS
    )


def arm_crawl_watchdog(project: str, argv: list[str] | None = None) -> threading.Thread | None:
    """Runner entry point: lead a process group and arm the watchdog.

    Only for ``crawl`` invocations: Scrapyd also runs the runner for
    ``list`` (listspiders.json), which must not leave its own group.
    """
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] != "crawl":
        return None
    become_process_group_leader()
    return arm_process_watchdog(crawl_deadline_seconds(project), label=f"{project} crawl")
