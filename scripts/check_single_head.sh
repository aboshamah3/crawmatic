#!/usr/bin/env bash
# check_single_head.sh — single linear migration history guard (FR-012, SC-006).
#
# Runs `alembic heads` and exits non-zero unless exactly ONE head is
# reported. DB-independent (Alembic resolves heads by reading the
# `alembic/versions/*.py` files' revision graph — no DB connection is
# ever opened; see alembic/env.py's offline path).
#
# CI wiring: this script is meant to be invoked by the CI workflow as a
# dedicated step, e.g.:
#
#   - name: Single-head migration guard
#     run: bash scripts/check_single_head.sh
#
# It must run after `uv sync` (alembic needs to be installed) and before
# any deploy/migrate step, so a branch that accidentally forks the
# migration history (two heads) fails CI instead of reaching the
# one-shot migrate job (contracts/migration-job.md).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

heads_output="$(uv run alembic heads 2>&1)"
head_count="$(printf '%s\n' "$heads_output" | grep -c '(head)' || true)"

if [[ "$head_count" -eq 0 ]]; then
    echo "check_single_head: FAIL — no heads found (expected exactly 1)." >&2
    echo "$heads_output" >&2
    exit 1
fi

if [[ "$head_count" -ne 1 ]]; then
    echo "check_single_head: FAIL — found $head_count heads, expected exactly 1 (multiple heads = diverged migration history)." >&2
    echo "$heads_output" >&2
    exit 1
fi

echo "check_single_head: OK — exactly 1 head."
echo "$heads_output"
exit 0
