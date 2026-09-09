"""Unit tests for `scripts/security/pip_audit_gate.py` (EPA A4/F04).

Covers the three triage outcomes the packet's acceptance criteria name:

1. An advisory absent from `advisory_triage.yaml` fails the gate
   (untriaged).
2. An entry `{id, decision, owner, expires, reason}` whose `expires` is in
   the past fails the gate (expired).
3. A valid (present, not-yet-expired) entry passes.

Pure/off-reactor: builds synthetic `pip-audit -f json`-shaped reports and
synthetic `advisory_triage.yaml` ledgers on disk (tmp_path) — no real
`pip-audit` invocation, no network.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
from pathlib import Path

import pytest
import yaml

# `scripts/` has no __init__.py / installed entry point -- match the
# sys.path convention `tests/unit/test_classify_match_set.py` /
# `tests/unit/test_seed_bootstrap.py` use to import `scripts.<module>`.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.security.pip_audit_gate import (  # noqa: E402
    classify,
    collect_findings,
    load_triage,
    main,
)

TODAY = _dt.date(2026, 9, 7)


def _report(*, id_: str = "GHSA-aaaa-bbbb-cccc", aliases: list[str] | None = None) -> dict:
    return {
        "dependencies": [
            {
                "name": "somepkg",
                "version": "1.2.3",
                "vulns": [
                    {
                        "id": id_,
                        "aliases": aliases or [],
                        "fix_versions": ["1.2.4"],
                    }
                ],
            }
        ]
    }


def _write_report(path: Path, report: dict) -> Path:
    out = path / "pip-audit-report.json"
    out.write_text(json.dumps(report))
    return out


def _write_triage(path: Path, advisories: list[dict]) -> Path:
    out = path / "advisory_triage.yaml"
    out.write_text(yaml.safe_dump({"advisories": advisories}))
    return out


# --- pure classify() tests -------------------------------------------------


def test_untriaged_finding_fails() -> None:
    findings = collect_findings(_report())
    untriaged, expired, valid = classify(findings, triage={}, today=TODAY)
    assert len(untriaged) == 1
    assert not expired
    assert not valid


def test_expired_entry_fails() -> None:
    findings = collect_findings(_report())
    triage = {
        "GHSA-aaaa-bbbb-cccc": {
            "decision": "accept",
            "owner": "sec-owner",
            "expires": "2026-01-01",
            "reason": "past deadline",
        }
    }
    untriaged, expired, valid = classify(findings, triage=triage, today=TODAY)
    assert not untriaged
    assert len(expired) == 1
    assert not valid


def test_expires_today_counts_as_expired() -> None:
    """`expires` is inclusive-past: a triage due TODAY is stale, not valid."""
    findings = collect_findings(_report())
    triage = {
        "GHSA-aaaa-bbbb-cccc": {
            "decision": "fix",
            "owner": "sec-owner",
            "expires": TODAY.isoformat(),
            "reason": "due today",
        }
    }
    untriaged, expired, valid = classify(findings, triage=triage, today=TODAY)
    assert len(expired) == 1


def test_valid_entry_passes() -> None:
    findings = collect_findings(_report())
    triage = {
        "GHSA-aaaa-bbbb-cccc": {
            "decision": "accept",
            "owner": "sec-owner",
            "expires": "2099-01-01",
            "reason": "not exploitable for our deployment shape",
        }
    }
    untriaged, expired, valid = classify(findings, triage=triage, today=TODAY)
    assert not untriaged
    assert not expired
    assert len(valid) == 1


def test_alias_match_counts_as_triaged() -> None:
    """A finding reported under its GHSA id matches an entry keyed by the CVE alias."""
    findings = collect_findings(_report(id_="GHSA-aaaa-bbbb-cccc", aliases=["CVE-2026-0001"]))
    triage = {
        "CVE-2026-0001": {
            "decision": "accept",
            "owner": "sec-owner",
            "expires": "2099-01-01",
            "reason": "tracked under the CVE id",
        }
    }
    untriaged, expired, valid = classify(findings, triage=triage, today=TODAY)
    assert len(valid) == 1


# --- load_triage() schema validation ---------------------------------------


def test_load_triage_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_triage(tmp_path / "does-not-exist.yaml") == {}


def test_load_triage_rejects_missing_required_field(tmp_path: Path) -> None:
    path = _write_triage(
        tmp_path,
        [{"id": "GHSA-x", "decision": "accept", "owner": "sec-owner"}],  # no expires/reason
    )
    with pytest.raises(ValueError, match="expires"):
        load_triage(path)


def test_load_triage_rejects_bad_decision(tmp_path: Path) -> None:
    path = _write_triage(
        tmp_path,
        [
            {
                "id": "GHSA-x",
                "decision": "ignore",
                "owner": "sec-owner",
                "expires": "2099-01-01",
                "reason": "bad decision value",
            }
        ],
    )
    with pytest.raises(ValueError, match="decision"):
        load_triage(path)


# --- main() end-to-end (report + triage files on disk) ---------------------


def test_main_fails_closed_on_missing_report(tmp_path: Path) -> None:
    triage_path = _write_triage(tmp_path, [])
    exit_code = main(
        [
            "--input",
            str(tmp_path / "missing.json"),
            "--triage",
            str(triage_path),
        ]
    )
    assert exit_code == 1


def test_main_untriaged_finding_exits_1(tmp_path: Path) -> None:
    report_path = _write_report(tmp_path, _report())
    triage_path = _write_triage(tmp_path, [])
    exit_code = main(["--input", str(report_path), "--triage", str(triage_path)])
    assert exit_code == 1


def test_main_expired_entry_exits_1(tmp_path: Path) -> None:
    report_path = _write_report(tmp_path, _report())
    triage_path = _write_triage(
        tmp_path,
        [
            {
                "id": "GHSA-aaaa-bbbb-cccc",
                "decision": "accept",
                "owner": "sec-owner",
                "expires": "2020-01-01",
                "reason": "long past",
            }
        ],
    )
    exit_code = main(["--input", str(report_path), "--triage", str(triage_path)])
    assert exit_code == 1


def test_main_valid_entry_exits_0(tmp_path: Path) -> None:
    report_path = _write_report(tmp_path, _report())
    triage_path = _write_triage(
        tmp_path,
        [
            {
                "id": "GHSA-aaaa-bbbb-cccc",
                "decision": "fix",
                "owner": "sec-owner",
                "expires": "2099-01-01",
                "reason": "dependency bump scheduled",
            }
        ],
    )
    exit_code = main(["--input", str(report_path), "--triage", str(triage_path)])
    assert exit_code == 0


def test_main_no_findings_exits_0(tmp_path: Path) -> None:
    report_path = _write_report(tmp_path, {"dependencies": []})
    triage_path = _write_triage(tmp_path, [])
    exit_code = main(["--input", str(report_path), "--triage", str(triage_path)])
    assert exit_code == 0


def test_main_writes_summary_file(tmp_path: Path) -> None:
    report_path = _write_report(tmp_path, _report())
    triage_path = _write_triage(tmp_path, [])
    summary_path = tmp_path / "summary.md"
    main(
        [
            "--input",
            str(report_path),
            "--triage",
            str(triage_path),
            "--summary-out",
            str(summary_path),
        ]
    )
    text = summary_path.read_text()
    assert "UNTRIAGED" in text
    assert "GHSA-aaaa-bbbb-cccc" in text
