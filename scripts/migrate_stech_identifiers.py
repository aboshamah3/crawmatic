#!/usr/bin/env python3
"""migrate_stech_identifiers.py — EPA B4: type the legacy S-Tech identifiers.

``competitor_product_matches.competitor_variant_identifier`` is one
**untyped** text slot. The Shopify adapter compared it straight against
``variants[].id``; on S-Tech (``stech.ink``) it in fact holds a product
handle, a barcode, or a supplier SKU, so the 2026-08-24 production canary
answered terminal ``NOT_LISTED`` for 26 of 30 targets whose product JSON
had come back HTTP 200 and healthy
(``PRODUCTION_READINESS_REPORT_2026-08-24.md``).

This script migrates those values into the typed
``match_competitor_identifiers`` child table (see
``app_shared.models.competitor_identifiers`` and
``alembic/versions/a3f5c81d7e46_match_competitor_identifiers.py``).

**Classification is by resolution, never by shape.** For each match the
storefront product JSON is fetched (bounded, robots-compliant,
request-logged) and the legacy value is checked against that document's
actual ``variants[].id`` / ``variants[].barcode`` / ``variants[].sku``
and ``handle`` — ``scrape_core.adapters.variant_resolution.classify_identifier``.
A regex or a length test cannot tell a 13-digit EAN from a Shopify
variant id; guessing by shape is precisely the bug being fixed here. A
value that matches nothing stays ``UNKNOWN`` and is quarantined for
review rather than force-fitted into a type.

Writes, per match:

* one ``match_competitor_identifiers`` row with
  ``source=PRODUCT_JSON`` when the fetched document proved the type
  (``verified_at`` set, ``confidence=1.0``), or ``source=LEGACY_BACKFILL``
  when it did not (``UNKNOWN``, ``verified_at`` NULL);
* ``competitor_product_matches.canonical_variant_ref`` -> that row, but
  **only** when the resolution is deterministic (exactly one variant
  identified). An ambiguous or unresolved match keeps
  ``canonical_variant_ref = NULL``: "we do not know" is a real state and
  inventing a canonical identity is the bug this table exists to fix;
* a ``NEEDS_REVIEW`` row in the A6 ``match_audit_classifications``
  sidecar for every quarantined match (``NEEDS_REVIEW`` is a *match
  audit* state — the target status vocabulary is untouched).

The legacy column is **never written and never dropped**. It stays as the
rollback anchor and the audit trail for every typed row derived from it.

Outputs (all mode 0600):

* ``--out-csv`` — the full classification, one row per match;
* ``--review-csv`` — the quarantine: ambiguous / unresolved matches;
* ``--rollback-csv`` — ``(match_id, old competitor_variant_identifier,
  old canonical_variant_ref, the typed rows written)``, enough to undo an
  ``--apply`` without a database restore;
* ``--request-log-csv`` — every HTTP request issued, robots.txt included.

``--dry-run`` is the default and performs every read **including the live
fetches** (they are real, they count against the budget, and they are
logged) — only the database writes are skipped. ``--apply`` requires
``--backup-id``, recorded in the CSV headers for audit.

Idempotent: the writes are ``ON CONFLICT DO NOTHING`` against the
partial unique index ``uq_mci_match_type_value_current``
``(match_id, identifier_type, value) WHERE effective_to IS NULL``, so a
second ``--apply`` inserts nothing.

**Connection requirement — this is a cross-tenant maintenance pass.**
``competitor_product_matches`` and ``match_competitor_identifiers`` both
carry forced RLS, and this script deliberately has no single workspace to
set ``app.workspace_id`` to. It must therefore run on a ``BYPASSRLS``
(system) role — the same sanctioned seam the other cross-tenant passes
use (``run_refresh_pass``, the outbox drain, ``_scan_job_refs``).
Connected as an ordinary role with no ``app.workspace_id`` set, the RLS
predicate is fail-closed and every query returns **zero rows** — the run
reports "loaded 0 matches" rather than doing anything wrong, but it is
also doing nothing. ``--allow-empty`` is off by default, so this script turns that silent
no-op into a hard failure (exit 3) instead of a clean-looking run.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libs" / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "libs" / "scrape-core"))

from app_shared.enums import (  # noqa: E402
    CompetitorIdentifierSource,
    CompetitorIdentifierType,
)
from scrape_core.adapters.variant_resolution import (  # noqa: E402
    Resolved,
    TypedIdentifier,
    classify_identifier,
    identifiers_from_legacy_context,
    resolve_shopify_variant,
)

BACKFILL_VERSION = "1"
DEFAULT_DOMAIN = "stech.ink"

REQUEST_HARD_CAP = 2000
MAX_REQUESTS_PER_DOMAIN_PER_SECOND = 2
#: Default pacing. The ceiling is 2 req/domain/s, but the *default* is
#: deliberately gentler: a 2026-08-25 dry-run at the ceiling drew HTTP
#: 429 for 399 of 781 stech.ink product fetches. Being rate-limited is
#: the store telling you you are too fast; a backfill has no deadline.
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = 1.0
MIN_REQUEST_INTERVAL_FLOOR = 1.0 / MAX_REQUESTS_PER_DOMAIN_PER_SECOND
#: 429 handling: honor Retry-After when present, otherwise back off
#: exponentially. A 429 is never treated as "the product is absent" —
#: that conflation is the whole class of bug this task exists to fix.
RETRY_ON_429 = 3
RETRY_BACKOFF_SECONDS = (5.0, 15.0, 45.0)
USER_AGENT = (
    "crawmatic-identifier-backfill/1 (+https://crawmatic.com; EPA B4 identifier typing)"
)
FETCH_TIMEOUT_SECONDS = 15


# --------------------------------------------------------------------
# Pure core (no I/O — unit-testable with synthetic facts)
# --------------------------------------------------------------------


@dataclass(frozen=True)
class MatchRow:
    match_id: uuid.UUID
    competitor_url: str
    legacy_identifier: str | None
    legacy_sku: str | None
    canonical_variant_ref: uuid.UUID | None


@dataclass(frozen=True)
class BackfillDecision:
    """What this run concluded about one match, and why."""

    match_id: uuid.UUID
    identifier_type: CompetitorIdentifierType
    value: str | None
    source: CompetitorIdentifierSource
    verified: bool
    canonical: bool
    quarantined: bool
    resolution: str
    reason: str
    evidence: dict = field(default_factory=dict)


def decide(
    row: MatchRow, product_json: dict | None, *, fetch_note: str
) -> BackfillDecision:
    """Classify ``row``'s legacy identifier against its fetched product JSON.

    Never a regex, never a length: the type is whatever field of a real
    store document the value was found in, and ``UNKNOWN`` when it was
    found in none.
    """
    value = (row.legacy_identifier or "").strip() or None
    if value is None:
        return BackfillDecision(
            match_id=row.match_id,
            identifier_type=CompetitorIdentifierType.UNKNOWN,
            value=None,
            source=CompetitorIdentifierSource.LEGACY_BACKFILL,
            verified=False,
            canonical=False,
            quarantined=False,
            resolution="NO_LEGACY_IDENTIFIER",
            reason="match carries no legacy identifier — nothing to type",
        )

    if product_json is None:
        return BackfillDecision(
            match_id=row.match_id,
            identifier_type=CompetitorIdentifierType.UNKNOWN,
            value=value,
            source=CompetitorIdentifierSource.LEGACY_BACKFILL,
            verified=False,
            canonical=False,
            quarantined=True,
            resolution="NO_PRODUCT_JSON",
            reason=(
                f"could not obtain the product JSON ({fetch_note}) — typing without "
                "evidence would be the very guess this migration exists to remove"
            ),
        )

    classification = classify_identifier(value, product_json)
    resolution = resolve_shopify_variant(
        product_json,
        identifiers_from_legacy_context(value, row.legacy_sku),
    )
    resolved = isinstance(resolution, Resolved)
    typed = classification.identifier_type is not CompetitorIdentifierType.UNKNOWN

    return BackfillDecision(
        match_id=row.match_id,
        identifier_type=classification.identifier_type,
        value=value,
        source=(
            CompetitorIdentifierSource.PRODUCT_JSON
            if typed and not classification.ambiguous
            else CompetitorIdentifierSource.LEGACY_BACKFILL
        ),
        verified=typed and not classification.ambiguous,
        # Canonical only when the identity is deterministic: the value
        # typed cleanly AND the resolver picked exactly one variant.
        canonical=bool(typed and not classification.ambiguous and resolved),
        quarantined=bool(classification.ambiguous or not resolved),
        resolution=type(resolution).__name__,
        reason=getattr(resolution, "reason", "") or "",
        evidence={
            "matched_field": classification.matched_field,
            "matched_variant_id": classification.matched_variant_id,
            "ambiguous": classification.ambiguous,
            "handle": product_json.get("handle"),
            "variant_count": len(product_json.get("variants") or []),
            "fetch": fetch_note,
        },
    )


def typed_identifier_for(decision: BackfillDecision, *, now: datetime) -> TypedIdentifier | None:
    if decision.value is None:
        return None
    return TypedIdentifier(
        identifier_type=decision.identifier_type,
        value=decision.value,
        source=decision.source,
        confidence=1.0 if decision.verified else None,
        verified_at=now if decision.verified else None,
        effective_from=now,
    )


# --------------------------------------------------------------------
# Bounded, direct-only, robots-compliant fetch
# --------------------------------------------------------------------


@dataclass
class RequestLogEntry:
    timestamp: str
    url: str
    status: int | str
    bytes: int
    elapsed_ms: float
    error: str
    note: str


@dataclass
class LiveFetcher:
    """Budgeted fetcher with a hard request cap, per-domain rate limiting
    and robots.txt compliance. Every request — success, failure or
    robots-blocked — is logged, with no auth headers ever sent."""

    budget: int
    min_interval: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS
    log: list[RequestLogEntry] = field(default_factory=list)
    requests_made: int = 0
    rate_limited: int = 0
    _robots: dict[str, object] = field(default_factory=dict)
    _last_request_at: dict[str, float] = field(default_factory=dict)
    _retry_after: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.budget = min(self.budget, REQUEST_HARD_CAP)
        self.min_interval = max(self.min_interval, MIN_REQUEST_INTERVAL_FLOOR)

    @property
    def budget_remaining(self) -> int:
        return max(0, self.budget - self.requests_made)

    def _rate_limit(self, origin: str) -> None:
        previous = self._last_request_at.get(origin)
        if previous is not None:
            elapsed = time.monotonic() - previous
            if elapsed < self.min_interval:
                time.sleep(self.min_interval - elapsed)
        self._last_request_at[origin] = time.monotonic()

    def _get_with_backoff(self, url: str, note: str) -> tuple[int | None, bytes]:
        """``_raw_get`` plus 429 backoff. Every attempt is logged separately."""
        status, body = self._raw_get(url, note)
        attempt = 0
        while status == 429 and attempt < RETRY_ON_429:
            self.rate_limited += 1
            delay = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS) - 1)]
            retry_after = self._retry_after.pop(url, None)
            if retry_after is not None:
                delay = max(delay, retry_after)
            time.sleep(delay)
            attempt += 1
            status, body = self._raw_get(url, f"{note}-retry{attempt}")
        return status, body

    def _raw_get(self, url: str, note: str) -> tuple[int | None, bytes]:
        if self.requests_made >= self.budget:
            return None, b""
        origin = "{0}://{1}".format(*urlsplit(url)[:2])
        self._rate_limit(origin)
        self.requests_made += 1
        started = time.monotonic()
        status: int | None = None
        body = b""
        error = ""
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                status = response.status
                body = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = exc.read() or b""
            error = f"HTTPError {exc.code}"
            if exc.code == 429:
                try:
                    self._retry_after[url] = float(exc.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    pass
        except Exception as exc:  # noqa: BLE001 - every failure is logged, none is fatal
            error = type(exc).__name__
        self.log.append(
            RequestLogEntry(
                timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                url=url,
                status=status if status is not None else "",
                bytes=len(body),
                elapsed_ms=round((time.monotonic() - started) * 1000, 1),
                error=error,
                note=note,
            )
        )
        return status, body

    def _robots_allows(self, url: str) -> bool:
        origin = "{0}://{1}".format(*urlsplit(url)[:2])
        if origin not in self._robots:
            if self.requests_made >= self.budget:
                # Budget gone before robots could be checked: refuse, never
                # assume permission.
                self._robots[origin] = "BUDGET_EXHAUSTED"
            else:
                status, body = self._get_with_backoff(f"{origin}/robots.txt", "robots-check")
                if status == 200 and body:
                    parser = RobotFileParser()
                    parser.parse(body.decode("utf-8", "replace").splitlines())
                    self._robots[origin] = parser
                elif status in (401, 403):
                    self._robots[origin] = "DISALLOW_ALL"
                else:
                    self._robots[origin] = None  # no robots.txt -> conventional allow
        cached = self._robots[origin]
        if cached in ("BUDGET_EXHAUSTED", "DISALLOW_ALL"):
            return False
        if cached is None:
            return True
        return bool(cached.can_fetch(USER_AGENT, url))  # type: ignore[union-attr]

    def product_json(self, product_url: str) -> tuple[dict | None, str]:
        """Fetch the Shopify ``products/{handle}.js`` document for ``product_url``."""
        json_url = _shopify_json_url(product_url)
        if self.requests_made >= self.budget:
            return None, "budget_exhausted"
        if not self._robots_allows(json_url):
            return None, "robots_disallowed"
        status, body = self._get_with_backoff(json_url, "product-json")
        if status == 429:
            return None, "rate_limited_429"
        if status != 200 or not body:
            return None, f"status_{status}"
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "malformed_json"
        if not isinstance(document, dict):
            return None, "not_an_object"
        # Some storefronts wrap the product in {"product": {...}}.
        if "variants" not in document and isinstance(document.get("product"), dict):
            document = document["product"]
        return document, "ok"


def _shopify_json_url(product_url: str) -> str:
    parsed = urlsplit(product_url)
    path = parsed.path.rstrip("/")
    if path.endswith(".js"):
        return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))
    return urlunsplit((parsed.scheme, parsed.netloc, f"{path}.js", "", ""))


# --------------------------------------------------------------------
# Database
# --------------------------------------------------------------------


def _resolve_db_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    for env_var in ("IDENTIFIER_BACKFILL_DATABASE_URL", "MIGRATION_DATABASE_URL"):
        value = os.environ.get(env_var)
        if value:
            return value
    raise RuntimeError(
        "No database URL: pass --db-url or set IDENTIFIER_BACKFILL_DATABASE_URL / "
        "MIGRATION_DATABASE_URL."
    )


def _has_canonical_ref_column(session) -> bool:
    """Is migration ``a3f5c81d7e46`` applied to THIS database?

    A dry-run is explicitly meant to be runnable *before* the migration
    lands (that is how you find out what the backfill would do before
    committing to it), so the read degrades to "no canonical ref yet"
    instead of failing. ``--apply`` against an unmigrated database still
    fails loudly at the INSERT, which is the correct behaviour.
    """
    from sqlalchemy import text

    return bool(
        session.execute(
            text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'competitor_product_matches' "
                "AND column_name = 'canonical_variant_ref'"
            )
        ).first()
    )


def load_matches(session, *, domain: str, limit: int | None = None) -> list[MatchRow]:
    from sqlalchemy import text

    canonical_select = (
        "cpm.canonical_variant_ref"
        if _has_canonical_ref_column(session)
        else "NULL::uuid AS canonical_variant_ref"
    )
    sql = f"""
        SELECT cpm.id, cpm.competitor_url, cpm.competitor_variant_identifier,
               cpm.competitor_variant_sku, {canonical_select}
        FROM competitor_product_matches cpm
        JOIN competitors c ON c.id = cpm.competitor_id
        WHERE c.domain = :domain
        ORDER BY cpm.id
    """
    if limit is not None:
        sql += " LIMIT :limit"
    rows = session.execute(
        text(sql), {"domain": domain, **({"limit": limit} if limit is not None else {})}
    ).all()
    return [
        MatchRow(
            match_id=row.id,
            competitor_url=row.competitor_url or "",
            legacy_identifier=row.competitor_variant_identifier,
            legacy_sku=row.competitor_variant_sku,
            canonical_variant_ref=row.canonical_variant_ref,
        )
        for row in rows
    ]


def apply_backfill(
    session,
    decisions: list[tuple[MatchRow, BackfillDecision]],
    *,
    now: datetime,
) -> dict[str, int]:
    """Write the typed rows, the canonical refs and the NEEDS_REVIEW audit rows.

    Bulk, not per-row: over a networked connection a SELECT-then-INSERT
    loop across thousands of matches is minutes of pure latency. Every
    INSERT is ``ON CONFLICT DO NOTHING`` against the partial unique index,
    so re-running is a no-op rather than a duplicate.
    """
    from sqlalchemy import text

    from app_shared.ids import new_uuid7

    identifier_params = []
    canonical_params = []
    review_params = []

    for row, decision in decisions:
        identifier = typed_identifier_for(decision, now=now)
        if identifier is not None:
            identifier_id = new_uuid7()
            identifier_params.append(
                {
                    "id": identifier_id,
                    "match_id": row.match_id,
                    "identifier_type": str(identifier.identifier_type),
                    "value": identifier.value,
                    "source": str(identifier.source),
                    "verified_at": identifier.verified_at,
                    "confidence": identifier.confidence,
                    "effective_from": now,
                }
            )
            if decision.canonical:
                canonical_params.append(
                    {"match_id": row.match_id, "ref": identifier_id}
                )
        if decision.quarantined:
            review_params.append(
                {
                    "id": new_uuid7(),
                    "match_id": row.match_id,
                    "state": "NEEDS_REVIEW",
                    "classifier_version": f"b4-identifier-backfill-{BACKFILL_VERSION}",
                    "evidence": json.dumps(
                        {
                            "resolution": decision.resolution,
                            "reason": decision.reason,
                            "legacy_identifier": decision.value,
                            **decision.evidence,
                        },
                        default=str,
                    ),
                    "reviewer": None,
                    "effective_at": now,
                }
            )

    inserted = 0
    if identifier_params:
        session.execute(
            text(
                """
                INSERT INTO match_competitor_identifiers
                    (id, match_id, identifier_type, value, source, verified_at,
                     confidence, effective_from, effective_to)
                VALUES (:id, :match_id, :identifier_type, :value, :source,
                        :verified_at, :confidence, :effective_from, NULL)
                ON CONFLICT DO NOTHING
                """
            ),
            identifier_params,
        )
        inserted = len(identifier_params)

    canonical_set = 0
    if canonical_params:
        # Only set a ref that actually landed (ON CONFLICT DO NOTHING may
        # have skipped a re-run's row); the sub-select keeps the FK honest.
        session.execute(
            text(
                """
                UPDATE competitor_product_matches cpm
                SET canonical_variant_ref = :ref
                WHERE cpm.id = :match_id
                  AND EXISTS (SELECT 1 FROM match_competitor_identifiers m
                              WHERE m.id = :ref)
                """
            ),
            canonical_params,
        )
        canonical_set = len(canonical_params)

    reviewed = 0
    if review_params:
        match_ids = [p["match_id"] for p in review_params]
        # A6's sidecar is append-only with a single-current-row invariant:
        # supersede the previous current row, never mutate it.
        session.execute(
            text(
                """
                UPDATE match_audit_classifications
                SET superseded_at = :now
                WHERE superseded_at IS NULL AND match_id = ANY(:match_ids)
                """
            ),
            {"now": now, "match_ids": match_ids},
        )
        session.execute(
            text(
                """
                INSERT INTO match_audit_classifications
                    (id, match_id, state, classifier_version, evidence, reviewer,
                     effective_at, superseded_at)
                VALUES (:id, :match_id, :state, :classifier_version,
                        CAST(:evidence AS jsonb), :reviewer, :effective_at, NULL)
                """
            ),
            review_params,
        )
        reviewed = len(review_params)

    session.commit()
    return {
        "identifiers_inserted": inserted,
        "canonical_refs_set": canonical_set,
        "needs_review_rows": reviewed,
    }


# --------------------------------------------------------------------
# CSV output
# --------------------------------------------------------------------


def _chmod_0600(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _write_csv(path: Path, header: list[str], rows: list[list], meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([f"# {json.dumps(meta, default=str)}"])
        writer.writerow(header)
        writer.writerows(rows)
    _chmod_0600(path)


def write_outputs(
    decisions: list[tuple[MatchRow, BackfillDecision]],
    *,
    out_csv: Path,
    review_csv: Path,
    rollback_csv: Path,
    request_log_csv: Path,
    fetcher: LiveFetcher,
    meta: dict,
) -> dict[str, int]:
    _write_csv(
        out_csv,
        [
            "match_id",
            "competitor_url",
            "legacy_identifier",
            "identifier_type",
            "source",
            "verified",
            "canonical",
            "quarantined",
            "resolution",
            "reason",
            "evidence",
        ],
        [
            [
                str(d.match_id),
                row.competitor_url,
                d.value or "",
                str(d.identifier_type),
                str(d.source),
                d.verified,
                d.canonical,
                d.quarantined,
                d.resolution,
                d.reason,
                json.dumps(d.evidence, default=str, ensure_ascii=False),
            ]
            for row, d in decisions
        ],
        meta,
    )

    quarantined = [(row, d) for row, d in decisions if d.quarantined]
    _write_csv(
        review_csv,
        ["match_id", "competitor_url", "legacy_identifier", "resolution", "reason", "evidence"],
        [
            [
                str(d.match_id),
                row.competitor_url,
                d.value or "",
                d.resolution,
                d.reason,
                json.dumps(d.evidence, default=str, ensure_ascii=False),
            ]
            for row, d in quarantined
        ],
        meta,
    )

    # Enough to undo an --apply without a restore: the pre-change values
    # plus exactly what would be written.
    _write_csv(
        rollback_csv,
        [
            "match_id",
            "old_competitor_variant_identifier",
            "old_canonical_variant_ref",
            "new_identifier_type",
            "new_identifier_value",
            "new_identifier_source",
            "sets_canonical_variant_ref",
            "writes_needs_review_row",
            "undo_sql",
        ],
        [
            [
                str(d.match_id),
                row.legacy_identifier or "",
                str(row.canonical_variant_ref) if row.canonical_variant_ref else "",
                str(d.identifier_type),
                d.value or "",
                str(d.source),
                d.canonical,
                d.quarantined,
                (
                    "UPDATE competitor_product_matches SET canonical_variant_ref = "
                    + (
                        f"'{row.canonical_variant_ref}'"
                        if row.canonical_variant_ref
                        else "NULL"
                    )
                    + f" WHERE id = '{d.match_id}'; "
                    "DELETE FROM match_competitor_identifiers WHERE match_id = "
                    f"'{d.match_id}' AND effective_from = '{meta['effective_at']}';"
                ),
            ]
            for row, d in decisions
            if d.value is not None
        ],
        meta,
    )

    _write_csv(
        request_log_csv,
        ["timestamp", "url", "status", "bytes", "elapsed_ms", "error", "note"],
        [
            [e.timestamp, e.url, e.status, e.bytes, e.elapsed_ms, e.error, e.note]
            for e in fetcher.log
        ],
        meta,
    )
    return {"quarantined": len(quarantined), "requests": len(fetcher.log)}


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=None, help="SQLAlchemy DB URL (else env).")
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--limit", type=int, default=None, help="Cap matches processed.")
    parser.add_argument(
        "--fetch-budget",
        type=int,
        default=200,
        help=f"Max HTTP requests (hard cap {REQUEST_HARD_CAP}).",
    )
    parser.add_argument(
        "--min-interval",
        type=float,
        default=DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        help=(
            "Seconds between requests to one domain "
            f"(floor {MIN_REQUEST_INTERVAL_FLOOR}s = the 2 req/domain/s ceiling)."
        ),
    )
    parser.add_argument(
        "--product-json-dir",
        default=None,
        help=(
            "Directory of pre-captured <match_id>.json product documents "
            "(a rehearsal/offline replay: nothing is fetched for a match found here)."
        ),
    )
    parser.add_argument("--out-csv", default="/srv/crawmatic/evidence/b4-identifier-backfill.csv")
    parser.add_argument("--review-csv", default="/srv/crawmatic/evidence/b4-identifier-review.csv")
    parser.add_argument(
        "--rollback-csv", default="/srv/crawmatic/evidence/b4-identifier-rollback.csv"
    )
    parser.add_argument(
        "--request-log-csv", default="/srv/crawmatic/evidence/b4-identifier-request-log.csv"
    )
    parser.add_argument("--apply", action="store_true", help="Write to the database.")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help=(
            "Permit a run that loaded zero matches. Off by default: zero rows "
            "almost always means the session is RLS-filtered (not a BYPASSRLS "
            "role) rather than that the domain has no matches, and a silent "
            "no-op is the worst possible outcome for a backfill."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", default=True)
    parser.add_argument("--backup-id", default=None, help="Required with --apply.")
    return parser.parse_args(argv)


def _load_local_product(directory: Path, match_id: uuid.UUID) -> dict | None:
    path = directory / f"{match_id}.json"
    if not path.exists():
        path = directory / str(match_id) / "product.json"
    if not path.exists():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    if "variants" not in document and isinstance(document.get("product"), dict):
        document = document["product"]
    return document if isinstance(document, dict) else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.backup_id:
        print("migrate_stech_identifiers: --apply requires --backup-id", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(_resolve_db_url(args.db_url), pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    mode = "APPLY" if args.apply else "DRY-RUN"
    meta = {
        "script": "migrate_stech_identifiers",
        "backfill_version": BACKFILL_VERSION,
        "mode": mode,
        "domain": args.domain,
        "effective_at": now.isoformat(),
        "backup_id": args.backup_id or "-",
    }
    print(
        f"migrate_stech_identifiers mode={mode} domain={args.domain} "
        f"version={BACKFILL_VERSION} now={now.isoformat()} "
        f"fetch_budget={args.fetch_budget} backup_id={args.backup_id or '-'}"
    )

    with Session() as session:
        rows = load_matches(session, domain=args.domain, limit=args.limit)
    print(f"loaded {len(rows)} matches on {args.domain}")
    if not rows and not args.allow_empty:
        print(
            f"migrate_stech_identifiers: loaded 0 matches on {args.domain}. This is "
            "almost always an RLS-filtered session — connect with a BYPASSRLS "
            "(system) role, or pass --allow-empty if the domain really has none.",
            file=sys.stderr,
        )
        return 3

    local_dir = Path(args.product_json_dir) if args.product_json_dir else None
    fetcher = LiveFetcher(budget=args.fetch_budget, min_interval=args.min_interval)
    # One fetch per product URL, not per match: several matches routinely
    # point at the same competitor product.
    cache: dict[str, tuple[dict | None, str]] = {}
    decisions: list[tuple[MatchRow, BackfillDecision]] = []
    for row in rows:
        product: dict | None = None
        note = "no_url"
        if local_dir is not None:
            product = _load_local_product(local_dir, row.match_id)
            note = "local_fixture" if product is not None else "local_fixture_missing"
        if product is None and row.competitor_url:
            key = _shopify_json_url(row.competitor_url)
            if key not in cache:
                cache[key] = fetcher.product_json(row.competitor_url)
            product, note = cache[key]
        decisions.append((row, decide(row, product, fetch_note=note)))

    tally: dict[str, int] = {}
    for _, decision in decisions:
        tally[str(decision.identifier_type)] = tally.get(str(decision.identifier_type), 0) + 1
    resolutions: dict[str, int] = {}
    for _, decision in decisions:
        resolutions[decision.resolution] = resolutions.get(decision.resolution, 0) + 1

    print("identifier type distribution:")
    for identifier_type in CompetitorIdentifierType:
        print(f"  {identifier_type}: {tally.get(str(identifier_type), 0)}")
    print("resolution distribution:")
    for name, count in sorted(resolutions.items()):
        print(f"  {name}: {count}")
    print(f"canonical_variant_ref selectable: {sum(1 for _, d in decisions if d.canonical)}")
    print(f"quarantined for review: {sum(1 for _, d in decisions if d.quarantined)}")
    print(
        f"live fetches issued: {fetcher.requests_made} (budget {fetcher.budget}, "
        f"min_interval={fetcher.min_interval}s, 429 backoffs={fetcher.rate_limited})"
    )

    counts = write_outputs(
        decisions,
        out_csv=Path(args.out_csv),
        review_csv=Path(args.review_csv),
        rollback_csv=Path(args.rollback_csv),
        request_log_csv=Path(args.request_log_csv),
        fetcher=fetcher,
        meta=meta,
    )
    print(
        f"CSVs written (mode 0600): {args.out_csv}, {args.review_csv} "
        f"({counts['quarantined']} rows), {args.rollback_csv}, {args.request_log_csv}"
    )

    if args.apply:
        with Session() as session:
            written = apply_backfill(session, decisions, now=now)
        print(f"APPLY: {written}")
    else:
        print("DRY-RUN: no database write performed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
