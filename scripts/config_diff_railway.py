#!/usr/bin/env python3
"""Diff a Railway service's configured variable NAMES against
``app_shared.config.Settings`` (F20, core production-readiness plan).

Why this exists
---------------

A release is only reproducible if the environment it runs in is known.
The release manifest already carries the config *schema* (names + types,
never values — see ``scripts/build_release_manifest.py``); this script is
the other half: it answers "which of those names does this Railway
service actually have set, which does it have that the code has never
heard of, and which code defaults is the environment quietly pinning?"

Secret discipline (the whole design constraint)
----------------------------------------------

**This script never reads, stores, prints or writes a variable VALUE.**
Its input is a NAMES file the operator produces with::

    railway variables --service <service> --kv | cut -d= -f1 > names.txt

``cut`` strips every value before the names ever reach this process. If a
line nevertheless contains an ``=`` (someone piped the raw ``--kv``
output), everything after the first ``=`` is discarded immediately at
parse time and the script warns — by *count*, never by content — so a
mistake degrades into a warning instead of a leak. The only "value" that
ever appears in the output is the *code* default declared in
``config.py`` for a pinned override, which is source, not a credential.

Usage::

    uv run python scripts/config_diff_railway.py \\
        --service api --names-file names.txt --out build/config_diff.json

Output (``diff.json``)::

    {
      "service": "api",
      "generated_at": "...",
      "missing_in_railway":  ["FLEET_BUDGET_MONTHLY_CAP_USD_PROXY", ...],
      "unknown_to_settings": ["RAILWAY_ENVIRONMENT", ...],
      "pinned_overrides":    [{"name": "DB_POOL_SIZE",
                               "railway": "<set>",
                               "default": 8}, ...]
    }

* ``missing_in_railway`` — declared in ``Settings``, absent from the
  service. Entries with ``required: true`` are the ones that make the
  service fail to boot; the rest fall back to their code default.
* ``unknown_to_settings`` — set on the service, unknown to ``Settings``.
  Platform variables (``RAILWAY_*``, ``PORT``, ...) live here legitimately;
  anything else is either dead configuration or a typo of a real name.
* ``pinned_overrides`` — set on the service AND carrying a code default:
  the environment is pinning a value the code would otherwise choose.
  ``railway`` is the literal string ``"<set>"`` — presence, never content.

Exit status is 0 whenever the diff was produced. This is a reporting
tool, not a gate; a caller that wants a gate reads the JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "libs" / "shared"))

#: Presence marker written into ``pinned_overrides``. A deliberate constant
#: so no code path can ever be tempted to substitute the real value here.
SET_MARKER = "<set>"


def parse_names_file(path: Path) -> tuple[list[str], int]:
    """Return ``(names, values_stripped)`` from a names file.

    Blank lines and ``#`` comments are ignored. A line containing ``=`` is
    truncated at the first ``=`` and the remainder is dropped on the floor
    (never bound to a name, never logged) — the count of such lines is
    returned so ``main`` can warn about the operator's pipeline without
    echoing a single character of what was stripped.
    """
    names: list[str] = []
    values_stripped = 0
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            values_stripped += 1
            line = line.split("=", 1)[0].strip()
            if not line:
                continue
        names.append(line)
    # De-duplicate, keep deterministic order.
    return sorted(set(names)), values_stripped


def _json_safe(value: Any) -> Any:
    """Code defaults are source, not secrets — but they still have to be
    JSON. Anything pydantic hands back that json can't encode (an enum, a
    ``Path``, a sentinel) is rendered with ``repr`` rather than crashing
    the diff."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    return repr(value)


def _settings_fields() -> dict[str, Any]:
    """``Settings.model_fields`` — read from the class, never an instance.

    Importing the class evaluates field *declarations* only, so no
    environment value is in scope at any point (the same property
    ``app_shared.release.config_schema()`` relies on).
    """
    from app_shared.config import Settings

    return dict(Settings.model_fields)


def build_diff(*, service: str, railway_names: list[str], generated_at: str | None = None) -> dict[str, Any]:
    fields = _settings_fields()
    declared = set(fields)
    present = set(railway_names)

    missing_in_railway = sorted(declared - present)
    unknown_to_settings = sorted(present - declared)

    pinned_overrides: list[dict[str, Any]] = []
    for name in sorted(declared & present):
        field = fields[name]
        if field.is_required():
            # Required settings have no code default to override — the
            # environment is the only source, so "set" is the norm, not a pin.
            continue
        pinned_overrides.append(
            {
                "name": name,
                "railway": SET_MARKER,
                "default": _json_safe(field.get_default(call_default_factory=True)),
            }
        )

    return {
        "service": service,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "note": "variable NAMES only — no Railway value is ever read or written by this tool",
        "missing_in_railway": missing_in_railway,
        "unknown_to_settings": unknown_to_settings,
        "pinned_overrides": pinned_overrides,
        "required_and_missing": sorted(
            name for name in missing_in_railway if fields[name].is_required()
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--service", required=True, help="Railway service the names came from")
    parser.add_argument(
        "--names-file",
        required=True,
        type=Path,
        help="file of variable NAMES, one per line "
        "(`railway variables --service <s> --kv | cut -d= -f1`)",
    )
    parser.add_argument("--out", required=True, type=Path, help="where to write diff.json")
    parser.add_argument(
        "--generated-at",
        default=None,
        help="pin the timestamp (ISO-8601) so two runs are byte-comparable",
    )
    cli = parser.parse_args(argv)

    if not cli.names_file.exists():
        print(f"names file not found: {cli.names_file}", file=sys.stderr)
        return 2

    names, values_stripped = parse_names_file(cli.names_file)
    if values_stripped:
        print(
            f"warning: {values_stripped} line(s) in {cli.names_file} contained '='; "
            "everything after the first '=' was discarded unread. Produce the file "
            "with `railway variables --service <s> --kv | cut -d= -f1` so values "
            "never leave Railway.",
            file=sys.stderr,
        )

    diff = build_diff(service=cli.service, railway_names=names, generated_at=cli.generated_at)

    cli.out.parent.mkdir(parents=True, exist_ok=True)
    cli.out.write_text(json.dumps(diff, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"service={diff['service']}")
    print(f"missing_in_railway={len(diff['missing_in_railway'])}")
    print(f"  of those required={len(diff['required_and_missing'])}")
    print(f"unknown_to_settings={len(diff['unknown_to_settings'])}")
    print(f"pinned_overrides={len(diff['pinned_overrides'])}")
    print(f"wrote {cli.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
