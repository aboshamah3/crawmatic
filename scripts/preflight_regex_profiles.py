#!/usr/bin/env python
"""Run every stored profile regex against adversarial subjects, under a deadline.

EPA A2 (F02) step 5. Release-checklist gate (A10): before a release that
turns on regex execution, prove that no ``scrape_profiles`` row carries a
pattern that blows :data:`Settings.EXTRACTION_REGEX_TIMEOUT_SECONDS` on
input a competitor page could plausibly contain.

Each pattern is executed through
:func:`scrape_core.extraction.regex.search_with_deadline` — the *same*
engine and the *same* deadline the live path uses, so a pass here means
"this pattern is safe under the rule that will actually be enforced",
not "this pattern looked fine to a heuristic".

READ-ONLY. It issues a single ``SELECT`` against ``scrape_profiles`` and
writes nothing to the database. It is nevertheless **not** to be pointed
at production: a pattern that blows its deadline burns
``EXTRACTION_REGEX_TIMEOUT_SECONDS`` of CPU per subject on whatever host
runs the script, and production hosts are sized for scraping, not for
absorbing a deliberate ReDoS sweep. Run it against a restored dump or
staging.

Usage::

    PREFLIGHT_DATABASE_URL='postgresql+psycopg://...@staging:6432/db' \
        uv run python scripts/preflight_regex_profiles.py --report out.json

    # offline: check patterns from a JSON file instead of a database
    uv run python scripts/preflight_regex_profiles.py \
        --patterns-file patterns.json --report out.json

Exit codes: ``0`` no offender, ``2`` at least one offender (or an
unrunnable pattern), ``1`` could not run at all (no URL, connection
refused).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from typing import Any

from scrape_core.extraction.regex import RegexDeadlineExceeded, search_with_deadline

from app_shared.config import get_settings

#: The six adversarial subjects. Two families, three lengths each, chosen
#: to bracket what a real product page can hand a price/stock pattern:
#: a run of one letter with a non-matching sentinel (the classic
#: ``(a+)+$`` blowup) and a run of digits (the shape every *money* pattern
#: is written against, so the shape most likely to be quadratic in a
#: hand-written ``(\d+[.,])*`` rule). Lengths 64/256/1024 because a
#: catastrophic pattern that survives 64 characters routinely dies on
#: 1024, and 1024 is well inside one real text node.
ADVERSARIAL_SUBJECTS: tuple[str, ...] = (
    "a" * 64 + "!",
    "a" * 256 + "!",
    "a" * 1024 + "!",
    "9" * 64,
    "9" * 256,
    "9" * 1024,
)

#: Only the two fields the task names. ``old_price_regex``/
#: ``currency_regex`` are deliberately out: this gate is about the rules
#: that decide whether a page yields a price at all.
PREFLIGHT_FIELDS: tuple[str, ...] = ("price_regex", "stock_regex")


@dataclass
class Offender:
    """One (profile, field, subject) triple that failed the gate."""

    scrape_profile_id: str
    workspace_id: str | None
    profile_name: str
    field: str
    pattern: str
    subject_kind: str
    subject_length: int
    reason: str
    elapsed_seconds: float


def _subject_kind(subject: str) -> str:
    return "letters+sentinel" if subject.startswith("a") else "digits"


def check_pattern(
    pattern: str,
    *,
    timeout: float,
    profile_id: str = "-",
    workspace_id: str | None = None,
    profile_name: str = "-",
    field: str = "-",
) -> list[Offender]:
    """Every adversarial subject this pattern fails, in order. Empty = clean."""
    offenders: list[Offender] = []
    for subject in ADVERSARIAL_SUBJECTS:
        started = time.monotonic()
        try:
            search_with_deadline(pattern, subject, timeout=timeout)
        except RegexDeadlineExceeded:
            reason = f"exceeded the {timeout}s deadline"
        except Exception as exc:  # uncompilable / engine refusal
            reason = f"{type(exc).__name__}: {exc}"
        else:
            continue
        offenders.append(
            Offender(
                scrape_profile_id=profile_id,
                workspace_id=workspace_id,
                profile_name=profile_name,
                field=field,
                pattern=pattern,
                subject_kind=_subject_kind(subject),
                subject_length=len(subject),
                reason=reason,
                elapsed_seconds=round(time.monotonic() - started, 4),
            )
        )
    return offenders


def _load_rows_from_db(database_url: str) -> list[dict[str, Any]]:
    from sqlalchemy import create_engine, text

    engine = create_engine(database_url, future=True)
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text(
                    "SELECT id, workspace_id, name, price_regex, stock_regex "
                    "FROM scrape_profiles"  # noqa: workspace-scope - operator sweep
                )
            )
            return [dict(row._mapping) for row in result]
    finally:
        engine.dispose()


def _load_rows_from_file(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("--patterns-file must contain a JSON list of profile objects")
    return payload


def run(rows: list[dict[str, Any]], *, timeout: float) -> list[Offender]:
    offenders: list[Offender] = []
    for row in rows:
        for field in PREFLIGHT_FIELDS:
            pattern = row.get(field)
            if not pattern:
                continue
            offenders.extend(
                check_pattern(
                    str(pattern),
                    timeout=timeout,
                    profile_id=str(row.get("id", "-")),
                    workspace_id=(
                        str(row["workspace_id"]) if row.get("workspace_id") else None
                    ),
                    profile_name=str(row.get("name", "-")),
                    field=field,
                )
            )
    return offenders


#: Mirrors `Settings.EXTRACTION_REGEX_TIMEOUT_SECONDS`. Used only when the
#: process has no usable environment (the `--patterns-file` mode is meant to
#: be runnable on a laptop with no DATABASE_URL/JWT_SECRET/... in scope), so
#: the gate never silently changes strictness because config failed to load.
FALLBACK_TIMEOUT_SECONDS = 0.25


def _default_timeout() -> float:
    try:
        return float(get_settings().EXTRACTION_REGEX_TIMEOUT_SECONDS)
    except Exception:
        return FALLBACK_TIMEOUT_SECONDS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--report", required=True, help="path to write the JSON report to"
    )
    parser.add_argument(
        "--patterns-file",
        default=None,
        help="read profiles from this JSON file instead of a database",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="per-subject deadline (default: EXTRACTION_REGEX_TIMEOUT_SECONDS)",
    )
    args = parser.parse_args(argv)

    timeout = args.timeout if args.timeout is not None else _default_timeout()

    if args.patterns_file:
        rows = _load_rows_from_file(args.patterns_file)
        source = f"file:{args.patterns_file}"
    else:
        database_url = os.environ.get("PREFLIGHT_DATABASE_URL") or os.environ.get(
            "DATABASE_URL"
        )
        if not database_url:
            print(
                "preflight_regex_profiles: no PREFLIGHT_DATABASE_URL/DATABASE_URL set "
                "and no --patterns-file given.",
                file=sys.stderr,
            )
            return 1
        try:
            rows = _load_rows_from_db(database_url)
        except Exception as exc:
            # The URL itself is never echoed - it carries a password.
            print(
                f"preflight_regex_profiles: could not read scrape_profiles "
                f"({type(exc).__name__}).",
                file=sys.stderr,
            )
            return 1
        source = "database"

    offenders = run(rows, timeout=timeout)

    report = {
        "source": source,
        "timeout_seconds": timeout,
        "subjects": [
            {"kind": _subject_kind(s), "length": len(s)} for s in ADVERSARIAL_SUBJECTS
        ],
        "profiles_checked": len(rows),
        "patterns_checked": sum(
            1 for row in rows for field in PREFLIGHT_FIELDS if row.get(field)
        ),
        "offender_count": len(offenders),
        "offenders": [asdict(offender) for offender in offenders],
    }
    with open(args.report, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")

    if offenders:
        print(
            f"preflight_regex_profiles: FAIL - {len(offenders)} offending "
            f"(profile, field, subject) triples; report written to {args.report}",
            file=sys.stderr,
        )
        for offender in offenders:
            print(
                f"  {offender.profile_name} ({offender.scrape_profile_id}) "
                f"{offender.field}: {offender.reason} on "
                f"{offender.subject_kind}[{offender.subject_length}]",
                file=sys.stderr,
            )
        return 2

    print(
        f"preflight_regex_profiles: OK - {report['patterns_checked']} patterns across "
        f"{report['profiles_checked']} profiles, no offender. Report: {args.report}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
