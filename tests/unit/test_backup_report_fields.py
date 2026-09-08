"""The measured backup report's payload contract (EPA C10, F21).

`scripts/dr/dr_lib.sh` builds one JSON document per backup leg and POSTs it to
`/admin/ops/backup-report`. It is the ONLY evidence that moving the dump
inside Railway actually removed the egress it was supposed to remove (the
2026-09 deep dive's baseline: 0.853 GB/day), so its shape is a contract:

* `bytes_exported`  plaintext out of `pg_dump`, counted in the pipe BEFORE
                    encryption -- the size of what was backed up;
* `bytes_encrypted` the ciphertext that landed on the volume;
* `bytes_on_wire`   what the network namespace's own counters moved --
                    the number the egress claim rests on;
* `private_network` which side of the move this leg came from.

The functions under test are shell, so they are exercised as shell: the tests
source `dr_lib.sh` in a subprocess exactly the way `backup.sh` and
`backup_prod.sh` do, and assert on real output rather than on a Python
re-implementation of it.

`dr_pipe_count` gets its own test because it is the one measurement with a
race in it: an earlier `tee >(wc -c > file)` shape produced an intermittently
EMPTY count, which is precisely the silently-wrong number this report must
never carry.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DR_LIB = REPO_ROOT / "scripts" / "dr" / "dr_lib.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="dr_lib.sh's JSON builders require jq"
)

TARGETS = [
    {
        "name": "engine",
        "bytes_exported": 134_217_728,
        "bytes_encrypted": 27_818_309,
        "bytes_on_wire": 28_500_000,
        "dump_seconds": 40,
        "server_version": "18.4 (Debian 18.4-1.pgdg13+1)",
        "alembic_head": "e7b34c0af219",
    },
    {
        "name": "saas",
        "bytes_exported": 34_603_008,
        "bytes_encrypted": 4_581_414,
        "bytes_on_wire": 4_700_000,
        "dump_seconds": 4,
        "server_version": "18.6 (Debian 18.6-1.pgdg13+2)",
        "alembic_head": "none",
    },
]
RETENTION = {"hours": 48, "days": 14, "weeks": 8, "sets_kept": 41, "bytes_stored": 1_400_000_000}


def run_lib(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Source dr_lib.sh and run `script`, the way the DR scripts do."""
    return subprocess.run(
        ["bash", "-c", f'source "{DR_LIB}"\n{script}'],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/tmp", **(env or {})},
        timeout=60,
    )


def build_payload(tmp_path: Path, targets=TARGETS, retention=RETENTION, private="true") -> dict:
    targets_file = tmp_path / "targets.json"
    targets_file.write_text(json.dumps(targets), encoding="utf-8")
    proc = run_lib(
        f"dr_backup_report_payload backup dr-backup set-20260908T120000Z "
        f"'{targets_file}' '{json.dumps(retention)}'",
        env={"DR_PRIVATE_NETWORK": private},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_payload_carries_every_contract_field(tmp_path: Path) -> None:
    payload = build_payload(tmp_path)
    assert payload["schema"] == "crawmatic.backup-report.v1"
    assert payload["backup_set"] == "set-20260908T120000Z"
    assert payload["stage"] == "backup"
    assert payload["source"] == "dr-backup"
    assert payload["private_network"] is True
    assert payload["created_utc"].endswith("Z")
    assert [t["name"] for t in payload["targets"]] == ["engine", "saas"]
    for target in payload["targets"]:
        for field in (
            "bytes_exported",
            "bytes_encrypted",
            "bytes_on_wire",
            "dump_seconds",
            "server_version",
            "alembic_head",
        ):
            assert field in target, f"{target['name']} is missing {field}"


def test_totals_are_the_sum_of_the_targets(tmp_path: Path) -> None:
    payload = build_payload(tmp_path)
    assert payload["totals"] == {
        "bytes_exported": 134_217_728 + 34_603_008,
        "bytes_encrypted": 27_818_309 + 4_581_414,
        "bytes_on_wire": 28_500_000 + 4_700_000,
        "seconds": 44,
    }


def test_bytes_exported_exceeds_bytes_encrypted_is_not_assumed(tmp_path: Path) -> None:
    """The builder must not "helpfully" normalise the numbers it is given.

    Compression means ciphertext is normally far smaller than plaintext, but a
    report that silently fixed up a surprising pair would hide exactly the
    anomaly (an empty dump, a failed compression) worth seeing.
    """
    odd = [dict(TARGETS[0], bytes_exported=1, bytes_encrypted=999, bytes_on_wire=0)]
    payload = build_payload(tmp_path, targets=odd)
    assert payload["targets"][0]["bytes_exported"] == 1
    assert payload["targets"][0]["bytes_encrypted"] == 999
    assert payload["totals"]["bytes_on_wire"] == 0


def test_private_network_flag_follows_the_leg(tmp_path: Path) -> None:
    assert build_payload(tmp_path, private="true")["private_network"] is True
    assert build_payload(tmp_path, private="false")["private_network"] is False


def test_retention_window_is_reported_verbatim(tmp_path: Path) -> None:
    payload = build_payload(tmp_path)
    assert payload["retention"] == RETENTION


def test_empty_target_list_produces_zero_totals_not_null(tmp_path: Path) -> None:
    payload = build_payload(tmp_path, targets=[])
    assert payload["targets"] == []
    assert payload["totals"] == {
        "bytes_exported": 0,
        "bytes_encrypted": 0,
        "bytes_on_wire": 0,
        "seconds": 0,
    }


def test_payload_contains_no_credential_material(tmp_path: Path) -> None:
    raw = json.dumps(build_payload(tmp_path)).lower()
    for forbidden in ("password", "passphrase", "pgpassword", "postgres://", "postgresql://", "bearer"):
        assert forbidden not in raw, f"the report payload must never carry {forbidden}"


def test_post_is_fail_soft_when_no_sink_is_configured(tmp_path: Path) -> None:
    """A backup that succeeded must not be turned into a failure by a
    reporting endpoint that is unset or down."""
    payload = tmp_path / "payload.json"
    payload.write_text('{"schema":"crawmatic.backup-report.v1"}', encoding="utf-8")
    # The call shape the DR scripts actually use: `... || true` under the
    # `set -euo pipefail` dr_lib.sh turns on. The run must CONTINUE.
    proc = run_lib(
        f"if dr_post_backup_report '{payload}'; then echo rc=0; else echo rc=$?; fi\n"
        "echo still_running",
        env={"DR_REPORT_URL": ""},
    )
    assert "rc=1" in proc.stdout, proc.stdout + proc.stderr
    assert "still_running" in proc.stdout, "a failed post must not abort the backup"
    assert "WARN" in proc.stdout
    # …and it must say WHERE the unsent report is, so it is not just lost.
    assert str(payload) in proc.stdout


def test_post_without_a_token_does_not_post(tmp_path: Path) -> None:
    payload = tmp_path / "payload.json"
    payload.write_text("{}", encoding="utf-8")
    proc = run_lib(
        f"if dr_post_backup_report '{payload}'; then echo rc=0; else echo rc=$?; fi",
        env={"DR_REPORT_URL": "http://api.railway.internal:8000", "DR_REPORT_TOKEN": ""},
    )
    assert "rc=1" in proc.stdout
    assert "no token" in proc.stdout


@pytest.mark.parametrize("size", [0, 1, 7, 65_536, 1_000_000])
def test_dr_pipe_count_measures_the_stream_exactly(tmp_path: Path, size: int) -> None:
    """The count file must be COMPLETE when the pipeline returns.

    This is the regression guard for the process-substitution race: with
    `tee >(wc -c > f)` the shell does not wait for the counter, so the caller
    intermittently read an empty file and recorded bytes_exported=0.
    """
    out = tmp_path / "count"
    src = tmp_path / "src"
    src.write_bytes(b"x" * size)
    proc = run_lib(f"cat '{src}' | dr_pipe_count '{out}' > /dev/null; cat '{out}'")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(size)


def test_dr_pipe_count_passes_the_stream_through_unchanged(tmp_path: Path) -> None:
    out = tmp_path / "count"
    proc = run_lib(f"printf 'abcdef' | dr_pipe_count '{out}'")
    assert proc.stdout == "abcdef"
    assert out.read_text().strip() == "6"


def test_net_bytes_is_a_monotonic_counter() -> None:
    """`bytes_on_wire` is a delta of this reading; a non-numeric or shrinking
    counter would silently produce nonsense byte totals."""
    first = run_lib("dr_net_bytes")
    second = run_lib("dr_net_bytes")
    assert first.stdout.isdigit() and second.stdout.isdigit(), (first.stdout, second.stdout)
    assert int(second.stdout) >= int(first.stdout)
