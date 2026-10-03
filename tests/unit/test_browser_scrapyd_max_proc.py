"""The browser node's Scrapyd `max_proc` is set from the environment (2026-09-29, E3.4).

`max_proc = 1` was baked into `apps/scrapers-browser/scrapyd.conf`; changing
the browser node's process concurrency meant a code change and an image
rebuild. The entrypoint now renders it from `SCRAPYD_MAX_PROC` (default 1,
today's value), so sizing it to a measured instance is an env change. The
production value is an owner decision (see docs/ops/CAPACITY.md).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NODE = REPO_ROOT / "apps" / "scrapers-browser"


def _render(tmp_path: Path, extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    shutil.copy(NODE / "scrapyd.conf", tmp_path / "scrapyd.conf")
    shutil.copy(NODE / "docker-entrypoint.sh", tmp_path / "docker-entrypoint.sh")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "python"
    stub.write_text("#!/bin/sh\nexit 0\n")  # the result-spool check is not under test
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "SCRAPYD_USERNAME": "u",
        "SCRAPYD_PASSWORD": "p",
        **extra_env,
    }
    return subprocess.run(
        ["sh", "docker-entrypoint.sh", "true"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _max_proc(tmp_path: Path) -> str:
    for line in (tmp_path / "scrapyd.conf").read_text().splitlines():
        if line.replace(" ", "").startswith("max_proc="):
            return line.split("=", 1)[1].strip()
    raise AssertionError("no max_proc line")


def test_default_is_todays_single_process(tmp_path: Path) -> None:
    proc = _render(tmp_path, {})
    assert proc.returncode == 0, proc.stderr
    assert _max_proc(tmp_path) == "1"


def test_env_sets_it(tmp_path: Path) -> None:
    proc = _render(tmp_path, {"SCRAPYD_MAX_PROC": "3"})
    assert proc.returncode == 0, proc.stderr
    assert _max_proc(tmp_path) == "3"


@pytest.mark.parametrize("bad", ["0", "-1", "two", "3; rm"])
def test_a_non_positive_or_non_numeric_value_refuses_to_start(tmp_path: Path, bad: str) -> None:
    proc = _render(tmp_path, {"SCRAPYD_MAX_PROC": bad})
    assert proc.returncode != 0
    assert "SCRAPYD_MAX_PROC" in proc.stderr
