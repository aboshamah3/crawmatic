#!/usr/bin/env python3
"""Build the deployment attestation, linked to a build manifest (READY-001, A5).

Audit ref: `PRODUCTION_READINESS_IMPLEMENTATION_PLAN_2026-08-25.md` Task A5:
"Deployment attestation (produced at deploy time, signed, linked to the build
manifest by digest): environment, backup ID taken before migration, approver,
deployment timestamp, live database migration head, smoke/post-deploy
verification results, rollback artifact references. Deploy-time facts never
require mutating the signed build manifest."

Run at DEPLOY time, after the smoke checks::

    uv run python scripts/build_deployment_attestation.py \\
        --build-manifest build/release_manifest.json \\
        --out /srv/crawmatic/evidence/deploy-2026-08-25/deployment_attestation.json \\
        --environment production \\
        --backup-id <A4 backup id taken BEFORE the migration> \\
        --approver <name> \\
        --live-migration-head "$(curl -s https://<engine-host>/version | jq -r .live_db_migration)" \\
        --smoke version_endpoint=pass --smoke ready_endpoint=pass \\
        --rollback-artifact api=sha256:<previous image digest>

Why this is a second record rather than more fields on the first
----------------------------------------------------------------

The build manifest is self-hashed and (optionally) GPG-signed. Appending the
approver or the smoke results to it would change its `manifest_id` and
invalidate its signature — so a design that keeps one file forces you either
to re-sign after every deploy (at which point the signature attests to
nothing stable) or to leave the file unsigned (at which point nothing attests
to the build at all). Two records, linked by digest, keep the build record
frozen while the deploy record accumulates.

The link is verified, not assumed
---------------------------------

This script recomputes the manifest's self-hash and refuses to proceed if it
does not match the `manifest_id` recorded inside it. An attestation that
points at an edited manifest would assert provenance it cannot support, which
is worse than no attestation: it launders a tampered record through a
document that looks authoritative.

`--backup-id` is mandatory
--------------------------

Task A4 establishes "backup before every schema/data migration". An
attestation without a backup id is not evidence of a safe deploy, so the
argument is required rather than optional-with-a-null — the failure has to
happen at the moment someone tries to skip it, not later during an audit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "libs" / "shared"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from app_shared.release import canonical_json, sha256_digest  # noqa: E402

from build_release_manifest import SIGNING_MECHANISM, compute_manifest_id  # noqa: E402

ATTESTATION_SCHEMA_VERSION = 1


def _parse_pairs(entries: list[str] | None, flag: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in entries or []:
        key, sep, value = entry.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"{flag} expects 'key=value', got {entry!r}")
        parsed[key.strip()] = value.strip()
    return parsed


def load_and_verify_manifest(path: Path) -> tuple[dict[str, Any], str]:
    """Load the build manifest and prove it has not been edited since build.

    Returns ``(manifest, sha256_of_the_file_bytes)``. The file-bytes digest is
    recorded alongside `manifest_id` in the attestation: the id proves the
    *content* is intact, the file digest pins the exact artifact an operator
    can fetch and compare, including its formatting.
    """
    if not path.exists():
        raise SystemExit(f"build manifest not found: {path}")
    raw = path.read_bytes()
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise SystemExit(f"build manifest is not valid JSON: {path}") from exc
    if not isinstance(manifest, dict):
        raise SystemExit(f"build manifest is not a JSON object: {path}")

    recorded = manifest.get("manifest_id")
    recomputed = compute_manifest_id(manifest)
    if recorded != recomputed:
        raise SystemExit(
            "build manifest self-hash does not match its recorded manifest_id — "
            "the file has been modified since it was built; rebuild it rather "
            f"than attesting to it ({path})"
        )

    # H6: a manifest built from a dirty tree (`source.dirty == true`) records
    # that fact honestly (see `build_release_manifest.py`'s `_source_record`
    # docstring — a dishonest-looking "clean" manifest is worse than a build
    # that admits it wasn't) but must never be attested to: an attestation
    # says "this exact, committed tree was deployed", and a dirty tree has no
    # single commit that claim can point at. `scripts/write_release_manifest.py`
    # already refuses to *write* a dirty manifest in the first place — this is
    # the second, independent gate for any manifest that reaches this script
    # by another path (a hand-run `build_release_manifest.py`, an older
    # manifest file, ...).
    if manifest.get("source", {}).get("dirty") is True:
        raise SystemExit(
            "build manifest was built from a DIRTY source tree "
            "(source.dirty == true) — refusing to attest to it. Rebuild from a "
            f"clean, committed tree ({path})."
        )
    return manifest, sha256_digest(raw)


def _gpg_record(attestation_path: Path, key_id: str | None) -> dict[str, Any]:
    """Same posture as the build manifest: sign when a key is named, otherwise
    record the mechanism and the exact operator command. Never generates a
    key — see `build_release_manifest.py`'s docstring."""
    operator_command = (
        f"gpg --detach-sign --armor --local-user <KEYID> {attestation_path.name}"
    )
    if not key_id:
        return {
            "status": "unsigned",
            "reason": "no signing key configured (--gpg-key not supplied)",
            "operator_command": operator_command,
            "signature_file": None,
        }
    if shutil.which("gpg") is None:
        return {
            "status": "unsigned",
            "reason": "gpg binary not available on the deploy host",
            "operator_command": operator_command,
            "signature_file": None,
        }
    signature_path = attestation_path.with_suffix(attestation_path.suffix + ".asc")
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            "gpg",
            "--batch",
            "--yes",
            "--detach-sign",
            "--armor",
            "--local-user",
            key_id,
            "--output",
            str(signature_path),
            str(attestation_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return {
            "status": "unsigned",
            "reason": "gpg detached-sign failed on the deploy host",
            "operator_command": operator_command,
            "signature_file": None,
        }
    return {
        "status": "signed",
        "key_id": key_id,
        "operator_command": operator_command,
        "signature_file": signature_path.name,
    }


def compute_attestation_id(attestation: dict[str, Any]) -> str:
    payload = {
        key: value for key, value in attestation.items() if key != "attestation_id"
    }
    signing = dict(payload.get("signing") or {})
    signing.pop("gpg", None)
    payload["signing"] = signing
    return sha256_digest(canonical_json(payload))


def build_attestation(args: argparse.Namespace) -> dict[str, Any]:
    manifest, manifest_file_digest = load_and_verify_manifest(args.build_manifest)

    smoke = _parse_pairs(args.smoke, "--smoke")
    rollback_artifacts = _parse_pairs(args.rollback_artifact, "--rollback-artifact")
    failed_smoke = sorted(
        name for name, outcome in smoke.items() if outcome.lower() not in {"pass", "ok"}
    )

    attestation: dict[str, Any] = {
        "schema_version": ATTESTATION_SCHEMA_VERSION,
        "record_type": "deployment_attestation",
        "build_manifest": {
            "manifest_id": manifest["manifest_id"],
            "sha256": manifest_file_digest,
            "path": str(args.build_manifest),
            "source_digest": manifest.get("source", {}).get("digest"),
            "images": manifest.get("images", {}),
            "expected_db_migration": manifest.get("migrations", {}).get("head"),
        },
        "environment": args.environment,
        "backup_id": args.backup_id,
        "approver": args.approver,
        "deployed_at": args.deployed_at
        or datetime.now(UTC).isoformat(timespec="seconds"),
        "live_db_migration": args.live_migration_head,
        # The comparison is recorded rather than enforced: this script writes
        # evidence of what happened, and a deploy that DID land on a
        # mismatched head is precisely the fact an attestation must be able
        # to state. `/ready` is what refuses traffic in that situation.
        "migration_heads_match": (
            None
            if not (args.live_migration_head and manifest.get("migrations", {}).get("head"))
            else args.live_migration_head == manifest["migrations"]["head"]
        ),
        "smoke": smoke,
        "smoke_passed": not failed_smoke,
        "smoke_failures": failed_smoke,
        "rollback": {
            "artifacts": rollback_artifacts,
            "restore_target": args.restore_target,
            "runbook": "docs/DEPLOY-ROLLBACK.md",
        },
        "notes": args.note or [],
        "signing": {"mechanism": SIGNING_MECHANISM},
    }
    attestation["attestation_id"] = compute_attestation_id(attestation)
    return attestation


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--build-manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--environment", required=True, help="e.g. staging, production")
    parser.add_argument(
        "--backup-id",
        required=True,
        help="the A4 backup taken BEFORE the migration — mandatory evidence",
    )
    parser.add_argument("--approver", required=True)
    parser.add_argument(
        "--live-migration-head",
        default=None,
        help="alembic_version read from the LIVE database after deploy",
    )
    parser.add_argument(
        "--smoke",
        action="append",
        metavar="NAME=pass|fail",
        help="repeatable post-deploy verification result",
    )
    parser.add_argument(
        "--rollback-artifact",
        action="append",
        metavar="SERVICE=sha256:...",
        help="repeatable; the PREVIOUS image digest to roll each service back to",
    )
    parser.add_argument(
        "--restore-target",
        default=None,
        help="the restore point a schema rollback would use (never a down-migration)",
    )
    parser.add_argument("--note", action="append", help="repeatable free-text note")
    parser.add_argument(
        "--deployed-at", default=None, help="ISO-8601; defaults to now (UTC)"
    )
    parser.add_argument("--gpg-key", default=None)
    args = parser.parse_args(argv)

    attestation = build_attestation(args)
    _write_json(args.out, attestation)
    attestation["signing"]["gpg"] = _gpg_record(args.out, args.gpg_key)
    _write_json(args.out, attestation)

    print(f"attestation_id={attestation['attestation_id']}")
    print(f"build_manifest_id={attestation['build_manifest']['manifest_id']}")
    print(f"wrote {args.out}")
    if attestation["smoke_failures"]:
        print(
            "WARNING: recorded smoke failures: "
            + ", ".join(attestation["smoke_failures"]),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
