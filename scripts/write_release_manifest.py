#!/usr/bin/env python3
"""Deploy-time wrapper around `build_release_manifest.py` (H6, production
readiness audit).

`build_release_manifest.py` is deliberately permissive: it records
`source.dirty` honestly (§ its own docstring — "a manifest that says
'built from a dirty tree' is useful evidence; one that refuses to exist
is not") and is happy to run against any checkout, including one with
uncommitted changes or with only one half of the platform's two repos in
view.

This script is the OPPOSITE posture, on purpose, because it is the one
named in the deploy runbook (`docs/DEPLOY-ROLLBACK.md`, and
`/srv/crawmatic/evidence/A5_DEPLOY_RUNBOOK.md` §1.1) as the step that
actually produces the manifest a real deploy attests to:

* it REFUSES (exit 2) when the engine tree is dirty, instead of just
  recording the fact — a deploy must never attest to a tree that isn't
  exactly what's checked in;
* it resolves and embeds BOTH repositories' HEAD SHAs
  (`source.engine_sha`, `source.saas_sha`) into the manifest, because a
  release of this platform is a pair of commits (this repo + the SaaS
  repo), not one — see the module docstring in
  `libs/shared/app_shared/release.py` for why release truth must never
  be "whatever someone last typed."

`compute_manifest_id` (imported, not reimplemented) hashes whatever keys
a manifest dict happens to have, so adding `engine_sha`/`saas_sha` to the
`source` block here does not change how OLD manifests (built before this
script existed) verify — `build_deployment_attestation.py`'s self-hash
check recomputes over each manifest's own recorded keys, never a
schema-versioned set of expected ones.

Run from the engine repo root, exactly like `build_release_manifest.py`::

    uv run python scripts/write_release_manifest.py \\
        --out build/release_manifest.json \\
        --emit-identity ./release_identity.json \\
        --saas-repo /srv/crawmatic/saas \\
        --image-digest api=sha256:... \\
        --evidence tests=https://...

`--saas-sha <sha>` overrides repo discovery (e.g. CI where the SaaS repo
isn't checked out locally but its SHA is already known from another job).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "libs" / "shared"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import build_release_manifest as brm  # noqa: E402


def _run_git(repo: Path, *args: str) -> str | None:
    if shutil.which("git") is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def resolve_engine_sha(repo: Path) -> str | None:
    """The engine repo's own HEAD SHA — this script always runs from that repo."""
    return _run_git(repo, "rev-parse", "HEAD")


def engine_tree_is_dirty(repo: Path) -> bool | None:
    """`True` when `git status --porcelain` reports anything at all.

    `None` — deliberately distinct from `False` — when cleanliness could not
    be established at all (no `git` binary, not a git checkout, the command
    failed): unlike `build_release_manifest.py`'s own `_source_record()`
    (which is honest that "unknown" isn't "clean" but still produces a best-
    effort manifest, because *some* record beats none), this wrapper's whole
    job is to gate a deploy, so "can't tell" must fail closed the same way
    "definitely dirty" does — see the `is not False` check in `main()`. A
    silent `False` here would let an unverifiable tree sail through as
    "clean" by omission.
    """
    status = _run_git(repo, "status", "--porcelain")
    return bool(status) if status is not None else None


def resolve_saas_sha(*, saas_repo: Path | None, saas_sha: str | None) -> str | None:
    """`--saas-sha` wins outright; otherwise read HEAD from `--saas-repo`.

    Read-only: this script never inspects the SaaS repo's working-tree
    cleanliness or writes to it. The engine tree's cleanliness is the one
    this script enforces (`engine_tree_is_dirty`) — the SaaS repo is a
    separate release with its own deploy process and its own guard.
    """
    if saas_sha:
        return saas_sha
    if saas_repo is None:
        return None
    return _run_git(saas_repo, "rev-parse", "HEAD")


def build_args_namespace(cli: argparse.Namespace) -> argparse.Namespace:
    """Translate this script's CLI into the `argparse.Namespace` shape
    `build_release_manifest.build_manifest()` expects, so that function's
    logic (source digest, config schema, migrations, signing, ...) is
    reused rather than duplicated."""
    return argparse.Namespace(
        generated_at=cli.generated_at,
        image_digest=cli.image_digest,
        evidence=cli.evidence,
        # Not a direct CLI flag on this wrapper (unlike build_release_manifest.py):
        # `main()` fills this in from the resolved SaaS SHA unless the caller
        # doesn't want that default — see the `if build_args.saas_commit is
        # None` line right after this function is called.
        saas_commit=None,
        saas_protocol_range=cli.saas_protocol_range,
        plugin_zip_sha256=cli.plugin_zip_sha256,
        plugin_version_matrix=cli.plugin_version_matrix,
        salla_contract_version=cli.salla_contract_version,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
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
    parser.add_argument(
        "--saas-repo",
        type=Path,
        default=None,
        help="path to a SaaS repo checkout to read HEAD from (e.g. /srv/crawmatic/saas)",
    )
    parser.add_argument(
        "--saas-sha",
        default=None,
        help="override: the SaaS HEAD SHA to embed, instead of reading --saas-repo",
    )
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
    parser.add_argument(
        "--engine-repo",
        type=Path,
        default=_REPO_ROOT,
        help="the engine repo this script runs against (default: this checkout)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "escape hatch for local experimentation ONLY — never use in a real "
            "deploy. Skips the dirty-tree refusal below."
        ),
    )
    cli = parser.parse_args(argv)

    engine_repo: Path = cli.engine_repo
    is_dirty = engine_tree_is_dirty(engine_repo)

    # `is not False` refuses on both `True` (definitely dirty) and `None`
    # (cleanliness couldn't be established at all — no git binary, not a
    # git checkout, the command failed): a deploy that can't prove the tree
    # is clean gets the same refusal as one that proves it's dirty.
    if not cli.allow_dirty and is_dirty is not False:
        reason = (
            "`git status --porcelain` is non-empty"
            if is_dirty is True
            else "the engine tree's cleanliness could not be verified at all "
            "(no `git` binary, not a git checkout, or the command failed)"
        )
        print(
            f"refusing to write a release manifest from a dirty engine tree "
            f"({engine_repo}): {reason}. Commit or stash outstanding changes "
            "first. A deploy must attest to exactly what is checked in, "
            "never to an uncommitted or unverifiable working tree — see "
            "scripts/build_deployment_attestation.py's dirty-manifest "
            "refusal, which this check exists to make unreachable in the "
            "first place.",
            file=sys.stderr,
        )
        return 2

    engine_sha = resolve_engine_sha(engine_repo)
    saas_sha = resolve_saas_sha(saas_repo=cli.saas_repo, saas_sha=cli.saas_sha)

    build_args = build_args_namespace(cli)
    # `external_components.saas.commit` (a separate, optional field —
    # `build_release_manifest.py`'s own `_external_components`) defaults to
    # the resolved SaaS SHA when the caller didn't say otherwise, so the two
    # SaaS references in the manifest agree unless deliberately overridden.
    if build_args.saas_commit is None:
        build_args.saas_commit = saas_sha

    manifest = brm.build_manifest(build_args)
    manifest["source"]["engine_sha"] = engine_sha
    manifest["source"]["saas_sha"] = saas_sha
    # `engine_sha` should already equal `source.commit` (both HEAD of this
    # checkout) — recorded as a separate, explicitly-named field anyway so a
    # reader of `source` never has to know that `commit` means "the engine's"
    # when a second repo's SHA now lives right next to it.
    #
    # `dirty` is overwritten with THIS script's own `--engine-repo`-aware
    # check rather than trusted from `build_manifest()`'s `_source_record()`,
    # which always inspects `build_release_manifest.py`'s own fixed location
    # on disk regardless of `--engine-repo`. In normal use (running this
    # script from inside the checkout it validates, the default) the two
    # agree; overwriting keeps that true even when `--engine-repo` points
    # somewhere else (as tests do), and keeps a single definition of "dirty"
    # — the one this function already refused the build over above — as the
    # one that ends up in the manifest.
    manifest["source"]["dirty"] = is_dirty
    manifest["manifest_id"] = brm.compute_manifest_id(manifest)

    brm._write_json(cli.out, manifest)
    manifest["signing"]["gpg"] = brm._gpg_record(cli.out, cli.gpg_key)
    brm._write_json(cli.out, manifest)

    if cli.emit_identity:
        brm._write_json(
            cli.emit_identity,
            brm.identity_subset(manifest, image_key=cli.identity_image),
        )

    print(f"manifest_id={manifest['manifest_id']}")
    print(f"engine_sha={engine_sha}")
    print(f"saas_sha={saas_sha}")
    print(f"source.dirty={manifest['source']['dirty']}")
    print(f"wrote {cli.out}")
    if cli.emit_identity:
        print(f"wrote {cli.emit_identity}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
