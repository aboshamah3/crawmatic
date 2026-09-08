"""The evidence-blob retention sweep and its reference gate (EPA C5, F19).

Closes the gap `docs/RETENTION_POLICY.md` §2.1 records verbatim:

    "**No deletion mechanism exists today** ... it is recorded here as an
    open gap the owner must decide on before evidence accumulates
    without bound"

and the shape that section prescribes for the mechanism when it is
built:

    "filesystem object deletion keyed by hash, gated on no
    `price_observations.offer_raw_evidence_hash` row still referencing
    it (a hash referenced by a NOT-yet-retention-eligible observation
    must not be deleted out from under it)"

So the sweep has exactly two conditions and both must hold before a
blob is removed: the blob is older than `EVIDENCE_RETENTION_DAYS`, AND
no observation still inside its OWN retention window references it.
The named acceptance case: a blob referenced by a 10-day-old
observation is kept; an unreferenced 40-day-old blob is deleted.

Pure filesystem + a fake session — no DB.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app_shared.observations.evidence_store import (
    EVIDENCE_RETENTION_DAYS,
    EvidenceNotFoundError,
    resolve_hash,
    store_evidence,
    sweep_evidence_retention,
)

NOW = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


class _FakeResult:
    def __init__(self, hashes: list[str]) -> None:
        self._hashes = hashes

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[str]:
        return self._hashes


class _FakeSession:
    """Answers the sweep's ONE question: which of these hashes are still
    referenced by an observation younger than its own retention?"""

    def __init__(self, referenced: set[str] | None = None) -> None:
        self.referenced = referenced or set()
        self.queries: list[tuple[str, Any]] = []

    def execute(self, stmt: Any, params: Any = None) -> _FakeResult:
        self.queries.append((str(stmt), params))
        asked = list((params or {}).get("hashes", []))
        return _FakeResult([h for h in asked if h in self.referenced])


def _age_blob(store_dir: Path, evidence_hash: str, *, days: int) -> Path:
    path = store_dir / evidence_hash[:2] / evidence_hash[2:4] / evidence_hash
    stamp = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


# --------------------------------------------------------------------------
# The policy constant
# --------------------------------------------------------------------------


def test_the_retention_window_is_thirty_days() -> None:
    assert EVIDENCE_RETENTION_DAYS == 30


def test_the_setting_mirrors_the_module_default() -> None:
    from app_shared.config import Settings

    assert Settings.model_fields["EVIDENCE_RETENTION_DAYS"].default == EVIDENCE_RETENTION_DAYS


# --------------------------------------------------------------------------
# The two named acceptance cases
# --------------------------------------------------------------------------


def test_an_unreferenced_forty_day_old_blob_is_deleted(tmp_path: Path) -> None:
    evidence_hash = store_evidence(b"forty days old, nobody references me", store_dir=tmp_path)
    _age_blob(tmp_path, evidence_hash, days=40)
    session = _FakeSession(referenced=set())

    report = sweep_evidence_retention(session, store_dir=tmp_path, now=NOW)

    assert report.blobs_deleted == 1
    assert report.blobs_kept_referenced == 0
    with pytest.raises(EvidenceNotFoundError):
        resolve_hash(evidence_hash, store_dir=tmp_path)


def test_a_blob_referenced_by_a_ten_day_old_observation_is_kept(tmp_path: Path) -> None:
    data = b"forty days old but still referenced"
    evidence_hash = store_evidence(data, store_dir=tmp_path)
    _age_blob(tmp_path, evidence_hash, days=40)
    # The observation is 10 days old -- comfortably inside
    # RETENTION_PRICE_OBSERVATIONS_DAYS -- so the row that points here
    # will still be readable long after the blob's own 30-day window.
    session = _FakeSession(referenced={evidence_hash})

    report = sweep_evidence_retention(session, store_dir=tmp_path, now=NOW)

    assert report.blobs_deleted == 0
    assert report.blobs_kept_referenced == 1
    assert resolve_hash(evidence_hash, store_dir=tmp_path) == data


def test_a_young_blob_is_never_even_considered(tmp_path: Path) -> None:
    data = b"ten days old"
    evidence_hash = store_evidence(data, store_dir=tmp_path)
    _age_blob(tmp_path, evidence_hash, days=10)
    session = _FakeSession(referenced=set())

    report = sweep_evidence_retention(session, store_dir=tmp_path, now=NOW)

    assert report.blobs_scanned == 1
    assert report.blobs_expired == 0
    assert report.blobs_deleted == 0
    # No expired candidate -> nothing to ask the database about.
    assert session.queries == []
    assert resolve_hash(evidence_hash, store_dir=tmp_path) == data


# --------------------------------------------------------------------------
# Bounds and safety
# --------------------------------------------------------------------------


def test_dry_run_deletes_nothing_but_reports_what_it_would(tmp_path: Path) -> None:
    evidence_hash = store_evidence(b"candidate", store_dir=tmp_path)
    _age_blob(tmp_path, evidence_hash, days=40)
    session = _FakeSession(referenced=set())

    report = sweep_evidence_retention(session, store_dir=tmp_path, now=NOW, dry_run=True)

    assert report.blobs_deleted == 1
    assert report.dry_run is True
    assert resolve_hash(evidence_hash, store_dir=tmp_path) == b"candidate"


def test_a_run_is_bounded_and_says_so(tmp_path: Path) -> None:
    for index in range(5):
        evidence_hash = store_evidence(f"blob {index}".encode(), store_dir=tmp_path)
        _age_blob(tmp_path, evidence_hash, days=40)
    session = _FakeSession(referenced=set())

    report = sweep_evidence_retention(
        session, store_dir=tmp_path, now=NOW, max_blobs=2
    )

    assert report.blobs_deleted == 2
    assert report.truncated is True


def test_a_missing_store_directory_is_a_no_op_not_an_error(tmp_path: Path) -> None:
    report = sweep_evidence_retention(
        _FakeSession(), store_dir=tmp_path / "never-mounted", now=NOW
    )
    assert report.blobs_scanned == 0
    assert report.blobs_deleted == 0


def test_a_database_failure_deletes_nothing(tmp_path: Path) -> None:
    """A sweep that cannot PROVE a blob is unreferenced must not delete it."""

    evidence_hash = store_evidence(b"candidate", store_dir=tmp_path)
    _age_blob(tmp_path, evidence_hash, days=40)

    class _BrokenSession:
        def execute(self, stmt: Any, params: Any = None) -> Any:
            raise RuntimeError("database is unreachable")

    with pytest.raises(RuntimeError):
        sweep_evidence_retention(_BrokenSession(), store_dir=tmp_path, now=NOW)

    assert resolve_hash(evidence_hash, store_dir=tmp_path) == b"candidate"


def test_stray_non_hash_files_are_left_alone(tmp_path: Path) -> None:
    stray = tmp_path / "ab" / "cd" / "README.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("not an evidence blob")
    stamp = (NOW - timedelta(days=400)).timestamp()
    os.utime(stray, (stamp, stamp))

    report = sweep_evidence_retention(_FakeSession(), store_dir=tmp_path, now=NOW)

    assert report.blobs_scanned == 0
    assert stray.exists()


def test_the_task_name_is_registered() -> None:
    from app_shared.task_names import MAINTENANCE_EVIDENCE_RETENTION

    assert MAINTENANCE_EVIDENCE_RETENTION == "maintenance.evidence_retention"
