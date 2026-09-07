"""Unit tests for `scripts/dr/inventory_backups.py` (Task 0.1, F21).

Verifies the read-only inventory correctly marks archive coverage: a
file present (and content-matching) in the coverage `.tgz` is
`in_archive=True` / `sha256_matches_archive=True`; a file absent from
the archive is `in_archive=False` and gets `keep_reason ==
"newest-not-archived"` -- the plan's safety precondition for the
owner-gate deletion step (never delete something with no other copy).
"""

from __future__ import annotations

import sys
import tarfile
from pathlib import Path

# `scripts/` has no __init__.py / installed entry point -- match the
# sys.path convention `tests/unit/test_backfill_daily_rollups.py` uses to
# import `scripts.backfill_daily_rollups`.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dr.inventory_backups import build_inventory  # noqa: E402


def test_inventory_marks_archive_coverage(tmp_path: Path):
    root = tmp_path / "backups"
    (root / "dr").mkdir(parents=True)
    old = root / "dr" / "prod-20260901T000000Z.dump"
    old.write_bytes(b"old")
    new = root / "dr" / "prod-20260907T021502Z.dump"
    new.write_bytes(b"new")
    archive = tmp_path / "backups-2026-09-03.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(old, arcname="backups/dr/prod-20260901T000000Z.dump")
    rows = build_inventory(root=root, archive=archive)
    by_name = {r["path"].split("/")[-1]: r for r in rows}
    assert by_name["prod-20260901T000000Z.dump"]["in_archive"] is True
    assert by_name["prod-20260901T000000Z.dump"]["sha256_matches_archive"] is True
    assert by_name["prod-20260907T021502Z.dump"]["in_archive"] is False
    assert by_name["prod-20260907T021502Z.dump"]["keep_reason"] == "newest-not-archived"
