#!/usr/bin/env python3
"""Write `packages.json` — a point-in-time software inventory of the image
this runs inside (EPA A4/F04).

Run as a build step (see `apps/scrapers/Dockerfile` and
`apps/scrapers-browser/Dockerfile`) so the inventory is baked into the
image itself rather than reconstructed later from a build log that may
not exist anymore. Records four things, each best-effort and each
reported as absent (never as a hard failure) when the tool it shells out
to is not present in this particular image:

* ``python``            — the interpreter version running this script.
* ``pip_freeze``        — the resolved Python dependency set, via
  ``uv pip freeze`` (works against a `uv`-managed venv with no `pip`
  installed) falling back to plain ``pip freeze``.
* ``dpkg``               — every OS package installed via `dpkg -l`
  (present on every `python:*-slim-bookworm` base this repo builds from).
* ``browser``           — the Playwright package version plus the actual
  Chromium executable's own `--version` output, resolved from
  `$PLAYWRIGHT_BROWSERS_PATH` (falling back to Playwright's default
  cache dir) — absent on `apps/scrapers`'s plain image, which installs no
  browser.

This is deliberately NOT wired into `scripts/build_release_manifest.py`
in this change (that script is out of this task's file scope) — the
release manifest's existing generic `--evidence key=value` mechanism is
the intended link: a caller building the manifest can pass
``--evidence packages_inventory=sha256:<hash of this image's packages.json>``
(or a URL/path to it) to record which inventory snapshot a given
`manifest_id` corresponds to, the same way it already links test/scan
evidence.

Exit code is always 0 — an inventory that could not fully populate itself
(e.g. no `dpkg` on a non-Debian base) still writes what it could, with the
gap recorded in the JSON rather than failing the build over it. A build
step that MUST verify inventory freshness should assert on the JSON's
content, not on this script's exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DEFAULT_PLAYWRIGHT_CACHE = Path.home() / ".cache" / "ms-playwright"


def _run(cmd: list[str], **kwargs: Any) -> str | None:
    """Run `cmd`, returning stripped stdout, or `None` on any failure.

    Never raises: a missing binary (`FileNotFoundError`), a non-zero exit,
    or a timeout are all "this tool is absent in this image" — exactly the
    condition this inventory is allowed to record as absent rather than
    treat as a build-breaking error.
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=kwargs.pop("timeout", 30),
            check=False,
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def python_record() -> dict[str, Any]:
    return {
        "version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "executable": sys.executable,
    }


def pip_freeze_record() -> dict[str, Any]:
    """`uv pip freeze` first (works with no `pip` installed in the venv),
    falling back to plain `pip freeze` for a non-uv environment."""
    out = _run(["uv", "pip", "freeze"])
    tool = "uv pip freeze"
    if out is None:
        out = _run([sys.executable, "-m", "pip", "freeze"])
        tool = "pip freeze"
    if out is None:
        return {"available": False, "tool": None, "packages": []}
    packages = [line for line in out.splitlines() if line and not line.startswith("#")]
    return {"available": True, "tool": tool, "packages": sorted(packages)}


def dpkg_record() -> dict[str, Any]:
    """Every `dpkg -l` entry as `{name, version, architecture}` — present
    on every Debian-slim base this repo's Dockerfiles build from."""
    out = _run(["dpkg-query", "-W", "-f=${Package}\\t${Version}\\t${Architecture}\\n"])
    if out is None:
        return {"available": False, "packages": []}
    packages = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, version, arch = parts
        packages.append({"name": name, "version": version, "architecture": arch})
    return {"available": True, "packages": sorted(packages, key=lambda p: p["name"])}


def _find_chromium_executable(browsers_path: Path) -> Path | None:
    if not browsers_path.is_dir():
        return None
    for candidate in sorted(browsers_path.glob("chromium*/**/chrome")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    for candidate in sorted(browsers_path.glob("chromium*/**/headless_shell")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def browser_record() -> dict[str, Any]:
    """Playwright's own package version plus the installed Chromium
    binary's `--version` — absent entirely on an image (like
    `apps/scrapers`) that never installs a browser."""
    playwright_version = _run([sys.executable, "-m", "playwright", "--version"])
    browsers_path = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or _DEFAULT_PLAYWRIGHT_CACHE)
    chromium_exe = _find_chromium_executable(browsers_path)
    chromium_version = _run([str(chromium_exe), "--version"]) if chromium_exe else None
    return {
        "available": playwright_version is not None or chromium_exe is not None,
        "playwright_version": playwright_version,
        "browsers_path": str(browsers_path),
        "chromium_executable": str(chromium_exe) if chromium_exe else None,
        "chromium_version": chromium_version,
    }


def build_inventory() -> dict[str, Any]:
    return {
        "record_type": "image_packages_inventory",
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": python_record(),
        "pip_freeze": pip_freeze_record(),
        "dpkg": dpkg_record(),
        "browser": browser_record(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="packages.json",
        type=Path,
        help="output path for the inventory JSON (default: ./packages.json)",
    )
    args = parser.parse_args(argv)

    inventory = build_inventory()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"image_inventory: wrote {args.out} "
          f"({len(inventory['pip_freeze']['packages'])} pip package(s), "
          f"{len(inventory['dpkg']['packages'])} dpkg package(s), "
          f"chromium={'present' if inventory['browser']['chromium_version'] else 'absent'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
