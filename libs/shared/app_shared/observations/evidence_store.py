"""A minimal, real evidence store backing ``OfferObservation.raw_evidence_hash``.

Per the W3.1 plan: "raw evidence referenced by hash must be retained +
replayable — a hash pointing at deleted data proves nothing." This
module is that governed store, kept deliberately small (the full W5.5
retention-hardening pass — expiry sweeps, off-box archival, size
budgets — lands later): content-addressed storage on the local
filesystem, a hash function, and a replay path that turns a hash back
into the exact bytes it was computed from.

Retention rules are documented in ``EVIDENCE_RETENTION.md`` next to this
module — read that file for the policy this module enforces
mechanically (content-addressing + integrity verification on replay);
this module is the *mechanism*, not the policy statement.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: Overridable via ``OFFER_EVIDENCE_STORE_DIR`` (never read from ``.env``
#: — callers, including tests, pass ``base_dir`` explicitly; this is
#: only a fallback default for ad-hoc/CLI use).
DEFAULT_STORE_DIR_ENV_VAR = "OFFER_EVIDENCE_STORE_DIR"
_DEFAULT_STORE_DIR = Path("var") / "evidence"

#: The one hash algorithm this store speaks. Fixed (not configurable)
#: so a hash string is self-describing without a prefix — widening this
#: later is a new store version, not a per-call option.
_HASH_ALGORITHM = "sha256"


class EvidenceNotFoundError(LookupError):
    """Raised when a hash has no corresponding evidence in the store."""


class EvidenceIntegrityError(ValueError):
    """Raised when the bytes read back do not hash to the name they were
    stored under — i.e. the store itself was tampered with or corrupted."""


def default_store_dir() -> Path:
    """The store directory to use when a caller doesn't pass one
    explicitly: ``$OFFER_EVIDENCE_STORE_DIR`` if set, else ``./var/evidence``
    relative to the current working directory."""
    override = os.environ.get(DEFAULT_STORE_DIR_ENV_VAR)
    return Path(override) if override else _DEFAULT_STORE_DIR


def compute_hash(data: bytes) -> str:
    """The content address for ``data`` — hex ``sha256``, matching the
    string an :class:`~app_shared.observations.offer_observation.OfferObservation`
    would carry in ``raw_evidence_hash``."""
    return hashlib.new(_HASH_ALGORITHM, data).hexdigest()


#: The only shape a store key may have: a lowercase hex sha256 digest.
#: Enforced (not assumed) because the hash is a *path component* — a
#: caller-supplied `"../../etc/passwd"` would otherwise walk straight out
#: of the store, and one such caller is the ``replay`` CLI below, whose
#: argument is whatever a human or a script pastes in.
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


def _path_for_hash(store_dir: Path, evidence_hash: str) -> Path:
    if not isinstance(evidence_hash, str) or _HASH_RE.match(evidence_hash) is None:
        raise ValueError(
            f"{evidence_hash!r} is not a {_HASH_ALGORITHM} content address "
            "(expected 64 lowercase hex characters)"
        )
    # Two-level fan-out (first 2 / next 2 hex chars) keeps any one
    # directory from accumulating an unbounded number of entries, the
    # same layout convention as Git's own object store.
    return store_dir / evidence_hash[:2] / evidence_hash[2:4] / evidence_hash


def store_evidence(data: bytes, *, store_dir: Path | None = None) -> str:
    """Write ``data`` into the store, content-addressed. Returns the hash
    (also the retrieval key for :func:`resolve_hash`/:func:`replay`).

    Idempotent: writing the same bytes twice is a no-op the second time
    (same hash, same path, contents already identical) rather than an
    error — an observation's evidence may legitimately be stored more
    than once across retries.
    """
    resolved_dir = store_dir if store_dir is not None else default_store_dir()
    evidence_hash = compute_hash(data)
    path = _path_for_hash(resolved_dir, evidence_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # Write to a temp file then rename: a reader that lists the
        # directory mid-write never observes a partially-written object.
        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_bytes(data)
        tmp_path.replace(path)
    return evidence_hash


def resolve_hash(evidence_hash: str, *, store_dir: Path | None = None) -> bytes:
    """The replay primitive: turn a hash back into the exact bytes it was
    computed from. Raises :class:`EvidenceNotFoundError` if the hash has
    no entry, and :class:`EvidenceIntegrityError` if the stored bytes no
    longer hash to the name they are filed under (the store's own
    integrity check — a hash that resolves to the WRONG bytes is worse
    than one that resolves to nothing)."""
    resolved_dir = store_dir if store_dir is not None else default_store_dir()
    path = _path_for_hash(resolved_dir, evidence_hash)
    if not path.exists():
        raise EvidenceNotFoundError(
            f"no evidence stored for hash {evidence_hash!r} under {resolved_dir}"
        )
    data = path.read_bytes()
    actual_hash = compute_hash(data)
    if actual_hash != evidence_hash:
        raise EvidenceIntegrityError(
            f"evidence at {path} hashes to {actual_hash!r}, not the requested "
            f"{evidence_hash!r} — the store is corrupted for this entry"
        )
    return data


@dataclass(frozen=True)
class ReplayResult:
    """The outcome of replaying one hash: the bytes, and confirmation the
    integrity check passed (always true when this is returned — a failed
    check raises instead — kept as an explicit field so a caller can log
    a positive replay confirmation without re-deriving it)."""

    evidence_hash: str
    data: bytes
    verified: bool = True


def replay(evidence_hash: str, *, store_dir: Path | None = None) -> ReplayResult:
    """The public replay entry point named in the W3.1 plan: resolve
    ``evidence_hash`` back to bytes and confirm it is the exact evidence
    the hash names."""
    data = resolve_hash(evidence_hash, store_dir=store_dir)
    return ReplayResult(evidence_hash=evidence_hash, data=data, verified=True)


def _main(argv: list[str]) -> int:
    """CLI replay tool: ``python -m app_shared.observations.evidence_store
    replay <hash> [--store-dir PATH] [--out PATH]``. Prints the resolved
    byte count and, with ``--out``, writes the bytes there for manual
    inspection; without ``--out`` the raw bytes go to stdout's buffer."""
    import argparse

    parser = argparse.ArgumentParser(prog="evidence_store")
    subparsers = parser.add_subparsers(dest="command", required=True)

    replay_parser = subparsers.add_parser("replay", help="Resolve a hash back to bytes")
    replay_parser.add_argument("evidence_hash")
    replay_parser.add_argument("--store-dir", type=Path, default=None)
    replay_parser.add_argument("--out", type=Path, default=None)

    args = parser.parse_args(argv)

    if args.command == "replay":
        try:
            result = replay(args.evidence_hash, store_dir=args.store_dir)
        # `ValueError` covers both `EvidenceIntegrityError` (a subclass) and
        # the malformed-hash refusal from `_path_for_hash`, so a pasted
        # non-hash prints the refusal instead of a traceback.
        except (EvidenceNotFoundError, ValueError) as exc:
            print(f"replay failed: {exc}", file=sys.stderr)
            return 1
        if args.out is not None:
            args.out.write_bytes(result.data)
            print(f"replayed {len(result.data)} bytes -> {args.out}")
        else:
            sys.stdout.buffer.write(result.data)
        return 0

    return 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(_main(sys.argv[1:]))
