"""Single-Alembic-head guard for the SPEC-16 webhooks migration (T013).

Asserts `alembic heads` reports exactly one head after adding
`03dec3037c8f_webhook_events_and_endpoints.py` (linear chain,
`down_revision='4a1dca402f78'`, the verified SPEC-15 head) — so
`tests/unit/test_strategy_single_head.py::test_alembic_heads_reports_exactly_one_head`
(and every other single-head guard) stays green.

Phase 2 Task 5 (`f87cf9a237cd_usage_export_indexes.py`, PLAN risk P2)
chains on top of `03dec3037c8f`, moving the alembic head forward, so
the head assertion below now targets `f87cf9a237cd` instead of
`03dec3037c8f`. The `03dec3037c8f` down_revision assertion is untouched
since that edge in the linear history is unaffected.

Phase 4 Task 2 (`5b9a86717a66_normalise_api_key_status.py`,
phase4-connect) chains on top of `f87cf9a237cd`, moving the alembic
head forward again, so the head assertion below now targets
`5b9a86717a66`. Same pattern as the Task 5 update above: only the head
assertion changes, not the down_revision chain below.
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
    # Head moved forward from 03dec3037c8f to f87cf9a237cd (Task 5,
    # usage_export_indexes), then to 5b9a86717a66 (phase4-connect Task 2,
    # normalise_api_key_status) after this migration's own single-head
    # guard was written; only the head assertion changes, not the
    # down_revision chain below.
    assert "5b9a86717a66" in head_lines[0]
    assert "(head)" in head_lines[0]


def test_webhooks_migration_down_revision_is_spec15_head() -> None:
    result = _run_alembic("history")
    assert result.returncode == 0, (
        f"alembic history failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "4a1dca402f78 -> 03dec3037c8f" in result.stdout
