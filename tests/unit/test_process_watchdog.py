"""The out-of-reactor crawl watchdog (2026-09-29, plan E1.3).

The in-reactor hard deadline (`scrape_core.extensions.hard_deadline`) is a
`reactor.callLater` armed at spider_opened and cancelled at spider_closed:
it cannot fire while the loop is blocked, before the spider opens, or
during Playwright's unbounded shutdown on engine_stopped. These tests run
real subprocesses, block them in exactly those ways, and require that the
watchdog kills the process AND its children (the Playwright driver and
Chromium in production; a `sleep` here).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from scrape_core import process_watchdog

REPO_ROOT = Path(__file__).resolve().parents[2]

_ENV = {
    "DATABASE_URL": "postgresql+psycopg://crawmatic:crawmatic@pgbouncer:6432/crawmatic",
    "REDIS_URL": "redis://redis:6379/0",
    "SCRAPYD_HTTP_URLS": "http://scrapers:6800",
    "SCRAPYD_BROWSER_URLS": "http://scrapers-browser:6800",
    "SCRAPYD_USERNAME": "scrapyd",
    "SCRAPYD_PASSWORD": "change-me",
    "JWT_SECRET": "test-jwt-secret",
    "ENCRYPTION_KEYS": "1:DDdqY9HwOBbYpfuS_6K-Z_fa75VD5fxAt0HNkdYP940=",
}

_PRELUDE = textwrap.dedent(
    """\
    import asyncio, subprocess, sys, time
    from scrape_core.process_watchdog import arm_process_watchdog, become_process_group_leader
    assert become_process_group_leader()
    child = subprocess.Popen(["sleep", "60"])
    print(child.pid, flush=True)
    arm_process_watchdog(0.5)
    """
)


def _pid_alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            state = fh.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


def _run_blocked(body: str) -> tuple[subprocess.CompletedProcess[str], int]:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", _PRELUDE + textwrap.dedent(body)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 10, f"watchdog did not fire (ran {elapsed:.1f}s)"
    child_pid = int(proc.stdout.split()[0])
    return proc, child_pid


def _assert_group_killed(proc: subprocess.CompletedProcess[str], child_pid: int) -> None:
    assert proc.returncode == -signal.SIGKILL, proc.stderr
    assert "process_watchdog" in proc.stderr
    deadline = time.monotonic() + 3
    while _pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(child_pid), "the child (Chromium's stand-in) was orphaned"


def test_fires_while_the_main_thread_is_blocked_in_a_synchronous_call() -> None:
    proc, child = _run_blocked("time.sleep(30)\n")
    _assert_group_killed(proc, child)


def test_fires_while_an_asyncio_shutdown_await_never_resolves() -> None:
    """The shape of a hung `browser.close()` / `playwright.stop()`: the loop
    is alive, the awaited future never completes."""
    body = """\
    async def hung_close():
        await asyncio.get_running_loop().create_future()
    asyncio.run(hung_close())
    """
    proc, child = _run_blocked(body)
    _assert_group_killed(proc, child)


def test_a_process_that_finishes_in_time_exits_normally() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from scrape_core.process_watchdog import arm_process_watchdog\n"
            "arm_process_watchdog(30)\nprint('done')\n",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == "done"


def test_only_crawl_invocations_arm_it() -> None:
    assert process_watchdog.arm_crawl_watchdog("browser", ["list"]) is None
    assert process_watchdog.arm_crawl_watchdog("browser", []) is None


@pytest.mark.parametrize("project", ["browser", "http"])
def test_the_deadline_comes_after_both_graceful_deadlines_and_before_the_reaper(
    project: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app_shared.config import get_settings

    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    try:
        cfg = get_settings()
        deadline = process_watchdog.crawl_deadline_seconds(project)
    finally:
        get_settings.cache_clear()
    runtime = (
        cfg.SCRAPE_BROWSER_SPIDER_MAX_RUNTIME_SECONDS
        if project == "browser"
        else cfg.SCRAPE_SPIDER_MAX_RUNTIME_SECONDS
    )
    assert deadline > runtime + cfg.SCRAPE_SPIDER_HARD_KILL_GRACE_SECONDS
    assert deadline < cfg.SCRAPE_STARTED_REAP_AFTER_SECONDS
    assert deadline <= process_watchdog.FALLBACK_DEADLINE_SECONDS


@pytest.mark.parametrize(
    ("runner", "project"),
    [
        (REPO_ROOT / "apps" / "scrapers-browser" / "asyncio_runner.py", "browser"),
        (REPO_ROOT / "apps" / "scrapers" / "watchdog_runner.py", "http"),
    ],
)
def test_both_scrapyd_runners_arm_the_watchdog_before_scrapyd_starts(runner: Path, project: str) -> None:
    text = runner.read_text()
    call = f'arm_crawl_watchdog("{project}")'
    assert call in text
    assert text.index(call) < text.index("from scrapyd.runner import main")


def test_the_http_node_uses_the_watchdog_runner() -> None:
    conf = (REPO_ROOT / "apps" / "scrapers" / "scrapyd.conf").read_text()
    assert "\nrunner        = watchdog_runner\n" in conf


def test_the_browser_image_runs_under_an_init_that_reaps() -> None:
    dockerfile = (REPO_ROOT / "apps" / "scrapers-browser" / "Dockerfile").read_text()
    assert "tini" in dockerfile
    assert 'ENTRYPOINT ["/usr/bin/tini", "-g", "--", "./docker-entrypoint.sh"]' in dockerfile


def test_never_kills_a_group_it_does_not_lead(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr(os, "getpgrp", lambda: 1)
    monkeypatch.setattr(os, "getpid", lambda: 4242)
    monkeypatch.setattr(os, "killpg", lambda g, s: calls.append(("killpg", g, s)))
    monkeypatch.setattr(os, "kill", lambda p, s: calls.append(("kill", p, s)))
    process_watchdog._kill_own_group()
    assert calls == [("kill", 4242, signal.SIGKILL)]
