"""The evidence store's key is a path component — so it is validated (W3 gate follow-up L2).

``resolve_hash``/``replay`` take a hash from whatever holds it: an
``OfferObservation`` row, a ``Rejection``, or the
``python -m app_shared.observations.evidence_store replay <hash>`` CLI,
whose argument is simply whatever a human or a script pastes in. That
string is then used verbatim as three path components, so a value like
``../../../../etc/passwd`` would read straight out of the store. These
tests pin the ``^[0-9a-f]{64}$`` refusal that closes it, and that the
legitimate round-trip is untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app_shared.observations.evidence_store import (
    EvidenceNotFoundError,
    compute_hash,
    replay,
    resolve_hash,
    store_evidence,
)

TRAVERSAL_SHAPED = [
    "../../../../etc/passwd",
    "..",
    "a/b",
    "/etc/passwd",
    # Right length and alphabet-adjacent, but not hex — still not an address.
    "z" * 64,
    # Real digest, wrong case: the store only ever writes lowercase, so an
    # uppercase twin would be a second name for the same bytes.
    compute_hash(b"x").upper(),
    "",
]


@pytest.mark.parametrize("bogus", TRAVERSAL_SHAPED)
def test_resolve_hash_refuses_a_non_hash_key(bogus: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        resolve_hash(bogus, store_dir=tmp_path)


def test_replay_refuses_a_traversal_shaped_hash_without_touching_the_filesystem(
    tmp_path: Path,
) -> None:
    """The refusal is a ``ValueError``, never a read of the escaped path.

    ``EvidenceNotFoundError`` would be the wrong answer here: it would
    mean the store looked, which is exactly what must not happen for a
    key that is not a content address.
    """
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"not evidence")
    store_dir = tmp_path / "store"
    store_dir.mkdir()

    with pytest.raises(ValueError) as excinfo:
        replay("../secret.txt", store_dir=store_dir)

    assert not isinstance(excinfo.value, EvidenceNotFoundError)
    assert "content address" in str(excinfo.value)


def test_a_real_hash_still_round_trips(tmp_path: Path) -> None:
    evidence_hash = store_evidence(b'{"price": 129}', store_dir=tmp_path)
    assert replay(evidence_hash, store_dir=tmp_path).data == b'{"price": 129}'


def test_a_well_formed_but_absent_hash_is_still_not_found(tmp_path: Path) -> None:
    """The guard narrows the input alphabet; it does not swallow the
    genuine "this hash has no evidence" outcome."""
    with pytest.raises(EvidenceNotFoundError):
        resolve_hash(compute_hash(b"never stored"), store_dir=tmp_path)
