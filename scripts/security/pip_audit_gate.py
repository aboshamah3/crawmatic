#!/usr/bin/env python3
"""Gate wrapper around a `pip-audit -f json` report (EPA W5.5-GA, item A).

`pip-audit`'s OSV-backed JSON output does not carry a reliable, cross-
ecosystem CVSS/severity field (see the report schema: each finding is
`{"id", "fix_versions", "aliases", "description"}` — no `severity` key).
That makes an automatic "fail only on CRITICAL" gate impossible to build
honestly from the report alone without either (a) a paid vuln-intel feed
or (b) a second network round-trip per finding against a severity source
that may itself be incomplete for a given advisory.

So this gate is a deliberately conservative, two-tier design instead of a
prettier one that would silently overclaim precision:

* **FAIL** — a finding whose advisory id OR any of its aliases (CVE/GHSA/
  PYSEC/...) appears in the owner-maintained allowlist at
  `scripts/security/known_critical_advisories.txt`. Seeded empty; the
  owner adds an id here after triaging a WARN finding as actually
  critical for this system. This is a real, auditable gate — not a
  rubber stamp — but it starts empty on purpose rather than guessing.
* **WARN** — every other finding. Always printed in full (package,
  installed version, advisory id + aliases, available fix version) to
  both stdout and the job summary, never dropped silently. The build
  stays green; a human triages.

Run against the engine's actual locked dependency set on 2026-08-26
(`uv export --frozen --all-packages --format requirements.txt --no-hashes`
piped through `pip-audit`), this surfaced 3 real, non-allowlisted findings
(cryptography 49.0.0, setuptools 80.10.2, pytest 8.4.2 — see the W5.5-GA
task report) — all WARN, none CRITICAL, which is the correct classification
for a timing side-channel needing a specific S/MIME-gateway shape, a build-
time sdist-packing bug, and a `pytest`-owned `/tmp` race, none of which are
`known_critical_advisories.txt` material without owner triage.

Exit code: 1 iff at least one CRITICAL (allowlisted) finding exists.
Exit code: 0 otherwise (including "some WARN findings, zero CRITICAL").
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


def load_allowlist(path: pathlib.Path) -> set[str]:
    if not path.exists():
        return set()
    ids: set[str] = set()
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        ids.add(line)
    return ids


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


def classify(findings: list[dict], critical_ids: set[str]) -> tuple[list[dict], list[dict]]:
    criticals: list[dict] = []
    warns: list[dict] = []
    for finding in findings:
        ids = {finding["id"], *finding["aliases"]}
        if ids & critical_ids:
            criticals.append(finding)
        else:
            warns.append(finding)
    return criticals, warns


def render_summary(findings: list[dict], criticals: list[dict]) -> str:
    lines = [
        "# pip-audit gate",
        f"- {len(findings)} finding(s) total, {len(criticals)} on the critical allowlist",
        "",
    ]
    critical_ids = {f["id"] for f in criticals}
    if not findings:
        lines.append("No known vulnerabilities in the audited dependency set.")
    for f in findings:
        tag = "CRITICAL" if f["id"] in critical_ids else "warn"
        aliases = ", ".join(f["aliases"]) or "none"
        fixes = ", ".join(f["fix_versions"]) or "no fix published"
        lines.append(
            f"- [{tag}] {f['package']}=={f['version']} — {f['id']} "
            f"(aliases: {aliases}; fix: {fixes})"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="pip-audit `-f json` report path")
    parser.add_argument(
        "--allowlist",
        required=True,
        help="path to the owner-maintained known-critical advisory id list",
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
    critical_ids = load_allowlist(pathlib.Path(args.allowlist))
    criticals, warns = classify(findings, critical_ids)

    summary = render_summary(findings, criticals)
    print(summary)
    if args.summary_out:
        with open(args.summary_out, "a", encoding="utf-8") as fh:
            fh.write(summary)

    if criticals:
        print(
            f"::error::{len(criticals)} finding(s) matched the critical-advisory "
            "allowlist — failing the audit job",
            file=sys.stderr,
        )
        return 1
    if warns:
        print(
            f"::warning::{len(warns)} non-critical finding(s) — see the job "
            "summary; not blocking",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
