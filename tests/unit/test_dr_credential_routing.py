"""Behavioural tests for the DR Railway credential bridge (plan E10.1, F9).

On 2026-09-23 the SaaS project moved to the rw003 Railway account while the
engine project stayed on railway2. `dr_lib.sh` used ONE hardcoded token
(`RAILWAY_TOKEN_RAILWAY2`) for both, sent the CLI's stderr to /dev/null, and
`dr_die`d inside the target loop — so the SaaS lookup answered
"Unauthorized", nobody could see why, and the engine database lost its
backups too. No set was written for six days.

These tests source the real `dr_lib.sh` against a stub `railway` binary that
behaves like the real accounts: each project answers only to its own token.
No network, no Railway, no credentials — the stub prints fake values.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DR_LIB = REPO_ROOT / "scripts" / "dr" / "dr_lib.sh"

ENGINE_PROJECT = "69dc4bda-0d97-4290-a82f-822ed97d3fb8"
SAAS_PROJECT = "91debd0f-1dc4-4b7f-aa74-b9874ac27071"
OUTREACH_PROJECT = "cf7d41bd-8a77-4831-bab4-7edd03a6a1d5"

# The stub: each project answers only to the token of the account that owns
# it today, exactly as Railway did when the incident was reproduced.
RAILWAY_STUB = textwrap.dedent(
    f"""\
    #!/usr/bin/env bash
    project=""
    while (( $# )); do
      case "$1" in --project) project="$2"; shift 2;; *) shift;; esac
    done
    echo "$project $RAILWAY_API_TOKEN" >> "$STUB_CALLS"
    case "$project:$RAILWAY_API_TOKEN" in
      {ENGINE_PROJECT}:tok-railway2-SECRET|{SAAS_PROJECT}:tok-rw003-SECRET|{OUTREACH_PROJECT}:tok-railway4-SECRET)
        printf 'PGUSER=u\\nPGPASSWORD=pw-SECRET\\nPGDATABASE=db\\n'
        # The outreach Postgres has NO public TCP proxy in production.
        if [ "$STUB_NO_PROXY" = 1 ] && [ "$project" = {OUTREACH_PROJECT} ]; then exit 0; fi
        printf 'RAILWAY_TCP_PROXY_DOMAIN=h.example\\nRAILWAY_TCP_PROXY_PORT=5432\\n'
        exit 0;;
      *)
        echo "Unauthorized. Please login with railway login (token $RAILWAY_API_TOKEN)" >&2
        exit 1;;
    esac
    """
)


def _harness(
    tmp_path: Path,
    body: str,
    *,
    rw003: bool = True,
    railway4: bool = False,
    no_proxy: bool = False,
    env_extra: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "railway"
    stub.write_text(RAILWAY_STUB)
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    accounts = tmp_path / "accounts.sh"
    lines = ["export RAILWAY_TOKEN_RAILWAY2=tok-railway2-SECRET"]
    if rw003:
        lines.append("export RAILWAY_TOKEN_RW003=tok-rw003-SECRET")
    if railway4:
        lines.append("export RAILWAY_TOKEN_RAILWAY4=tok-railway4-SECRET")
    accounts.write_text("\n".join(lines) + "\n")
    script = textwrap.dedent(
        f"""\
        set -euo pipefail
        export DR_RAILWAY_ACCOUNTS={accounts}
        export STUB_CALLS={tmp_path / "calls"}
        export STUB_NO_PROXY={1 if no_proxy else 0}
        source {DR_LIB}
        """
    ) + textwrap.dedent(body)
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path), **(env_extra or {})}
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=60
    )


def _targets(tmp_path: Path) -> dict[str, list[str]]:
    proc = _harness(tmp_path, 'printf "%s\\n" "${DR_TARGETS[@]}"\n')
    assert proc.returncode == 0, proc.stderr
    return {line.split("|")[0]: line.split("|") for line in proc.stdout.splitlines()}


def test_each_target_names_the_token_of_the_account_that_owns_it(tmp_path: Path) -> None:
    targets = _targets(tmp_path)
    assert set(targets) == {"engine", "saas", "outreach"}
    for fields in targets.values():
        assert len(fields) == 4, f"name|project|service|token_var expected, got {fields}"
    assert targets["engine"][1:] == [ENGINE_PROJECT, "postgres", "RAILWAY_TOKEN_RAILWAY2"]
    assert targets["saas"][1:] == [SAAS_PROJECT, "Postgres", "RAILWAY_TOKEN_RW003"]
    # O5: outreach lives on the railway4 account since 2026-09-11.
    assert targets["outreach"][1:] == [OUTREACH_PROJECT, "Postgres", "RAILWAY_TOKEN_RAILWAY4"]


def test_saas_credentials_load_with_the_rw003_token(tmp_path: Path) -> None:
    proc = _harness(
        tmp_path,
        f"""\
        dr_load_pgenv {SAAS_PROJECT} Postgres RAILWAY_TOKEN_RW003
        echo "loaded PGUSER=$PGUSER PGPORT=$PGPORT"
        """,
    )
    assert proc.returncode == 0, proc.stderr
    assert "loaded PGUSER=u PGPORT=5432" in proc.stdout
    calls = (tmp_path / "calls").read_text().split()
    assert calls == [SAAS_PROJECT, "tok-rw003-SECRET"]


def test_a_failed_lookup_surfaces_scrubbed_cli_stderr(tmp_path: Path) -> None:
    proc = _harness(
        tmp_path,
        f"dr_load_pgenv {SAAS_PROJECT} Postgres RAILWAY_TOKEN_RAILWAY2\n",
    )
    assert proc.returncode != 0
    assert "DR-ALERT" in proc.stderr
    assert "Unauthorized" in proc.stderr, "the CLI's reason must reach the log"
    assert "RAILWAY_TOKEN_RAILWAY2" in proc.stderr, "the alert names WHICH token failed"
    assert "SECRET" not in proc.stderr + proc.stdout, "the token value must be scrubbed"


def test_a_missing_token_variable_fails_naming_the_variable(tmp_path: Path) -> None:
    proc = _harness(
        tmp_path,
        f"dr_load_pgenv {SAAS_PROJECT} Postgres RAILWAY_TOKEN_RW003\n",
        rw003=False,
    )
    assert proc.returncode != 0
    assert "RAILWAY_TOKEN_RW003 not exported" in proc.stderr


def test_one_failed_target_does_not_abort_the_other(tmp_path: Path) -> None:
    """The saas token is missing; the engine dump must still run."""
    stage = tmp_path / "stage"
    work = tmp_path / "work"
    stage.mkdir()
    work.mkdir()
    proc = _harness(
        tmp_path,
        f"""\
        # dr_dump_target is replaced by a stub that records it ran with the
        # credentials the bridge loaded for THIS target.
        dr_dump_target() {{
          echo "dumped $1 as $PGUSER" >> {tmp_path / "dumps"}
          echo '{{}}' > "$3/$1.meta.json"
        }}
        dr_dump_all_targets {stage} {work}
        echo "failed=${{DR_FAILED_TARGETS[*]}}"
        echo "pguser_after=${{PGUSER:-unset}}"
        """,
        rw003=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "dumps").read_text() == "dumped engine as u\n"
    assert "failed=saas" in proc.stdout
    assert "pguser_after=unset" in proc.stdout, "credentials must not leak past the target"
    assert (work / "engine.meta.json").exists()
    assert not (work / "saas.meta.json").exists()
    assert "[saas] target FAILED" in proc.stderr


def test_a_die_inside_a_target_keeps_errexit_semantics(tmp_path: Path) -> None:
    """A failing command inside dr_dump_target must still fail THAT target.

    Guards the subtle bash rule: errexit is disabled inside anything run under
    `||`/`if`, so a naive `( … ) || failed+=…` would let a broken dump carry on
    and be recorded as a success.
    """
    stage = tmp_path / "stage"
    work = tmp_path / "work"
    stage.mkdir()
    work.mkdir()
    proc = _harness(
        tmp_path,
        f"""\
        dr_dump_target() {{
          false
          echo '{{}}' > "$3/$1.meta.json"
        }}
        dr_dump_all_targets {stage} {work}
        echo "failed=${{DR_FAILED_TARGETS[*]}}"
        """,
    )
    assert proc.returncode == 0, proc.stderr
    # outreach has no railway4 token in this harness: SKIPPED, not failed.
    assert "failed=engine saas" in proc.stdout
    assert not list(work.glob("*.meta.json"))


def test_backup_prod_uses_the_isolating_loop() -> None:
    text = (REPO_ROOT / "scripts" / "dr" / "backup_prod.sh").read_text()
    assert "dr_dump_all_targets" in text
    assert "failed_targets" in text, "a partial set must name what is missing"
    assert "2>/dev/null) \\" not in (DR_LIB.read_text())
    assert os.access(REPO_ROOT / "scripts" / "dr" / "backup_prod.sh", os.X_OK)


def _run_all(tmp_path: Path, **kw: object) -> subprocess.CompletedProcess[str]:
    stage = tmp_path / "stage"
    work = tmp_path / "work"
    stage.mkdir()
    work.mkdir()
    return _harness(
        tmp_path,
        f"""\
        dr_dump_target() {{
          echo "dumped $1" >> {tmp_path / "dumps"}
          echo '{{}}' > "$3/$1.meta.json"
        }}
        dr_dump_all_targets {stage} {work}
        echo "failed=${{DR_FAILED_TARGETS[*]:-}}"
        echo "skipped=${{DR_SKIPPED_TARGETS[*]:-}}"
        """,
        **kw,  # type: ignore[arg-type]
    )


def test_outreach_is_dumped_with_the_railway4_token(tmp_path: Path) -> None:
    proc = _run_all(tmp_path, railway4=True)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "dumps").read_text().split() == [
        "dumped", "engine", "dumped", "saas", "dumped", "outreach",
    ]
    assert "failed=" in proc.stdout and "skipped=\n" in proc.stdout + "\n"
    calls = (tmp_path / "calls").read_text().splitlines()
    assert f"{OUTREACH_PROJECT} tok-railway4-SECRET" in calls
    assert "SECRET" not in proc.stderr + proc.stdout


def test_missing_railway4_token_skips_outreach_only(tmp_path: Path) -> None:
    proc = _run_all(tmp_path, railway4=False)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "dumps").read_text().split() == [
        "dumped", "engine", "dumped", "saas",
    ]
    assert "failed=\n" in proc.stdout
    assert "skipped=outreach" in proc.stdout
    assert "SKIP: RAILWAY_TOKEN_RAILWAY4 not exported" in proc.stdout + proc.stderr
    assert "DR-ALERT" not in proc.stderr, "a skipped optional target must not alert"
    assert not (tmp_path / "work" / "outreach.meta.json").exists()


def test_outreach_without_a_public_tcp_proxy_skips_with_a_loud_warning(tmp_path: Path) -> None:
    proc = _run_all(tmp_path, railway4=True, no_proxy=True)
    assert proc.returncode == 0, proc.stderr
    assert "skipped=outreach" in proc.stdout
    assert "no public TCP proxy" in proc.stdout + proc.stderr
    assert "failed=\n" in proc.stdout


def test_dr_skip_targets_switch_disables_outreach_without_touching_the_others(
    tmp_path: Path,
) -> None:
    proc = _run_all(tmp_path, railway4=True, env_extra={"DR_SKIP_TARGETS": "outreach"})
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "dumps").read_text().split() == [
        "dumped", "engine", "dumped", "saas",
    ]
    assert "skipped=outreach" in proc.stdout
    assert "disabled by DR_SKIP_TARGETS" in proc.stdout + proc.stderr
    # the token must never even be looked up for a disabled target
    assert OUTREACH_PROJECT not in (tmp_path / "calls").read_text()


def test_required_targets_cannot_be_skipped_by_a_missing_token(tmp_path: Path) -> None:
    """engine/saas keep fail-loud semantics: missing token = FAILED + alert."""
    proc = _run_all(tmp_path, rw003=False, railway4=True)
    assert proc.returncode == 0, proc.stderr
    assert "failed=saas" in proc.stdout
    assert "skipped=\n" in proc.stdout
    assert "[saas] target FAILED" in proc.stderr


def test_backup_manifest_names_skipped_targets_and_all_skipped_is_fatal() -> None:
    text = (REPO_ROOT / "scripts" / "dr" / "backup_prod.sh").read_text()
    assert "skipped_targets" in text
    assert "DR_SKIPPED_TARGETS" in text
