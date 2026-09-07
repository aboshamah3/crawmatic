#!/usr/bin/env python3
"""Gate wrapper around a `pip-audit -f json` report (EPA A4/F04, replaces
the W5.5-GA item A two-tier design).

`pip-audit`'s OSV-backed JSON output does not carry a reliable, cross-
ecosystem CVSS/severity field (see the report schema: each finding is
`{"id", "fix_versions", "aliases", "description"}` — no `severity` key).
The original design (fail only on an owner-maintained "known critical"
allowlist, WARN forever on everything else) made that limitation honest,
but it also meant a real, unreviewed finding could sit as a permanent WARN
indefinitely — nothing ever forced a second look.

This gate inverts that default. Every finding `pip-audit` reports MUST
have a matching entry in the owner-maintained triage ledger at
`scripts/security/advisory_triage.yaml`
(``{id, decision: accept|fix, owner, expires: YYYY-MM-DD, reason}``), or
the gate FAILS. A triaged entry only keeps passing while `expires` is in
the future — a lapsed triage is treated exactly like no triage at all,
because the owner's last look at it is now stale.

Three outcomes per finding:

* **untriaged** — no entry in the ledger (by id or alias) — FAIL.
* **expired**   — an entry exists but `expires` is today or in the past —
  FAIL.
* **valid**     — an entry exists and `expires` is in the future — PASS
  (for that finding); printed in the summary either way so a human can
  see what is currently accepted and when it needs review again.

`decision: accept` vs `decision: fix` do not change gate logic — both pass
identically while valid. The field exists for human triage tracking (has
the owner accepted the risk, or committed to removing it) and is echoed in
the summary, never used as a leniency switch.

Exit code: 1 if any finding is untriaged or expired, or if the report
itself is missing/empty/invalid. Exit code: 0 iff every finding has a
currently-valid triage entry (including the "no findings at all" case).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import pathlib
import sys

import yaml


def load_triage(path: pathlib.Path) -> dict[str, dict]:
    """Return ``{advisory_id: entry}`` for every id/alias listed in the ledger.

    A single entry with ``id: GHSA-xxxx`` is indexed only under that one
    id — matching against a finding's aliases is done by the caller
    (:func:`classify`), which checks the finding's own id AND aliases
    against this map's keys. Keeping the map keyed by the ledger's literal
    ``id`` (rather than pre-expanding aliases the ledger doesn't itself
    enumerate) keeps this loader a pure, mechanical parse of the file with
    nothing invented.
    """
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text()) or {}
    entries = raw.get("advisories") or []
    by_id: dict[str, dict] = {}
    for entry in entries:
        eid = entry.get("id")
        if not eid:
            continue
        for required in ("decision", "owner", "expires", "reason"):
            if required not in entry:
                raise ValueError(
                    f"advisory_triage.yaml entry {eid!r} is missing required "
                    f"field {required!r}"
                )
        if entry["decision"] not in ("accept", "fix"):
            raise ValueError(
                f"advisory_triage.yaml entry {eid!r} has decision "
                f"{entry['decision']!r} — must be 'accept' or 'fix'"
            )
        by_id[eid] = entry
    return by_id


def collect_findings(report: dict) -> list[dict]:
    findings: list[dict] = []
    for dep in report.get("dependencies", []):
        if dep.get("skip_reason"):
            continue
        name = dep.get("name")
        version = dep.get("version")
        seen_ids: set[str] = set()
        for vuln in dep.get("vulns") or []:
            vid = vuln.get("id")
            if vid in seen_ids:
                # pip-audit can list the same advisory twice (once per
                # OSV record that maps to it, e.g. PYSEC + GHSA-derived
                # duplicate entries with identical id) — de-dupe on id.
                continue
            seen_ids.add(vid)
            aliases = sorted(set(vuln.get("aliases") or []))
            findings.append(
                {
                    "package": name,
                    "version": version,
                    "id": vid,
                    "aliases": aliases,
                    "fix_versions": vuln.get("fix_versions") or [],
                }
            )
    return findings


def classify(
    findings: list[dict], triage: dict[str, dict], *, today: _dt.date
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split findings into (untriaged, expired, valid) — in that order."""
    untriaged: list[dict] = []
    expired: list[dict] = []
    valid: list[dict] = []
    for finding in findings:
        ids = {finding["id"], *finding["aliases"]}
        matched_id = next((i for i in ids if i in triage), None)
        if matched_id is None:
            untriaged.append(finding)
            continue
        entry = triage[matched_id]
        expires = entry["expires"]
        if isinstance(expires, str):
            expires = _dt.date.fromisoformat(expires)
        finding_with_entry = {**finding, "triage": entry, "matched_id": matched_id}
        if expires <= today:
            expired.append(finding_with_entry)
        else:
            valid.append(finding_with_entry)
    return untriaged, expired, valid


def render_summary(
    findings: list[dict],
    untriaged: list[dict],
    expired: list[dict],
    valid: list[dict],
) -> str:
    lines = [
        "# pip-audit gate (advisory triage)",
        f"- {len(findings)} finding(s) total: {len(untriaged)} untriaged, "
        f"{len(expired)} expired, {len(valid)} validly triaged",
        "",
    ]
    if not findings:
        lines.append("No known vulnerabilities in the audited dependency set.")
    for f in untriaged:
        aliases = ", ".join(f["aliases"]) or "none"
        fixes = ", ".join(f["fix_versions"]) or "no fix published"
        lines.append(
            f"- [UNTRIAGED] {f['package']}=={f['version']} — {f['id']} "
            f"(aliases: {aliases}; fix: {fixes})"
        )
    for f in expired:
        entry = f["triage"]
        lines.append(
            f"- [EXPIRED] {f['package']}=={f['version']} — {f['id']} "
            f"(matched {f['matched_id']}, decision={entry['decision']}, "
            f"owner={entry['owner']}, expired {entry['expires']})"
        )
    for f in valid:
        entry = f["triage"]
        lines.append(
            f"- [triaged:{entry['decision']}] {f['package']}=={f['version']} — "
            f"{f['id']} (matched {f['matched_id']}, owner={entry['owner']}, "
            f"expires {entry['expires']})"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="pip-audit `-f json` report path")
    parser.add_argument(
        "--triage",
        required=True,
        help="path to the owner-maintained advisory_triage.yaml ledger",
    )
    parser.add_argument(
        "--summary-out",
        default=None,
        help="path to append the human-readable summary to (e.g. $GITHUB_STEP_SUMMARY)",
    )
    args = parser.parse_args(argv)

    report_path = pathlib.Path(args.input)
    if not report_path.exists() or report_path.stat().st_size == 0:
        # Fail-closed: a missing/empty report means pip-audit itself did
        # not complete (crash, or an OSV lookup that never returned), not
        # "found nothing." Silently treating that as a pass would let a
        # flaky/broken audit look identical to a clean one.
        print(
            f"::error::pip-audit report not found or empty at {report_path} — "
            "treating as an audit failure, not a clean scan",
            file=sys.stderr,
        )
        return 1

    report = json.loads(report_path.read_text())
    findings = collect_findings(report)
    triage = load_triage(pathlib.Path(args.triage))
    untriaged, expired, valid = classify(findings, triage, today=_dt.date.today())

    summary = render_summary(findings, untriaged, expired, valid)
    print(summary)
    if args.summary_out:
        with open(args.summary_out, "a", encoding="utf-8") as fh:
            fh.write(summary)

    if untriaged or expired:
        print(
            f"::error::{len(untriaged)} untriaged and {len(expired)} expired "
            "finding(s) — failing the audit job. Add or renew an entry in "
            "advisory_triage.yaml.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
