"""`scripts/config_diff_railway.py` — Railway-vs-`Settings` configuration
diff by variable NAME (F20, core production-readiness plan).

Follows the in-process CLI convention of `tests/unit/test_write_release_manifest.py`
and `tests/unit/test_release_identity.py`: the script exposes
`main(argv) -> int`, so importing it by path and calling `main` exercises the
whole argparse surface without spawning an interpreter per test.

The load-bearing property under test is negative as much as positive: the
diff must be able to say "this name is set" without the tool ever having a
value in hand.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _ScriptResult:
    returncode: int
    stdout: str
    stderr: str


def _load_script(name: str) -> Any:
    module_name = f"_f20_script_{name.removesuffix('.py')}"
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


def _write_names(tmp_path: Path, *names: str) -> Path:
    path = tmp_path / "names.txt"
    path.write_text("\n".join(names) + "\n", encoding="utf-8")
    return path


def _run_diff(tmp_path: Path, *names: str) -> dict[str, Any]:
    names_file = _write_names(tmp_path, *names)
    out = tmp_path / "diff.json"
    result = _run_script(
        "config_diff_railway.py",
        "--service",
        "api",
        "--names-file",
        str(names_file),
        "--out",
        str(out),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(out.read_text(encoding="utf-8"))


def test_present_name_with_a_code_default_is_a_pinned_override(tmp_path: Path) -> None:
    """`DB_POOL_SIZE` is declared in `Settings` with a default (8). A Railway
    service that sets it is pinning that default — and the diff records the
    presence marker, never a value."""
    diff = _run_diff(tmp_path, "DB_POOL_SIZE", "RAILWAY_ENVIRONMENT")

    pinned = {entry["name"]: entry for entry in diff["pinned_overrides"]}
    assert "DB_POOL_SIZE" in pinned
    assert pinned["DB_POOL_SIZE"]["railway"] == "<set>"
    # The recorded default is the one declared in config.py — source, not env.
    assert pinned["DB_POOL_SIZE"]["default"] == 8
    assert "DB_POOL_SIZE" not in diff["missing_in_railway"]


def test_absent_setting_lands_in_missing_in_railway(tmp_path: Path) -> None:
    diff = _run_diff(tmp_path, "DB_POOL_SIZE")

    assert "FLEET_BUDGET_MONTHLY_CAP_USD_PROXY" in diff["missing_in_railway"]
    assert "FLEET_BUDGET_MONTHLY_CAP_USD_PROXY" not in [
        entry["name"] for entry in diff["pinned_overrides"]
    ]


def test_name_railway_has_but_settings_does_not_is_unknown(tmp_path: Path) -> None:
    diff = _run_diff(tmp_path, "DB_POOL_SIZE", "RAILWAY_ENVIRONMENT")

    assert "RAILWAY_ENVIRONMENT" in diff["unknown_to_settings"]
    assert "DB_POOL_SIZE" not in diff["unknown_to_settings"]


def test_required_settings_are_never_pinned_overrides(tmp_path: Path) -> None:
    """A required setting (no code default) that Railway sets is just
    configured, not an override — reporting it as a "pin" would bury the
    handful of real pins in the full variable list."""
    diff = _run_diff(tmp_path, "DATABASE_URL")

    names = [entry["name"] for entry in diff["pinned_overrides"]]
    assert "DATABASE_URL" not in names
    assert "DATABASE_URL" not in diff["missing_in_railway"]


def test_required_and_missing_is_the_boot_blocking_subset(tmp_path: Path) -> None:
    diff = _run_diff(tmp_path, "DB_POOL_SIZE")

    assert "DATABASE_URL" in diff["required_and_missing"]
    assert set(diff["required_and_missing"]) <= set(diff["missing_in_railway"])


def test_a_line_carrying_a_value_is_truncated_and_never_recorded(tmp_path: Path) -> None:
    """Defence in depth: if an operator pipes raw `railway variables --kv`
    output instead of `| cut -d= -f1`, the value must be discarded at parse
    time — the name still diffs, the secret never lands in the JSON."""
    names_file = _write_names(tmp_path, "DB_POOL_SIZE=32", "SOME_TOKEN=super-secret-value")
    out = tmp_path / "diff.json"
    result = _run_script(
        "config_diff_railway.py",
        "--service",
        "api",
        "--names-file",
        str(names_file),
        "--out",
        str(out),
    )

    assert result.returncode == 0, result.stderr
    raw_json = out.read_text(encoding="utf-8")
    assert "super-secret-value" not in raw_json
    assert "super-secret-value" not in result.stdout + result.stderr
    assert "32" not in json.dumps(
        [e for e in json.loads(raw_json)["pinned_overrides"] if e["name"] == "DB_POOL_SIZE"]
    )
    diff = json.loads(raw_json)
    assert "SOME_TOKEN" in diff["unknown_to_settings"]
    # The stripped-value warning reports a COUNT, not content.
    assert "2 line(s)" in result.stderr


def test_output_is_deterministic_when_the_timestamp_is_pinned(tmp_path: Path) -> None:
    names_file = _write_names(tmp_path, "DB_POOL_SIZE")
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    for out in (first, second):
        result = _run_script(
            "config_diff_railway.py",
            "--service",
            "api",
            "--names-file",
            str(names_file),
            "--out",
            str(out),
            "--generated-at",
            "2026-09-07T00:00:00+00:00",
        )
        assert result.returncode == 0, result.stderr

    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_missing_names_file_exits_2(tmp_path: Path) -> None:
    result = _run_script(
        "config_diff_railway.py",
        "--service",
        "api",
        "--names-file",
        str(tmp_path / "nope.txt"),
        "--out",
        str(tmp_path / "diff.json"),
    )

    assert result.returncode == 2
    assert not (tmp_path / "diff.json").exists()
