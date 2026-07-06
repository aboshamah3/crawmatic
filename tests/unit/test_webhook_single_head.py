"""Single-Alembic-head guard for the SPEC-16 webhooks migration (T013).

Asserts `alembic heads` reports exactly one head after adding
`03dec3037c8f_webhook_events_and_endpoints.py` (linear chain,
`down_revision='4a1dca402f78'`, the verified SPEC-15 head) — so
`tests/unit/test_strategy_single_head.py::test_alembic_heads_reports_exactly_one_head`
(and every other single-head guard) stays green.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_alembic(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_alembic_heads_reports_exactly_one_head_after_webhooks_migration() -> None:
    result = _run_alembic("heads")

    assert result.returncode == 0, (
        f"alembic heads failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )

    head_lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    assert len(head_lines) == 1, f"expected exactly one head, got: {head_lines!r}"
    assert "03dec3037c8f" in head_lines[0]
    assert "(head)" in head_lines[0]


def test_webhooks_migration_down_revision_is_spec15_head() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "4a1dca402f78 -> 03dec3037c8f" in result.stdout
