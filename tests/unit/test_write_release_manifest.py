"""`scripts/write_release_manifest.py` — the deploy-runbook wrapper around
`build_release_manifest.py` (H6, production-readiness audit).

Follows the same in-process CLI-testing convention as
`tests/unit/test_release_identity.py`'s `_load_script`/`_run_script`: both
scripts under test expose `main(argv) -> int`, so importing them by path and
calling `main` directly exercises the whole argparse surface without the
cost (and subprocess-starvation risk documented in that file) of spawning a
real interpreter per test.

Covers:

1. A dirty engine tree makes `write_release_manifest.py` refuse (exit 2)
   without writing anything.
2. A clean engine (+ SaaS) repo pair produces a manifest with both SHAs
   embedded and `source.dirty is False`.
3. `build_deployment_attestation.py` refuses a manifest whose `source.dirty`
   is `True`, even when its self-hash is otherwise intact.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _ScriptResult:
    returncode: int
    stdout: str
    stderr: str


def _load_script(name: str) -> Any:
    """Import a `scripts/*.py` CLI as a module, once, by path — same
    approach as `test_release_identity.py::_load_script`."""
    module_name = f"_h6_script_{name.removesuffix('.py')}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _REPO_ROOT / "scripts" / name)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _run_script(name: str, *args: str) -> _ScriptResult:
    module = _load_script(name)
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = int(module.main(list(args)) or 0)
    except SystemExit as exc:
        raw = exc.code
        code = 0 if raw is None else (raw if isinstance(raw, int) else 1)
        if isinstance(raw, str):
            err.write(raw)
    return _ScriptResult(returncode=code, stdout=out.getvalue(), stderr=err.getvalue())


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _init_repo(path: Path) -> str:
    """A throwaway git repo with one committed file. Returns HEAD's SHA."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "initial")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return head


def test_refuses_a_dirty_engine_tree(tmp_path: Path) -> None:
    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)
    # Uncommitted change -> dirty.
    (engine_repo / "README.md").write_text("changed\n", encoding="utf-8")

    out = tmp_path / "release_manifest.json"
    result = _run_script(
        "write_release_manifest.py",
        "--out",
        str(out),
        "--engine-repo",
        str(engine_repo),
        "--saas-sha",
        "deadbeef" * 5,
    )

    assert result.returncode == 2
    assert "dirty" in (result.stdout + result.stderr).lower()
    assert not out.exists()


def test_refuses_an_untracked_only_change_too(tmp_path: Path) -> None:
    """`git status --porcelain` reports untracked files too — a build must
    not silently exclude "I forgot to add this" from what counts as dirty."""
    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)
    (engine_repo / "untracked.txt").write_text("new\n", encoding="utf-8")

    out = tmp_path / "release_manifest.json"
    result = _run_script(
        "write_release_manifest.py",
        "--out",
        str(out),
        "--engine-repo",
        str(engine_repo),
    )

    assert result.returncode == 2
    assert not out.exists()


def test_clean_tree_manifest_embeds_both_shas_and_is_not_dirty(tmp_path: Path) -> None:
    engine_repo = tmp_path / "engine"
    saas_repo = tmp_path / "saas"
    engine_sha = _init_repo(engine_repo)
    saas_sha = _init_repo(saas_repo)

    out = tmp_path / "release_manifest.json"
    identity_out = tmp_path / "release_identity.json"
    result = _run_script(
        "write_release_manifest.py",
        "--out",
        str(out),
        "--emit-identity",
        str(identity_out),
        "--engine-repo",
        str(engine_repo),
        "--saas-repo",
        str(saas_repo),
        "--image-digest",
        "api=sha256:" + "11" * 32,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))

    assert manifest["source"]["engine_sha"] == engine_sha
    assert manifest["source"]["saas_sha"] == saas_sha
    assert manifest["source"]["dirty"] is False
    assert manifest["manifest_id"].startswith("sha256:")
    # external_components.saas.commit defaults to the resolved SaaS SHA when
    # the caller didn't explicitly override it.
    assert manifest["external_components"]["saas"]["commit"] == saas_sha

    identity = json.loads(identity_out.read_text(encoding="utf-8"))
    assert identity["manifest_id"] == manifest["manifest_id"]


def test_saas_sha_override_wins_over_saas_repo(tmp_path: Path) -> None:
    engine_repo = tmp_path / "engine"
    saas_repo = tmp_path / "saas"
    _init_repo(engine_repo)
    _init_repo(saas_repo)
    override = "f" * 40

    out = tmp_path / "release_manifest.json"
    result = _run_script(
        "write_release_manifest.py",
        "--out",
        str(out),
        "--engine-repo",
        str(engine_repo),
        "--saas-repo",
        str(saas_repo),
        "--saas-sha",
        override,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["source"]["saas_sha"] == override


def test_manifest_id_is_recomputed_after_embedding_the_shas(tmp_path: Path) -> None:
    """The self-hash must cover `engine_sha`/`saas_sha` too, or a manifest
    could be edited to point at a different SHA without invalidating it."""
    brm = _load_script("build_release_manifest.py")
    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)

    out = tmp_path / "release_manifest.json"
    result = _run_script(
        "write_release_manifest.py",
        "--out",
        str(out),
        "--engine-repo",
        str(engine_repo),
        "--saas-sha",
        "a" * 40,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))

    assert manifest["manifest_id"] == brm.compute_manifest_id(manifest)

    tampered = dict(manifest)
    tampered["source"] = {**tampered["source"], "saas_sha": "b" * 40}
    assert brm.compute_manifest_id(tampered) != manifest["manifest_id"]


def test_allow_dirty_escape_hatch_bypasses_the_refusal(tmp_path: Path) -> None:
    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)
    (engine_repo / "README.md").write_text("changed\n", encoding="utf-8")

    out = tmp_path / "release_manifest.json"
    result = _run_script(
        "write_release_manifest.py",
        "--out",
        str(out),
        "--engine-repo",
        str(engine_repo),
        "--saas-sha",
        "a" * 40,
        "--allow-dirty",
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert manifest["source"]["dirty"] is True


# --------------------------------------------------------------------------
# build_deployment_attestation.py refuses a manifest built from a dirty tree
# --------------------------------------------------------------------------


def _build_clean_manifest(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)
    out = tmp_path / "release_manifest.json"
    result = _run_script(
        "write_release_manifest.py",
        "--out",
        str(out),
        "--engine-repo",
        str(engine_repo),
        "--saas-sha",
        "a" * 40,
        "--image-digest",
        "api=sha256:" + "11" * 32,
    )
    assert result.returncode == 0, result.stderr
    return out, json.loads(out.read_text(encoding="utf-8"))


def test_attestation_refuses_a_manifest_with_dirty_true(tmp_path: Path) -> None:
    brm = _load_script("build_release_manifest.py")
    manifest_path, manifest = _build_clean_manifest(tmp_path)

    manifest["source"] = {**manifest["source"], "dirty": True}
    manifest["manifest_id"] = brm.compute_manifest_id(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_script(
        "build_deployment_attestation.py",
        "--build-manifest",
        str(manifest_path),
        "--out",
        str(tmp_path / "attestation.json"),
        "--environment",
        "staging",
        "--backup-id",
        "backup-1",
        "--approver",
        "owner",
    )

    assert result.returncode != 0
    assert "dirty" in (result.stdout + result.stderr).lower()
    assert not (tmp_path / "attestation.json").exists()


def test_attestation_accepts_a_manifest_with_dirty_false(tmp_path: Path) -> None:
    manifest_path, manifest = _build_clean_manifest(tmp_path)
    assert manifest["source"]["dirty"] is False

    result = _run_script(
        "build_deployment_attestation.py",
        "--build-manifest",
        str(manifest_path),
        "--out",
        str(tmp_path / "attestation.json"),
        "--environment",
        "staging",
        "--backup-id",
        "backup-1",
        "--approver",
        "owner",
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "attestation.json").exists()


def test_no_git_binary_is_not_silently_treated_as_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`shutil.which("git") is None` must not read as "nothing to report" —
    a build host that can't run git at all must not produce a manifest that
    quietly claims dirty=false for a tree no one actually checked. Both the
    direct helper AND the CLI's refusal must fail closed on "unknown", not
    just on "definitely dirty"."""
    wrm = _load_script("write_release_manifest.py")
    monkeypatch.setattr(wrm.shutil, "which", lambda _name: None)

    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)

    assert wrm.engine_tree_is_dirty(engine_repo) is None
    assert wrm.resolve_engine_sha(engine_repo) is None

    out = tmp_path / "release_manifest.json"
    result = _run_script(
        "write_release_manifest.py",
        "--out",
        str(out),
        "--engine-repo",
        str(engine_repo),
    )
    assert result.returncode == 2
    assert not out.exists()


# --------------------------------------------------------------------------
# F20 part 1 — release identity extras (buffer/spool/blocklist versions,
# `alembic heads`, image toolchain, config NAMES)
# --------------------------------------------------------------------------


def test_manifest_carries_buffer_spool_and_blocklist_versions(tmp_path: Path) -> None:
    """The three on-disk/wire schema versions that change behaviour without
    changing the source digest. `scrape_result_spool_version` is an explicit
    `null` until plan task B1 introduces the spool — present-and-null, so the
    manifest shape does not change when B1 lands."""
    from app_shared.netledger.buffer import BUFFER_SCHEMA_VERSION
    from app_shared.profiles.browser_resource_policy import BLOCKLIST_VERSION

    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)
    out = tmp_path / "release_manifest.json"

    result = _run_script(
        "write_release_manifest.py", "--out", str(out), "--engine-repo", str(engine_repo)
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))

    versions = manifest["buffer_versions"]
    assert versions["netledger_buffer_schema_version"] == BUFFER_SCHEMA_VERSION
    assert versions["blocklist_version"] == BLOCKLIST_VERSION
    assert "scrape_result_spool_version" in versions
    assert versions["scrape_result_spool_version"] is None


def test_manifest_carries_toolchain_and_config_names(tmp_path: Path) -> None:
    """Toolchain versions come from the image build definition in the engine
    repo under attestation (base-image line + `uv.lock`), and are overridable
    by a builder that knows the exact value. `config_names` is the flat NAME
    vocabulary `scripts/config_diff_railway.py` diffs a service against —
    names only, never values."""
    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)
    (engine_repo / "apps" / "api").mkdir(parents=True)
    (engine_repo / "apps" / "api" / "Dockerfile").write_text(
        "FROM python:3.13.5-slim-bookworm\n", encoding="utf-8"
    )
    (engine_repo / "uv.lock").write_text(
        '[[package]]\nname = "playwright"\nversion = "1.61.0"\n', encoding="utf-8"
    )
    _git(engine_repo, "add", "-A")
    _git(engine_repo, "commit", "-q", "-m", "image build definition")

    out = tmp_path / "release_manifest.json"
    result = _run_script(
        "write_release_manifest.py", "--out", str(out), "--engine-repo", str(engine_repo)
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))

    assert manifest["toolchain"]["python"] == "3.13.5"
    assert manifest["toolchain"]["playwright"] == "1.61.0"
    # Chromium is installed by playwright at image build; unknown beats a guess.
    assert manifest["toolchain"]["chromium"] is None

    names = manifest["config_names"]
    assert "DATABASE_URL" in names
    assert names == sorted(names)
    assert names == [field["name"] for field in manifest["config_schema"]["fields"]]


def test_chromium_version_override_is_recorded(tmp_path: Path) -> None:
    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)
    out = tmp_path / "release_manifest.json"

    result = _run_script(
        "write_release_manifest.py",
        "--out",
        str(out),
        "--engine-repo",
        str(engine_repo),
        "--chromium-version",
        "141.0.7390.37",
        "--python-version",
        "3.13.5",
        "--playwright-version",
        "1.61.0",
    )
    assert result.returncode == 0, result.stderr
    toolchain = json.loads(out.read_text(encoding="utf-8"))["toolchain"]

    assert toolchain["chromium"] == "141.0.7390.37"
    assert toolchain["chromium_source"] == "explicit --chromium-version"
    assert toolchain["python"] == "3.13.5"
    assert toolchain["playwright"] == "1.61.0"


def test_alembic_heads_are_resolved_from_the_repo_under_attestation(tmp_path: Path) -> None:
    """`alembic heads` is the command `scripts/check_single_head.sh` enforces
    the single-head invariant with, so the manifest attests to what IT says.
    A repo that is not an alembic project records `null` (unknown), never a
    fabricated head."""
    wrm = _load_script("write_release_manifest.py")

    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)
    assert wrm.resolve_alembic_heads(engine_repo) is None

    # The real engine repo resolves to exactly one head.
    heads = wrm.resolve_alembic_heads(_REPO_ROOT)
    assert heads is not None and len(heads) == 1, heads


def test_new_sections_are_covered_by_the_self_hash(tmp_path: Path) -> None:
    """Editing `toolchain` or `buffer_versions` after the fact must invalidate
    `manifest_id` — an unhashed identity block would be worse than none."""
    brm = _load_script("build_release_manifest.py")
    engine_repo = tmp_path / "engine"
    _init_repo(engine_repo)
    out = tmp_path / "release_manifest.json"

    result = _run_script(
        "write_release_manifest.py", "--out", str(out), "--engine-repo", str(engine_repo)
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads(out.read_text(encoding="utf-8"))

    assert brm.compute_manifest_id(manifest) == manifest["manifest_id"]
    tampered = json.loads(json.dumps(manifest))
    tampered["toolchain"]["python"] = "2.7.18"
    assert brm.compute_manifest_id(tampered) != manifest["manifest_id"]
