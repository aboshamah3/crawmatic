#!/usr/bin/env python3
"""Build the immutable, self-hashed release manifest (READY-001, Task A5).

Audit ref: `PRODUCTION_READINESS_IMPLEMENTATION_PLAN_2026-08-25.md` Task A5:
"Build manifest (immutable, signed, produced at build time): image digests,
source digests, API schema version, migration set, SaaS commit + protocol
range, plugin ZIP hash + version matrix, Salla contract version, config
schema (no values), test/scan evidence links."

Run at BUILD time, from the repo root::

    uv run python scripts/build_release_manifest.py \\
        --out build/release_manifest.json \\
        --emit-identity build/release_identity.json \\
        --image-digest api=sha256:... --image-digest worker=sha256:... \\
        --evidence tests=https://... --evidence scan=https://...

`--emit-identity` writes the five-field subset that gets BAKED INTO THE
IMAGE and read back at runtime by `app_shared.release.get_release_identity()`.
That file is the reason a Railway environment variable is not the source of
release truth: it travels inside the artifact.

What "signed" means here
------------------------

Two layers, deliberately separable:

1. **Self-hash (always).** `manifest_id` is the SHA-256 of the manifest's own
   canonical JSON with the `manifest_id` and `signing.gpg` keys excluded. It
   is computed deterministically (sorted keys, no insignificant whitespace),
   so the same inputs always produce the same id and any later edit to the
   file is detectable by recomputation alone — which is exactly what
   `build_deployment_attestation.py` verifies before it will reference the
   manifest.
2. **GPG detached signature (when a key is configured).** Pass `--gpg-key
   <keyid>` to produce `<out>.asc`. Without it the manifest records
   `signing.gpg.status = "unsigned"` together with the exact operator command
   to sign it later. **No throwaway key is ever generated**: a signature from
   a key nobody controls or has published is worse than an honest "unsigned",
   because it looks like provenance while proving nothing.

Determinism
-----------

`--generated-at` pins the timestamp so two builds of the same tree can be
compared byte-for-byte. Left unset it records the current UTC time, which
makes each run's `manifest_id` unique — correct for a real build, unhelpful
for verifying reproducibility, hence the flag.

Secret discipline
-----------------

The config schema section carries setting NAMES, TYPES and required-ness
only. `app_shared.release.config_schema()` reads pydantic field
*declarations* and never constructs a `Settings`, so no environment value is
in scope. The manifest is intended to be readable by anyone who can read the
build log; nothing in it may be a credential.
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

from app_shared.release import (  # noqa: E402
    canonical_json,
    code_migration_head,
    compute_config_schema_version,
    config_schema,
    migration_revisions,
    sha256_digest,
)

MANIFEST_SCHEMA_VERSION = 1
SIGNING_MECHANISM = "sha256-self-hash+optional-gpg-detached"

#: Keys excluded from the self-hash. `manifest_id` cannot hash itself, and
#: `signing.gpg` records facts established *after* the id exists.
_SELF_HASH_EXCLUDED_TOP_LEVEL = ("manifest_id",)


# --------------------------------------------------------------------------
# Source digest
# --------------------------------------------------------------------------


def _git(*args: str) -> str | None:
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _source_record() -> dict[str, Any]:
    """Identify the source tree this artifact was built from.

    `digest` prefers git's own content-addressed tree hash (`git rev-parse
    HEAD^{tree}`), which is already a deterministic hash over the exact
    tracked content and costs nothing to compute. When git is unavailable
    (a source tarball, a `.git`-less build context — `.dockerignore` strips
    `.git` from the image build) it falls back to hashing the sorted list of
    tracked-file digests, and records which method was used so a reader never
    has to guess why two digests of "the same" tree differ.

    `dirty` is reported honestly rather than being allowed to fail the build:
    a manifest that says "built from a dirty tree" is useful evidence; one
    that refuses to exist is not.
    """
    commit = _git("rev-parse", "HEAD")
    tree = _git("rev-parse", "HEAD^{tree}")
    status = _git("status", "--porcelain")

    if tree:
        return {
            "method": "git-tree",
            "commit": commit,
            "digest": sha256_digest(tree),
            "git_tree": tree,
            "dirty": bool(status),
        }

    files = sorted(
        path
        for path in _REPO_ROOT.rglob("*")
        if path.is_file()
        and not any(
            part in {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
            for part in path.parts
        )
    )
    lines = [
        f"{path.relative_to(_REPO_ROOT).as_posix()}:{sha256_digest(path.read_bytes())}"
        for path in files
    ]
    return {
        "method": "file-walk",
        "commit": commit,
        "digest": sha256_digest("\n".join(lines)),
        "git_tree": None,
        "dirty": bool(status) if status is not None else None,
    }


# --------------------------------------------------------------------------
# API schema version
# --------------------------------------------------------------------------


def _api_schema_record() -> dict[str, Any]:
    """Digest the checked-in public OpenAPI document.

    `docs/openapi-public.json` is generated by `scripts/export_openapi.py` and
    committed, so hashing the file (rather than importing `app.main` and
    regenerating) keeps this script free of any FastAPI import and free of
    the settings-validation those imports trigger — this runs at build time,
    where a runtime `DATABASE_URL` does not and should not exist.
    """
    path = _REPO_ROOT / "docs" / "openapi-public.json"
    if not path.exists():
        return {"source": "docs/openapi-public.json", "status": "absent", "version": None}
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
        declared = document.get("info", {}).get("version")
    except (ValueError, AttributeError):
        declared = None
    return {
        "source": "docs/openapi-public.json",
        "status": "present",
        "declared_version": declared,
        "version": sha256_digest(raw),
        # Stated plainly because it is load-bearing: this digest covers the
        # COMMITTED document, which is what the SaaS `/docs` page serves — not
        # a document regenerated from the routers in this build. The two can
        # drift (as of 2026-08-25 the committed file predates `/version` and
        # `/ready` entirely). Re-run `scripts/export_openapi.py` and commit
        # the result to make this digest describe the live route surface.
        "note": (
            "digest of the committed spec artifact, not regenerated from the "
            "running routers; run scripts/export_openapi.py if they have drifted"
        ),
    }


# --------------------------------------------------------------------------
# External components (SaaS / WooCommerce plugin / Salla)
# --------------------------------------------------------------------------


def _external_components(args: argparse.Namespace) -> dict[str, Any]:
    """Record cross-repo component versions — or their absence, honestly.

    The SaaS control plane, the WooCommerce plugin ZIP and the Salla contract
    are **not in this repository**. This script therefore cannot discover
    them; it can only record what the build was told. When a value is not
    supplied the component is marked `"not-supplied"` rather than defaulted,
    guessed, or omitted: a release manifest whose job is to answer "what
    exactly shipped together" must never quietly invent one of the answers,
    and an absent-but-declared slot is what makes the gap visible in review.
    """
    saas: dict[str, Any] = {"status": "not-supplied", "commit": None, "protocol_range": None}
    if args.saas_commit or args.saas_protocol_range:
        saas = {
            "status": "supplied",
            "commit": args.saas_commit,
            "protocol_range": args.saas_protocol_range,
        }

    version_matrix: dict[str, str] = {}
    for entry in args.plugin_version_matrix or []:
        plugin_version, sep, requirements = entry.partition(":")
        if not sep:
            raise SystemExit(
                f"--plugin-version-matrix expects 'version:requirements', got {entry!r}"
            )
        version_matrix[plugin_version.strip()] = requirements.strip()

    woo: dict[str, Any] = {
        "status": "not-supplied",
        "zip_sha256": None,
        "version_matrix": {},
    }
    if args.plugin_zip_sha256 or version_matrix:
        woo = {
            "status": "supplied",
            "zip_sha256": args.plugin_zip_sha256,
            "version_matrix": version_matrix,
        }

    salla: dict[str, Any] = {"status": "not-supplied", "contract_version": None}
    if args.salla_contract_version:
        salla = {"status": "supplied", "contract_version": args.salla_contract_version}

    return {"saas": saas, "woo_plugin": woo, "salla": salla}


# --------------------------------------------------------------------------
# key=value argument parsing
# --------------------------------------------------------------------------


def _parse_pairs(entries: list[str] | None, flag: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for entry in entries or []:
        key, sep, value = entry.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"{flag} expects 'key=value', got {entry!r}")
        parsed[key.strip()] = value.strip()
    return parsed


# --------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------


def _gpg_record(manifest_path: Path, key_id: str | None) -> dict[str, Any]:
    """Sign with GPG when a key is named; otherwise document the step.

    Never generates a key. An unsigned manifest states the exact command an
    operator runs later, so "sign this" is a checklist item with a command
    attached rather than folklore.
    """
    operator_command = (
        f"gpg --detach-sign --armor --local-user <KEYID> {manifest_path.name}"
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
            "reason": "gpg binary not available on the build host",
            "operator_command": operator_command,
            "signature_file": None,
        }

    signature_path = manifest_path.with_suffix(manifest_path.suffix + ".asc")
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
            str(manifest_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # Reported, not raised: an unsigned manifest that says why is more
        # useful than a failed build that produced no record at all. The
        # stderr text is NOT copied in — it can name key ids and paths.
        return {
            "status": "unsigned",
            "reason": "gpg detached-sign failed on the build host",
            "operator_command": operator_command,
            "signature_file": None,
        }
    return {
        "status": "signed",
        "key_id": key_id,
        "operator_command": operator_command,
        "signature_file": signature_path.name,
    }


# --------------------------------------------------------------------------
# Manifest assembly
# --------------------------------------------------------------------------


def compute_manifest_id(manifest: dict[str, Any]) -> str:
    """SHA-256 over the manifest's canonical JSON, excluding the id itself and
    the post-hoc `signing.gpg` block."""
    payload = {
        key: value for key, value in manifest.items() if key not in _SELF_HASH_EXCLUDED_TOP_LEVEL
    }
    signing = dict(payload.get("signing") or {})
    signing.pop("gpg", None)
    payload["signing"] = signing
    return sha256_digest(canonical_json(payload))


def build_manifest(args: argparse.Namespace) -> dict[str, Any]:
    generated_at = args.generated_at or datetime.now(UTC).isoformat(timespec="seconds")
    images = _parse_pairs(args.image_digest, "--image-digest")
    evidence = _parse_pairs(args.evidence, "--evidence")

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "record_type": "build_manifest",
        "generated_at": generated_at,
        "source": _source_record(),
        "images": images,
        "api_schema": _api_schema_record(),
        "migrations": {
            "head": code_migration_head(),
            "revisions": migration_revisions(),
        },
        "config_schema": {
            "version": compute_config_schema_version(),
            "fields": config_schema(),
            "note": "names and types only — never values",
        },
        "external_components": _external_components(args),
        "evidence": evidence,
        "signing": {"mechanism": SIGNING_MECHANISM},
    }
    manifest["manifest_id"] = compute_manifest_id(manifest)
    return manifest


def identity_subset(manifest: dict[str, Any], *, image_key: str) -> dict[str, Any]:
    """The five fields baked into the image and read back by
    `app_shared.release.get_release_identity()`."""
    return {
        "manifest_id": manifest["manifest_id"],
        "source_digest": manifest["source"]["digest"],
        "image_digest": manifest["images"].get(image_key),
        "config_schema_version": manifest["config_schema"]["version"],
        "expected_db_migration": manifest["migrations"]["head"],
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True, type=Path, help="manifest output path")
    parser.add_argument(
        "--emit-identity",
        type=Path,
        default=None,
        help="also write the bakeable release_identity.json subset here",
    )
    parser.add_argument(
        "--identity-image",
        default="api",
        help="which --image-digest entry the baked identity's image_digest refers to",
    )
    parser.add_argument(
        "--image-digest",
        action="append",
        metavar="SERVICE=sha256:...",
        help="repeatable; one per built service image",
    )
    parser.add_argument(
        "--evidence",
        action="append",
        metavar="NAME=URL",
        help="repeatable; links to test/scan evidence for this build",
    )
    parser.add_argument("--saas-commit", default=None)
    parser.add_argument("--saas-protocol-range", default=None)
    parser.add_argument("--plugin-zip-sha256", default=None)
    parser.add_argument(
        "--plugin-version-matrix",
        action="append",
        metavar="PLUGIN_VERSION:REQUIREMENTS",
        help="repeatable, e.g. 0.9.3:wp>=6.4,woo>=8.5",
    )
    parser.add_argument("--salla-contract-version", default=None)
    parser.add_argument(
        "--generated-at",
        default=None,
        help="pin the timestamp (ISO-8601) so a rebuild is byte-comparable",
    )
    parser.add_argument(
        "--gpg-key",
        default=None,
        help="key id for a detached signature; omitted => recorded as unsigned",
    )
    args = parser.parse_args(argv)

    manifest = build_manifest(args)
    _write_json(args.out, manifest)

    # The GPG block is added AFTER the file exists (it signs the file) and is
    # excluded from the self-hash, so appending it cannot change manifest_id.
    manifest["signing"]["gpg"] = _gpg_record(args.out, args.gpg_key)
    _write_json(args.out, manifest)

    if args.emit_identity:
        _write_json(args.emit_identity, identity_subset(manifest, image_key=args.identity_image))

    print(f"manifest_id={manifest['manifest_id']}")
    print(f"wrote {args.out}")
    if args.emit_identity:
        print(f"wrote {args.emit_identity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
