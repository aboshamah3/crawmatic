"""Scratch Postgres containers never leave an anonymous volume behind (2026-09-29, E10.2).

The postgres images declare `VOLUME /var/lib/postgresql/data`, so every
`docker run` of one creates an anonymous volume, and `docker rm -f <name>`
without `-v` leaves it on disk. 51 such orphans (~5 GB, from test and gate
runs on 2026-08-26 and 2026-09-04..09) filled the ops host, and the DR
backup refuses to dump below 2 GB free. `scripts/dr/verify_restore.sh`
already did it right (`docker rm -f -v`); this pins the rest.

Every `docker rm` in a script or test harness must carry `-v`, and every
`docker run` recipe must either use `--rm` (removes the anonymous volume
with the container) or sit in a file that tears down with `docker rm -f -v`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNED_DIRS = ("scripts", "tests")
SUFFIXES = {".sh", ".py", ".md"}


def _files() -> list[Path]:
    out = []
    for d in SCANNED_DIRS:
        for p in (REPO_ROOT / d).rglob("*"):
            if p.suffix in SUFFIXES and p.is_file() and p.name != Path(__file__).name:
                if "docker run" in p.read_text(errors="ignore") or "docker rm" in p.read_text(errors="ignore"):
                    out.append(p)
    return sorted(out)


def test_the_scan_finds_the_known_harnesses() -> None:
    names = {p.name for p in _files()}
    assert {"run_load_suite.sh", "verify_restore.sh", "test_rls_cross_workspace.py"} <= names


@pytest.mark.parametrize("path", _files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_removal_drops_the_volume(path: Path) -> None:
    text = path.read_text(errors="ignore")
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue  # prose about the rule, not a command
        match = re.search(r"docker rm\s+-[^\n`]*", line)
        if match is None:
            continue
        command = match.group(0)
        assert re.search(r"\s-(f?v|vf?)\b|\s-v\b", command), (
            f"{path.relative_to(REPO_ROOT)}: `{command.strip()}` leaves the container's "
            "anonymous data volume behind; use `docker rm -f -v`"
        )


@pytest.mark.parametrize("path", _files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_run_is_removed_with_its_volume(path: Path) -> None:
    text = path.read_text(errors="ignore")
    runs = [
        m.group(0)
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        for m in [re.search(r"docker run\s+-[^\n]*", line)]
        if m is not None
    ]
    if not runs:
        return
    tears_down = bool(re.search(r"docker rm -f -v|docker rm -v -f|docker rm -fv", text))
    for command in runs:
        assert "--rm" in command or tears_down, (
            f"{path.relative_to(REPO_ROOT)}: `{command.strip()}` has no --rm and the file "
            "never runs `docker rm -f -v`, so its anonymous volume outlives it"
        )
