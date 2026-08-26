#!/usr/bin/env python3
"""import_dataimpulse_usage.py — import a DataImpulse usage export (EPA C5).

Parses a DataImpulse "Statistics / Usage" export in the format
documented by the owner runbook
(``/srv/crawmatic/evidence/canary-2026-08-24/
OWNER_RUNBOOK_dataimpulse_export.md`` § "What to export — exactly") and
hands it to :func:`app_shared.netledger.reconcile.import_provider_usage`,
which persists every row verbatim to ``provider_usage_records`` as
immutable evidence.

**Takes a file path. Nothing else.** No network call, no DataImpulse API
access, no credentials of any kind — the runbook is explicit that the
DataImpulse dashboard is owner-authenticated and no API credential for
it exists on this host; this script's whole job starts AFTER a human has
already exported the file by hand. Running it against
``dataimpulse_usage_2026-08-24.<ext>`` once the owner drops it at
``/srv/crawmatic/evidence/canary-2026-08-24/`` is exactly what closes
that bundle's one open item (MANIFEST.md §8, item 1).

Column mapping
--------------
The runbook lists the columns to request but the real export's exact
header spelling is still unknown (owner-pending at the time this script
was written). :data:`COLUMN_ALIASES` therefore tries several
case-insensitive header spellings per field rather than assuming one;
``--column`` lets an operator override a specific field's header name by
hand for an export whose headers don't match any built-in alias, without
touching this file.

Format: CSV (the runbook's first preference) or JSON — a flat list of
row objects, or ``{"rows": [...]}``. Detected from the file extension,
overridable with ``--format``.

Usage::

    uv run python scripts/import_dataimpulse_usage.py \\
        /srv/crawmatic/evidence/canary-2026-08-24/dataimpulse_usage_2026-08-24.csv \\
        --provider-account <sub-account, if the plan has one> \\
        --window-start 2026-08-24T20:00:00Z --window-end 2026-08-25T06:00:00Z

``window-start``/``window-end`` default to the min/max row timestamp
when omitted and the export is per-request (every row carries its own
timestamp); they are REQUIRED for an hourly/daily aggregate export,
which the runbook allows as a fallback when finer granularity is not
offered.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from app_shared.models.provider_usage import ProviderUsageGranularity
from app_shared.netledger.reconcile import (
    ProviderUsageRow,
    ProviderUsageSource,
    ProviderUsageWindow,
    import_provider_usage,
)

DEFAULT_PROVIDER = "dataimpulse"

#: field -> acceptable (lowercased) header spellings, tried in order.
#: Overridable per-field with ``--column field=header``.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "occurred_at": ("timestamp", "time", "datetime", "occurred_at", "date"),
    "target_host": ("target host/domain", "target_host", "host", "domain", "target"),
    "bytes_up": ("bytes up", "bytes_up", "upload_bytes", "bytes_sent"),
    "bytes_down": ("bytes down", "bytes_down", "download_bytes", "bytes_received"),
    "total_bytes": ("total bytes", "total_bytes", "bytes", "traffic_bytes"),
    "request_count": ("request count", "request_count", "requests", "count"),
    "http_status": ("http status", "http_status", "status", "status_code"),
    "success": ("success/failure", "success", "result", "outcome"),
    "sub_user": ("sub-user", "sub_user", "subuser", "login", "username"),
    "pool_identifier": ("pool identifier", "pool_identifier", "pool", "port"),
    "country": ("country",),
    "billing_line_item": (
        "plan/billing line item",
        "billing_line_item",
        "plan",
        "line_item",
    ),
}


class ImportError_(RuntimeError):
    """Raised for any parse/config failure — caught once in :func:`main`."""


def _resolve_column(
    headers: Sequence[str], field: str, overrides: dict[str, str]
) -> str | None:
    if field in overrides:
        return overrides[field]
    lowered = {h.lower(): h for h in headers}
    for alias in COLUMN_ALIASES.get(field, ()):
        if alias in lowered:
            return lowered[alias]
    return None


def _parse_bool(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "success", "1", "yes", "ok"):
        return True
    if text in ("false", "failure", "failed", "0", "no"):
        return False
    return None


def _parse_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(float(value))


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_from_mapping(raw: dict[str, Any], resolved: dict[str, str | None]) -> ProviderUsageRow:
    def get(field: str) -> Any:
        column = resolved.get(field)
        return raw.get(column) if column is not None else None

    bytes_up = _parse_int(get("bytes_up"))
    bytes_down = _parse_int(get("bytes_down"))
    total_bytes = _parse_int(get("total_bytes"))
    if total_bytes is None:
        # Fall back to summing up/down when the export has no single
        # "total bytes" column — a real possibility the runbook allows
        # for (columns are requested "if the dashboard offers them").
        total_bytes = (bytes_up or 0) + (bytes_down or 0)

    request_count = _parse_int(get("request_count")) or 1

    return ProviderUsageRow(
        occurred_at=_parse_timestamp(get("occurred_at")),
        target_host=(str(get("target_host")).strip() or None) if get("target_host") else None,
        bytes_up=bytes_up,
        bytes_down=bytes_down,
        total_bytes=total_bytes,
        request_count=request_count,
        http_status=_parse_int(get("http_status")),
        success=_parse_bool(get("success")),
        sub_user=(str(get("sub_user")) if get("sub_user") else None),
        pool_identifier=(str(get("pool_identifier")) if get("pool_identifier") else None),
        country=(str(get("country")) if get("country") else None),
        billing_line_item=(
            str(get("billing_line_item")) if get("billing_line_item") else None
        ),
        raw=raw,
    )


def _read_csv_rows(raw_bytes: bytes) -> list[dict[str, Any]]:
    text = raw_bytes.decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    return [dict(row) for row in reader]


def _read_json_rows(raw_bytes: bytes) -> list[dict[str, Any]]:
    payload = json.loads(raw_bytes.decode("utf-8"))
    if isinstance(payload, list):
        return [dict(row) for row in payload]
    if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
        return [dict(row) for row in payload["rows"]]
    raise ImportError_(
        "JSON export must be a list of row objects or {\"rows\": [...]}, got "
        f"{type(payload).__name__}"
    )


def detect_format(path: Path, override: str | None) -> str:
    if override is not None:
        return override.lower()
    suffix = path.suffix.lower().lstrip(".")
    if suffix in ("csv", "json"):
        return suffix
    raise ImportError_(
        f"cannot infer format from extension {path.suffix!r}; pass --format csv|json"
    )


def parse_export(
    path: Path,
    *,
    provider: str,
    provider_account: str | None,
    window_start: datetime | None,
    window_end: datetime | None,
    granularity: ProviderUsageGranularity,
    fmt: str | None = None,
    column_overrides: dict[str, str] | None = None,
    total_cost_minor_units: int | None = None,
    currency: str | None = None,
) -> ProviderUsageSource:
    """Parse ``path`` into a :class:`ProviderUsageSource`, no I/O beyond the read.

    ``raw_bytes`` on the returned source is the file's exact bytes — the
    hash :func:`~app_shared.netledger.reconcile.import_provider_usage`
    computes for idempotent re-import is over exactly what this function
    read, never a re-serialization of the parsed rows.
    """
    raw_bytes = path.read_bytes()
    resolved_format = detect_format(path, fmt)
    if resolved_format == "csv":
        raw_rows = _read_csv_rows(raw_bytes)
    elif resolved_format == "json":
        raw_rows = _read_json_rows(raw_bytes)
    else:
        raise ImportError_(f"unsupported format: {resolved_format!r}")

    if not raw_rows:
        raise ImportError_(f"{path} contains no data rows")

    headers = list(raw_rows[0].keys())
    overrides = column_overrides or {}
    resolved = {
        field: _resolve_column(headers, field, overrides) for field in COLUMN_ALIASES
    }
    if resolved["target_host"] is None:
        raise ImportError_(
            f"could not find a target-host column among {headers!r}; pass "
            "--column target_host=<header name>"
        )
    if resolved["total_bytes"] is None and (
        resolved["bytes_up"] is None and resolved["bytes_down"] is None
    ):
        raise ImportError_(
            f"could not find a bytes column among {headers!r}; pass "
            "--column total_bytes=<header name> (or bytes_up/bytes_down)"
        )

    rows = [_row_from_mapping(raw, resolved) for raw in raw_rows]

    resolved_start = window_start
    resolved_end = window_end
    if resolved_start is None or resolved_end is None:
        timestamps = [row.occurred_at for row in rows if row.occurred_at is not None]
        if not timestamps:
            raise ImportError_(
                "no --window-start/--window-end given and no row carries its own "
                "timestamp (an hourly/daily aggregate export) — pass both explicitly"
            )
        resolved_start = resolved_start or min(timestamps)
        resolved_end = resolved_end or max(timestamps)

    return ProviderUsageSource(
        provider=provider,
        provider_account=provider_account,
        window_start=resolved_start,
        window_end=resolved_end,
        rows=rows,
        source_ref=str(path),
        raw_bytes=raw_bytes,
        granularity=granularity,
        total_cost_minor_units=total_cost_minor_units,
        currency=currency,
    )


def _report(window: ProviderUsageWindow) -> str:
    status = "ALREADY IMPORTED (idempotent no-op)" if window.already_imported else "IMPORTED"
    return "\n".join(
        [
            f"import_dataimpulse_usage: OK — {status}",
            f"  window_id:        {window.id}",
            f"  provider:         {window.provider} (account={window.provider_account!r})",
            f"  window:           {window.window_start.isoformat()} .. "
            f"{window.window_end.isoformat()}",
            f"  rows:             {window.row_count}",
            f"  total_requests:   {window.total_requests}",
            f"  total_bytes:      {window.total_bytes}",
            f"  source_hash:      {window.source_hash}",
        ]
    )


def _parse_column_overrides(pairs: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ImportError_(f"--column expects field=header, got {pair!r}")
        field, header = pair.split("=", 1)
        field = field.strip()
        if field not in COLUMN_ALIASES:
            raise ImportError_(
                f"--column field {field!r} is not one of {sorted(COLUMN_ALIASES)!r}"
            )
        overrides[field] = header.strip()
    return overrides


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="path to the exported CSV/JSON file")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER)
    parser.add_argument("--provider-account", default=None)
    parser.add_argument("--window-start", default=None, help="ISO 8601 UTC, e.g. 2026-08-24T20:00:00Z")
    parser.add_argument("--window-end", default=None, help="ISO 8601 UTC")
    parser.add_argument(
        "--granularity",
        choices=[g.value for g in ProviderUsageGranularity],
        default=ProviderUsageGranularity.PER_REQUEST.value,
    )
    parser.add_argument("--format", choices=["csv", "json"], default=None)
    parser.add_argument(
        "--column",
        action="append",
        default=[],
        metavar="field=header",
        help="override a column's header spelling; repeatable",
    )
    parser.add_argument(
        "--total-cost-minor-units",
        type=int,
        default=None,
        help="an independent invoice total for this window, in integer minor "
        "units (only if the export/invoice actually names one — never guessed)",
    )
    parser.add_argument("--currency", default=None, help="ISO-4217, required with --total-cost-minor-units")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        overrides = _parse_column_overrides(args.column)
        window_start = _parse_timestamp(args.window_start) if args.window_start else None
        window_end = _parse_timestamp(args.window_end) if args.window_end else None
        if not args.path.exists():
            raise ImportError_(f"no such file: {args.path}")
        source = parse_export(
            args.path,
            provider=args.provider,
            provider_account=args.provider_account,
            window_start=window_start,
            window_end=window_end,
            granularity=ProviderUsageGranularity(args.granularity),
            fmt=args.format,
            column_overrides=overrides,
            total_cost_minor_units=args.total_cost_minor_units,
            currency=args.currency,
        )
        window = import_provider_usage(source)
    except ImportError_ as exc:
        print(f"import_dataimpulse_usage: FAIL — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - top-level CLI error boundary
        print(f"import_dataimpulse_usage: FAIL — {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(_report(window))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
