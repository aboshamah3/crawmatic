"""Optional compose smoke test (T044, quickstart §2).

Brings the full eight-component stack up with ``docker compose`` and
asserts every service reaches a running/healthy state (SC-001), then
tears the stack down again.

This test needs a reachable Docker daemon and Docker Compose v2 — it is
**not** runnable in daemon-less environments (e.g. sandboxes without a
running dockerd). It SKIPS cleanly whenever:

* the ``docker`` CLI isn't on ``PATH``, or
* ``docker compose`` (the v2 plugin) isn't available, or
* the daemon isn't reachable (``docker info`` fails — for example
  because ``/var/run/docker.sock`` exists but nothing is listening, or
  the current user lacks permission to use it).

Where Docker *is* available, this test actually drives
``docker compose up --build -d`` / ``docker compose ps`` / a full
teardown, aligned with quickstart.md §2 ("Bring up the whole stack").
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
ENV_EXAMPLE = REPO_ROOT / ".env.example"

# The eight components declared in docker-compose.yml (plan.md §4 / data-model.md).
EXPECTED_SERVICES = {
    "postgres",
    "pgbouncer",
    "redis",
    "api",
    "scheduler",
    "worker",
    "scrapers",
    "scrapers-browser",
}

# Generous bring-up budget: five images build from scratch plus
# Playwright's Chromium install in the browser Scrapyd image.
_UP_TIMEOUT_S = 900
_DOWN_TIMEOUT_S = 120


def _docker_available() -> bool:
    """True only if the docker CLI, the compose plugin, and a live daemon are all present."""
    if shutil.which("docker") is None:
        return False
    try:
        info = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if info.returncode != 0:
        return False

    try:
        compose_version = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return compose_version.returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon / compose v2 not available in this environment",
)


@pytest.fixture()
def env_file(tmp_path: Path) -> Path:
    """A throwaway `.env`, seeded from `.env.example`, for `--env-file`."""
    dest = tmp_path / ".env"
    dest.write_text(ENV_EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


def _compose(*args: str, env_file: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "--env-file",
            str(env_file),
            "-p",
            "crawmatic-smoke",
            *args,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_all_eight_components_reach_running_or_healthy(env_file: Path) -> None:
    """`docker compose up --build -d` brings all eight components up (SC-001)."""
    try:
        up = _compose("up", "--build", "-d", env_file=env_file, timeout=_UP_TIMEOUT_S)
        assert up.returncode == 0, f"compose up failed:\n{up.stdout}\n{up.stderr}"

        ps = _compose(
            "ps", "--format", "json", env_file=env_file, timeout=60
        )
        assert ps.returncode == 0, f"compose ps failed:\n{ps.stdout}\n{ps.stderr}"

        # `docker compose ps --format json` emits either one JSON array or
        # newline-delimited JSON objects depending on compose version.
        raw = ps.stdout.strip()
        if raw.startswith("["):
            containers = json.loads(raw)
        else:
            containers = [json.loads(line) for line in raw.splitlines() if line.strip()]

        seen_services = {c["Service"] for c in containers}
        assert seen_services == EXPECTED_SERVICES, (
            f"expected {EXPECTED_SERVICES}, compose reported {seen_services}"
        )

        for container in containers:
            state = container.get("State", "")
            health = container.get("Health", "")
            service = container["Service"]
            ok = state == "running" and health in ("", "healthy", "starting")
            assert ok, f"service {service!r} not running/healthy: state={state!r} health={health!r}"
    finally:
        _compose("down", "-v", env_file=env_file, timeout=_DOWN_TIMEOUT_S)
