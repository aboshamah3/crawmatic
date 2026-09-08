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
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text

logger = logging.getLogger(__name__)

#: Overridable via ``OFFER_EVIDENCE_STORE_DIR`` (never read from ``.env``
#: — callers, including tests, pass ``base_dir`` explicitly; this is
#: only a fallback default for ad-hoc/CLI use).
DEFAULT_STORE_DIR_ENV_VAR = "OFFER_EVIDENCE_STORE_DIR"
_DEFAULT_STORE_DIR = Path("var") / "evidence"

#: EPA C5 (F19). How long an evidence blob is kept once nothing needs it
#: any more — the *age* half of the two-part gate below. Mirrored by
#: ``Settings.EVIDENCE_RETENTION_DAYS`` (the tunable a deployment
#: actually reads); this module-level constant is the documented default
#: and what a direct/CLI caller gets.
EVIDENCE_RETENTION_DAYS = 30

#: The observation retention window the *reference* half of the gate is
#: measured against — the same number ``Settings.
#: RETENTION_PRICE_OBSERVATIONS_DAYS`` carries, restated here so a
#: direct caller of :func:`sweep_evidence_retention` gets the correct
#: default without this stdlib-shaped module importing the settings
#: object. A caller that has settings should pass the real value.
DEFAULT_OBSERVATION_RETENTION_DAYS = 90

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


def store_dir_from_settings(settings: Any) -> Path | None:
    """``Settings.EVIDENCE_STORE_DIR`` as a ``Path``, or ``None``.

    ``None``/empty means "evidence storage is not configured on this
    deployment" and every caller must treat that as *do not write and do
    not record a hash* — never as "fall back to a local directory".
    See that setting's own comment for why an ephemeral fallback is
    worse than no store at all.
    """
    configured = getattr(settings, "EVIDENCE_STORE_DIR", None)
    if not configured:
        return None
    return Path(str(configured))


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


# ---------------------------------------------------------------------------
# EPA C5 (F19): the age-AND-reference gated retention sweep
# ---------------------------------------------------------------------------
#
# `docs/RETENTION_POLICY.md` §2.1 records this as an OPEN GAP -- "No
# deletion mechanism exists today ... evidence currently accumulates
# indefinitely" -- and prescribes the exact shape the mechanism must
# take when it is built:
#
#     "filesystem object deletion keyed by hash, gated on no
#      `price_observations.offer_raw_evidence_hash` row still
#      referencing it (a hash referenced by a NOT-yet-retention-eligible
#      observation must not be deleted out from under it)"
#
# That is why this is not a plain `find -mtime +30 -delete`. Evidence and
# the observations that cite it age on DIFFERENT clocks: an observation
# lives `RETENTION_PRICE_OBSERVATIONS_DAYS` (90) and a blob 30, so the
# naive sweep would routinely leave a 60-day stretch of rows whose
# content addresses resolve to nothing -- a table that LOOKS auditable
# and is not, which is worse than one that never claimed to be.
#
# Both conditions must hold before a blob is removed:
#   1. the blob itself is older than `retention_days`, and
#   2. no observation younger than `observation_retention_days` still
#      names its hash.
#
# The order matters for cost as much as for correctness: condition 1 is
# a stat() and prunes the candidate set to (in steady state) almost
# nothing, so condition 2 -- the only database work -- runs over a small,
# bounded list of hashes rather than the whole store.


@dataclass(frozen=True)
class EvidenceRetentionReport:
    """One sweep run's outcome, for the task's structured log line."""

    #: Well-formed blobs found in the store (a non-hash file is not one).
    blobs_scanned: int = 0
    #: Of those, how many were past ``retention_days``.
    blobs_expired: int = 0
    #: Of those, how many were removed (or would be, under ``dry_run``).
    blobs_deleted: int = 0
    #: Expired blobs an in-window observation still references.
    blobs_kept_referenced: int = 0
    bytes_reclaimed: int = 0
    #: ``True`` when ``max_blobs`` cut the run short — not an error; the
    #: next tick continues where this one stopped.
    truncated: bool = False
    dry_run: bool = False
    #: Blobs whose deletion raised (permissions, a racing reader). Counted
    #: rather than fatal: one unremovable file must not abandon the run.
    delete_errors: int = 0


def iter_stored_hashes(store_dir: Path) -> Any:
    """Yield ``(hash, path)`` for every well-formed blob in ``store_dir``.

    A file whose NAME is not a sha256 content address is not a blob and
    is never yielded — the store's two-level fan-out shares its directory
    tree with whatever an operator drops in it (a ``README``, a
    ``.tmp`` from an interrupted write), and a retention sweep that
    deletes files it cannot identify is a data-loss bug waiting for its
    first operator.
    """
    if not store_dir.is_dir():
        return
    for path in store_dir.rglob("*"):
        if not path.is_file():
            continue
        if _HASH_RE.match(path.name) is None:
            continue
        yield path.name, path


#: How many hashes one reference query asks about. Keeps the ``= ANY``
#: array (and the resulting plan) bounded regardless of ``max_blobs``.
_REFERENCE_QUERY_CHUNK = 500


def _referenced_hashes(
    session: Any, hashes: list[str], *, cutoff: datetime
) -> set[str]:
    """Which of ``hashes`` an observation newer than ``cutoff`` still names.

    Cross-tenant by construction: one blob is referenced by whichever
    workspaces happened to scrape that page, and a per-workspace answer
    could not decide a fleet-wide filesystem deletion. Runs on the
    BYPASSRLS system session the maintenance task opens, the same
    posture as every other entry in ``app_shared.maintenance``.

    The ``scraped_at >= cutoff`` predicate is doing two jobs: it IS the
    "younger than its own retention" half of the gate, and it prunes the
    partitioned scan to the handful of monthly partitions inside that
    window.
    """
    found: set[str] = set()
    for start in range(0, len(hashes), _REFERENCE_QUERY_CHUNK):
        chunk = hashes[start : start + _REFERENCE_QUERY_CHUNK]
        result = session.execute(  # noqa: workspace-scope - fleet-wide sweep
            text(
                "SELECT DISTINCT offer_raw_evidence_hash FROM price_observations "
                "WHERE scraped_at >= :cutoff "
                "AND offer_raw_evidence_hash = ANY(:hashes)"
            ),
            {"cutoff": cutoff, "hashes": chunk},
        )
        found.update(row for row in result.scalars().all() if row)
    return found


def sweep_evidence_retention(
    session: Any,
    *,
    store_dir: Path,
    now: datetime | None = None,
    retention_days: int = EVIDENCE_RETENTION_DAYS,
    observation_retention_days: int = DEFAULT_OBSERVATION_RETENTION_DAYS,
    max_blobs: int = 5000,
    dry_run: bool = False,
) -> EvidenceRetentionReport:
    """Delete expired, unreferenced evidence blobs. Returns what it did.

    ``session`` is only ever asked one question (:func:`_referenced_hashes`)
    and is never written to — the sweep's only mutation is on the
    filesystem, so there is nothing to commit and a caller may pass a
    read-only session.

    **A sweep that cannot prove a blob is unreferenced deletes nothing.**
    A database error propagates rather than being swallowed into a
    "nothing was referenced, delete everything" answer; that is the one
    failure mode of this function that would be irreversible.
    """
    reference_moment = now if now is not None else datetime.now(UTC)
    blob_cutoff = reference_moment - timedelta(days=retention_days)
    observation_cutoff = reference_moment - timedelta(days=observation_retention_days)

    scanned = 0
    expired: list[tuple[str, Path, int]] = []
    truncated = False
    for evidence_hash, path in iter_stored_hashes(store_dir):
        scanned += 1
        try:
            stat = path.stat()
        except OSError:  # pragma: no cover - raced with another sweep
            continue
        if datetime.fromtimestamp(stat.st_mtime, UTC) >= blob_cutoff:
            continue
        if len(expired) >= max_blobs:
            truncated = True
            continue
        expired.append((evidence_hash, path, stat.st_size))

    if not expired:
        return EvidenceRetentionReport(
            blobs_scanned=scanned, truncated=truncated, dry_run=dry_run
        )

    referenced = _referenced_hashes(
        session, [entry[0] for entry in expired], cutoff=observation_cutoff
    )

    deleted = 0
    kept = 0
    reclaimed = 0
    delete_errors = 0
    for evidence_hash, path, size in expired:
        if evidence_hash in referenced:
            kept += 1
            continue
        if dry_run:
            deleted += 1
            reclaimed += size
            continue
        try:
            path.unlink()
        except OSError:
            delete_errors += 1
            logger.warning("evidence_retention could not delete %s", path, exc_info=True)
            continue
        deleted += 1
        reclaimed += size

    return EvidenceRetentionReport(
        blobs_scanned=scanned,
        blobs_expired=len(expired),
        blobs_deleted=deleted,
        blobs_kept_referenced=kept,
        bytes_reclaimed=reclaimed,
        truncated=truncated,
        dry_run=dry_run,
        delete_errors=delete_errors,
    )


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
