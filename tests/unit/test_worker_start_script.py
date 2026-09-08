"""`apps/workers/start.sh` — two Celery consumer pools in one container
(EPA B4, F09).

Pure shell-script assertions: `bash -n` for syntax, then the exact
rendered `celery ... worker` command lines the packet's acceptance
criteria name, verbatim. The exit-code/kill behavior (`wait -n`, then
kill the other, always non-zero) is exercised end-to-end against a fake
`celery` executable on `PATH` -- no real Celery/Redis needed.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
START_SCRIPT = REPO_ROOT / "apps" / "workers" / "start.sh"

_CRITICAL_CMD = (
    "-Q scrape_dispatch,maintenance -c $CELERY_CRITICAL_CONCURRENCY -n critical@%h"
)
_BULK_CMD = (
    "-Q price_analysis,strategy_discovery,webhook_events "
    "-c $CELERY_BULK_CONCURRENCY -n bulk@%h"
)


def test_start_script_exists_and_is_executable() -> None:
    assert START_SCRIPT.is_file(), START_SCRIPT
    # Owner-executable bit set (0o100).
    assert START_SCRIPT.stat().st_mode & 0o100, "start.sh must be executable"


def test_start_script_passes_bash_syntax_check() -> None:
    result = subprocess.run(
        ["bash", "-n", str(START_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_start_script_renders_exactly_two_celery_worker_commands() -> None:
    text = START_SCRIPT.read_text()
    celery_worker_lines = [
        line
        for line in text.splitlines()
        if line.strip().startswith("celery ") and "worker" in line
    ]
    assert len(celery_worker_lines) == 2, celery_worker_lines


def test_start_script_renders_the_critical_pool_command() -> None:
    text = START_SCRIPT.read_text()
    assert _CRITICAL_CMD in text, text


def test_start_script_renders_the_bulk_pool_command() -> None:
    text = START_SCRIPT.read_text()
    assert _BULK_CMD in text, text


def test_start_script_uses_wait_dash_n_then_kills_the_other() -> None:
    text = START_SCRIPT.read_text()
    assert "wait -n" in text
    assert "kill " in text


def _run_with_fake_celery(tmp_path: Path, fake_celery_body: str) -> subprocess.CompletedProcess:
    """Run `start.sh` with a stub `celery` executable prepended to PATH.

    `fake_celery_body` is a bash script body that receives the real
    `celery` CLI args in `$*` (e.g. it can branch on `critical@%h` vs
    `bulk@%h` appearing in `$*`).
    """
    fake_celery = tmp_path / "celery"
    fake_celery.write_text(
        "#!/usr/bin/env bash\n" + textwrap.dedent(fake_celery_body)
    )
    fake_celery.chmod(0o755)
    return subprocess.run(
        ["bash", str(START_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=15,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin"},
    )


def test_start_script_exits_nonzero_when_bulk_pool_exits_nonzero(tmp_path: Path) -> None:
    """The bulk pool dying (simulated) must end the whole script non-zero
    and must not leave the critical pool's process running."""
    result = _run_with_fake_celery(
        tmp_path,
        """
        args="$*"
        if [[ "$args" == *"critical@%h"* ]]; then
            exec sleep 5
        else
            sleep 0.2
            exit 7
        fi
        """,
    )
    assert result.returncode != 0, result.stdout + result.stderr


def test_start_script_forces_nonzero_even_on_a_clean_child_exit(tmp_path: Path) -> None:
    """Either pool exiting is unexpected (both are meant to run forever)
    -- even a clean `exit 0` must still fail the script."""
    result = _run_with_fake_celery(
        tmp_path,
        """
        args="$*"
        if [[ "$args" == *"critical@%h"* ]]; then
            exec sleep 5
        else
            sleep 0.2
            exit 0
        fi
        """,
    )
    assert result.returncode != 0, result.stdout + result.stderr


def test_start_script_kills_the_surviving_pool(tmp_path: Path) -> None:
    """When one pool exits, the still-running one must actually receive
    the kill signal -- not be left running as an orphan. The marker file
    is removed by the surviving pool's OWN `TERM` trap, so its absence
    proves `start.sh` actually signaled that process (not merely that
    the script itself returned)."""
    marker = tmp_path / "critical_still_running"
    result = _run_with_fake_celery(
        tmp_path,
        f"""
        args="$*"
        if [[ "$args" == *"critical@%h"* ]]; then
            touch {marker}
            trap 'rm -f {marker}; exit 143' TERM
            sleep 5
        else
            sleep 0.2
            exit 3
        fi
        """,
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert not marker.exists(), "critical pool's marker file was not cleaned up on TERM"


if __name__ == "__main__":
    sys.exit(0)
