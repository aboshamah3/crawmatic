"""Read-only backup inventory for selective disk relief (F21).

Walks a backup root directory, classifies every file by "generation"
(`dr`, `saas`, `pre-variant`, `other`), and cross-references it against a
single `.tgz` archive to say whether each file is already archived (and
whether the archived copy's content matches on disk, via sha256).

This module NEVER deletes, moves, or modifies anything under `root` or
`archive`. It only reads. Deletion is an OWNER GATE: the CLI prints the
per-row `rm <path>` list for rows with `keep_reason is None`, it never
runs them.

Generation classification (first path segment relative to `root`):
    - "dr"                          -> "dr"
    - "saas"                        -> "saas"
    - starts with "pre_variant_"    -> "pre-variant"
    - anything else                 -> "other"

`keep_reason` (why a row must NOT be deleted; `None` means deletable):
    - "newest-not-archived"  -- the file has no matching entry in the
      archive at all, regardless of generation. This is the precondition
      for safe deletion (never delete something with no other copy).
    - "latest-two-dr-sets"   -- the file belongs to one of the two most
      recent "dr" backup sets, even if that set IS already archived.
      Production DR backups live either as flat files directly under
      `dr/` (e.g. `dr/prod-<ts>.dump`, one file == one set) or nested as
      `dr/sets/<set-name>/...` (multiple files per set, `<set-name>`
      groups them). Either layout is supported: the "set id" for a flat
      file is the file itself; for a nested file it is the set
      directory name. Sets are ranked by the newest mtime among their
      member files.
    - `None`                 -- in the archive, sha256-verified or not,
      and not one of the two newest dr sets: safe for the owner to
      delete per the gate in the plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

_PRE_VARIANT_PREFIX = "pre_variant_"
_LATEST_DR_SETS_KEPT = 2
_HASH_CHUNK = 1024 * 1024


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_archive_members(archive: Path, prefix: str) -> dict[str, str]:
    """Open `archive` once and hash every regular-file member under `prefix`.

    Returns a map of archive-relative path (with `prefix` stripped) -> sha256.
    """
    shas: dict[str, str] = {}
    with tarfile.open(archive, "r:*") as tar:
        for member in tar:
            if not member.isfile():
                continue
            name = member.name
            if not name.startswith(prefix):
                continue
            rel = name[len(prefix):]
            extracted = tar.extractfile(member)
            if extracted is None:
                continue
            digest = hashlib.sha256()
            for chunk in iter(lambda: extracted.read(_HASH_CHUNK), b""):
                digest.update(chunk)
            shas[rel] = digest.hexdigest()
    return shas


def _classify_generation(rel_parts: tuple[str, ...]) -> str:
    if not rel_parts:
        return "other"
    first = rel_parts[0]
    if first == "dr":
        return "dr"
    if first == "saas":
        return "saas"
    if first.startswith(_PRE_VARIANT_PREFIX):
        return "pre-variant"
    return "other"


def _dr_set_id(rel_parts: tuple[str, ...]) -> str:
    """Identify the "set" a dr-generation file belongs to.

    `dr/sets/<set-name>/...`  -> `<set-name>` (nested, multi-file sets).
    `dr/<file>`               -> `<file>`     (flat, one file == one set).
    """
    # rel_parts[0] == "dr" always, by construction.
    rest = rel_parts[1:]
    if len(rest) >= 2 and rest[0] == "sets":
        return rest[1]
    if rest:
        return rest[0]
    return "dr"


@dataclass
class _Row:
    path: Path
    rel_parts: tuple[str, ...]
    bytes: int
    mtime: float
    generation: str


def build_inventory(root: Path, archive: Path) -> list[dict]:
    """Build the read-only inventory. Never deletes or modifies anything.

    Returns a list of dicts, one per regular file under `root`:
    `{path, bytes, mtime, in_archive, sha256_matches_archive, generation,
    keep_reason}`.
    """
    root = Path(root)
    archive = Path(archive)
    prefix = f"{root.name}/"
    archive_shas = _sha256_archive_members(archive, prefix)

    rows: list[_Row] = []
    for file_path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = file_path.relative_to(root)
        rel_parts = rel.parts
        stat = file_path.stat()
        rows.append(
            _Row(
                path=file_path,
                rel_parts=rel_parts,
                bytes=stat.st_size,
                mtime=stat.st_mtime,
                generation=_classify_generation(rel_parts),
            )
        )

    # Rank dr "sets" by the newest mtime among their member files; keep
    # the newest _LATEST_DR_SETS_KEPT set ids regardless of archive status.
    set_latest_mtime: dict[str, float] = {}
    for row in rows:
        if row.generation != "dr":
            continue
        set_id = _dr_set_id(row.rel_parts)
        set_latest_mtime[set_id] = max(set_latest_mtime.get(set_id, 0.0), row.mtime)
    latest_dr_set_ids = {
        set_id
        for set_id, _ in sorted(
            set_latest_mtime.items(), key=lambda kv: kv[1], reverse=True
        )[:_LATEST_DR_SETS_KEPT]
    }

    out: list[dict] = []
    for row in rows:
        rel_posix = "/".join(row.rel_parts)
        sha = archive_shas.get(rel_posix)
        in_archive = sha is not None
        sha256_matches_archive: bool | None
        if not in_archive:
            sha256_matches_archive = None
        else:
            sha256_matches_archive = _sha256_file(row.path) == sha

        keep_reason: str | None
        if not in_archive:
            keep_reason = "newest-not-archived"
        elif row.generation == "dr" and _dr_set_id(row.rel_parts) in latest_dr_set_ids:
            keep_reason = "latest-two-dr-sets"
        else:
            keep_reason = None

        out.append(
            {
                "path": str(row.path),
                "bytes": row.bytes,
                "mtime": datetime.fromtimestamp(row.mtime, tz=timezone.utc).isoformat(),
                "in_archive": in_archive,
                "sha256_matches_archive": sha256_matches_archive,
                "generation": row.generation,
                "keep_reason": keep_reason,
            }
        )
    return out


def _du_style_totals(rows: Iterable[dict]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for row in rows:
        totals[row["generation"]] = totals.get(row["generation"], 0) + row["bytes"]
    totals["TOTAL"] = sum(v for k, v in totals.items() if k != "TOTAL")
    return totals


def _human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024.0 or unit == "TiB":
            return f"{size:,.1f}{unit}"
        size /= 1024.0
    return f"{size:,.1f}TiB"


def _print_table(rows: list[dict]) -> None:
    header = f"{'GEN':<12} {'KEEP_REASON':<22} {'ARCHIVED':<9} {'SHA_OK':<7} {'BYTES':>12}  PATH"
    print(header)
    print("-" * len(header))
    for row in rows:
        sha_ok = "-" if row["sha256_matches_archive"] is None else str(row["sha256_matches_archive"])
        print(
            f"{row['generation']:<12} "
            f"{(row['keep_reason'] or '-'):<22} "
            f"{str(row['in_archive']):<9} "
            f"{sha_ok:<7} "
            f"{row['bytes']:>12}  {row['path']}"
        )


def _print_docker_context(rows: list[dict]) -> None:
    totals = _du_style_totals(rows)
    print()
    print("-- du-style totals by generation --")
    for gen, total in sorted(totals.items()):
        print(f"{_human_bytes(total):>10}  {gen}")

    print()
    print("-- docker system df --")
    docker_bin = shutil.which("docker")
    if docker_bin is None:
        print("docker system df: SKIPPED (docker binary not found)")
        return
    try:
        result = subprocess.run(
            [docker_bin, "system", "df"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"docker system df: FAILED ({exc})")
        return
    if result.stdout:
        print(result.stdout.rstrip())
    if result.returncode != 0:
        print(f"docker system df: exit {result.returncode}")
        if result.stderr:
            print(result.stderr.rstrip())


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read-only backup inventory with archive coverage (F21). "
            "Never deletes anything; prints the deletable-row rm list for "
            "the owner to run by hand."
        )
    )
    parser.add_argument("--root", required=True, type=Path, help="Backup root directory to walk")
    parser.add_argument("--archive", required=True, type=Path, help="Path to the coverage .tgz archive")
    parser.add_argument("--json", type=Path, default=None, help="Write the row list as JSON to this path")
    parser.add_argument(
        "--docker",
        action="store_true",
        help="Also print du-style totals per generation and `docker system df` output",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    rows = build_inventory(root=args.root, archive=args.archive)

    _print_table(rows)

    total_bytes = sum(r["bytes"] for r in rows)
    deletable = [r for r in rows if r["keep_reason"] is None]
    deletable_bytes = sum(r["bytes"] for r in deletable)
    print()
    print(f"TOTAL rows={len(rows)} bytes={total_bytes} ({_human_bytes(total_bytes)})")
    print(
        f"DELETABLE rows={len(deletable)} bytes={deletable_bytes} "
        f"({_human_bytes(deletable_bytes)})"
    )

    if args.docker:
        _print_docker_context(rows)

    print()
    print("-- owner-gate rm list (NOT executed by this script) --")
    for row in deletable:
        print(f"rm {row['path']}")

    if args.json is not None:
        args.json.write_text(json.dumps(rows, indent=2, sort_keys=True))
        print(f"\nWrote {len(rows)} rows to {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
