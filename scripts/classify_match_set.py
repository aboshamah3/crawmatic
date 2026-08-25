#!/usr/bin/env python3
"""classify_match_set.py — EPA A6: freeze and classify the Mushtryati match set.

Every active ``competitor_product_matches`` row gets a current audit
classification (:class:`app_shared.enums.MatchClassificationState`:
``ACTIVE`` / ``CONFIRMED_DELISTED`` / ``INVALID_IDENTITY`` / ``UNKNOWN``),
written to the versioned ``match_audit_classifications`` sidecar (never a
column on ``competitor_product_matches`` — see
``app_shared.models.match_audit`` and the migration that creates it,
``alembic/versions/b8f3d61c9e02_match_audit_classifications_table.py``).
Future success-rate denominators exclude every non-``ACTIVE`` match.

Rule set (``CLASSIFIER_VERSION``, bump on any rule change so old
evidence is never silently reinterpreted):

1. **INVALID_IDENTITY** — a match on a domain in
   ``INVALID_IDENTITY_DOMAINS`` (S-Tech / ``stech.ink``, per the
   2026-08-24 production canary finding:
   ``PRODUCTION_READINESS_REPORT_2026-08-24.md`` line 255 — S-Tech
   returned valid HTTP 200 Shopify product JSON for sampled URLs, but 26
   of 30 canary targets carrying a ``competitor_variant_identifier``
   [a product handle or barcode the adapter misread as a Shopify variant
   ID] were classified ``NOT_LISTED``) with a non-null
   ``competitor_variant_identifier`` AND at least one ``NOT_LISTED``
   observation is a false-negative identity bug, not a real absence.
2. **CONFIRMED_DELISTED** — requires >= 2 *validated-absence* fetches at
   least 24h apart. "Validated absence" is operationalized as
   ``error_code = NOT_LISTED`` — per ``scrape-core``'s own adapter
   semantics (``libs/scrape-core/scrape_core/adapters/*.py``), this
   error code is emitted only for a store response that IS valid
   (correct market/locale, store reachable/online) where the
   product/variant JSON is specifically absent — never for a raw
   transport-level ``HTTP_404``/``HTTP_410``, which this classifier
   deliberately does NOT treat as validated absence (never a single
   404/410, per the task brief; a raw 404 conflates "not found" with
   "blocked/misconfigured/wrong locale"). A single validated-absence
   fetch is NOT enough (never emitted from one fetch) — it yields
   ``UNKNOWN`` with the evidence recorded and ``second_pass=true``,
   meant to be re-run after ``SECOND_PASS_AFTER`` once >= 24h has
   elapsed since the first fetch.
3. **ACTIVE** — a recent (<= ``--recent-days``), successful, comparable
   observation exists. Recorded as *provisional* in the evidence (a
   single observation, not a confirmed trend).
4. **UNKNOWN** — anything else (never observed, or only inconclusive/
   non-validated errors, or stale).

Data source priority (per the task's fetch policy): classify from
EXISTING data (``price_observations``) first; a bounded, direct
(never proxied), robots-compliant live fetch is attempted ONLY for
matches with **zero** observation history at all (``never_observed``)
when ``--fetch-budget`` > 0, capped at ``REQUEST_HARD_CAP`` total
requests for the whole run and <= ``MAX_REQUESTS_PER_DOMAIN_PER_SECOND``
per domain. Every request (robots.txt fetch included) is logged to the
request-log CSV for later manual reconciliation against provider usage.
A domain with no direct-access precedent in ``request_attempts`` history
(every attempt on that domain was proxied) is skipped — never a
proxied fetch, per the firewall.

``--dry-run`` (the default) performs every read (including any bounded
live fetches, which DO count against the request budget and DO get
logged even in dry-run — the fetch itself is real; only the DATABASE
WRITE is skipped) and prints/saves the classification distribution and
evidence without writing to ``match_audit_classifications``.
``--apply`` requires ``--backup-id`` (recorded in the evidence header
for audit) and performs the sidecar INSERTs — append-only: a fresh row
is always inserted, and the previous current row (``superseded_at IS
NULL``) for that ``match_id``, if any, gets ONLY its ``superseded_at``
column set — ``state``/``evidence`` on an existing row are never
mutated.
"""

from __future__ import annotations

import argparse
import csv
import os
import stat
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

CLASSIFIER_VERSION = "1"
DEFAULT_RECENT_DAYS = 14
CONFIRM_GAP = timedelta(hours=24)
INVALID_IDENTITY_DOMAINS: frozenset[str] = frozenset({"stech.ink"})
SECOND_PASS_AFTER = "2026-08-26"

REQUEST_HARD_CAP = 150
MAX_REQUESTS_PER_DOMAIN_PER_SECOND = 2
MIN_REQUEST_INTERVAL_SECONDS = 1.0 / MAX_REQUESTS_PER_DOMAIN_PER_SECOND
USER_AGENT = "crawmatic-match-classifier/1 (+https://crawmatic.com; EPA A6 audit)"
FETCH_TIMEOUT_SECONDS = 10


# --------------------------------------------------------------------
# Pure classification core (no I/O — unit-testable with synthetic facts)
# --------------------------------------------------------------------


@dataclass(frozen=True)
class MatchFacts:
    """Everything the classifier needs about one match, gathered from
    EITHER the database or a live fetch — the classifier itself never
    knows or cares which."""

    match_id: uuid.UUID
    competitor_id: uuid.UUID
    competitor_domain: str
    competitor_url: str
    competitor_variant_identifier: str | None
    match_status: str
    latest_observation_at: datetime | None
    latest_observation_success: bool | None
    latest_observation_comparable: bool | None
    latest_observation_error_code: str | None
    not_listed_at: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class ClassificationResult:
    state: str
    evidence: dict
    second_pass: bool = False


def classify_match(
    facts: MatchFacts,
    *,
    now: datetime,
    recent_days: int = DEFAULT_RECENT_DAYS,
    invalid_identity_domains: frozenset[str] = INVALID_IDENTITY_DOMAINS,
    confirm_gap: timedelta = CONFIRM_GAP,
) -> ClassificationResult:
    """Pure, deterministic classification — see module docstring for the rules."""
    if (
        facts.competitor_domain in invalid_identity_domains
        and facts.competitor_variant_identifier
        and facts.not_listed_at
    ):
        sorted_ts = sorted(facts.not_listed_at)
        return ClassificationResult(
            state="INVALID_IDENTITY",
            evidence={
                "reason": "handle_or_barcode_identifier_misread_as_variant_id",
                "competitor_domain": facts.competitor_domain,
                "competitor_variant_identifier": facts.competitor_variant_identifier,
                "not_listed_observation_count": len(sorted_ts),
                "not_listed_at": [ts.isoformat() for ts in sorted_ts],
                "classifier_version": CLASSIFIER_VERSION,
                "reference": (
                    "PRODUCTION_READINESS_REPORT_2026-08-24.md: S-Tech false "
                    "NOT_LISTED finding (26/30 canary targets)"
                ),
            },
        )

    if facts.not_listed_at:
        sorted_ts = sorted(facts.not_listed_at)
        first, last = sorted_ts[0], sorted_ts[-1]
        span = last - first
        if len(sorted_ts) >= 2 and span >= confirm_gap:
            return ClassificationResult(
                state="CONFIRMED_DELISTED",
                evidence={
                    "reason": "repeated_validated_absence",
                    "validated_absence_fetch_count": len(sorted_ts),
                    "first_validated_absence_at": first.isoformat(),
                    "last_validated_absence_at": last.isoformat(),
                    "span_hours": round(span.total_seconds() / 3600.0, 2),
                    "classifier_version": CLASSIFIER_VERSION,
                },
            )
        return ClassificationResult(
            state="UNKNOWN",
            evidence={
                "reason": "single_validated_absence_fetch_pending_24h_second_look",
                "validated_absence_fetch_count": len(sorted_ts),
                "first_validated_absence_at": first.isoformat(),
                "last_validated_absence_at": last.isoformat(),
                "second_pass_after": SECOND_PASS_AFTER,
                "classifier_version": CLASSIFIER_VERSION,
            },
            second_pass=True,
        )

    if (
        facts.latest_observation_at is not None
        and facts.latest_observation_success
        and facts.latest_observation_comparable
        and (now - facts.latest_observation_at) <= timedelta(days=recent_days)
    ):
        return ClassificationResult(
            state="ACTIVE",
            evidence={
                "reason": "recent_successful_comparable_observation",
                "provisional": True,
                "last_success_at": facts.latest_observation_at.isoformat(),
                "recent_days_window": recent_days,
                "classifier_version": CLASSIFIER_VERSION,
            },
        )

    return ClassificationResult(
        state="UNKNOWN",
        evidence={
            "reason": (
                "never_observed"
                if facts.latest_observation_at is None
                else "no_decisive_recent_signal"
            ),
            "latest_observation_at": (
                facts.latest_observation_at.isoformat()
                if facts.latest_observation_at
                else None
            ),
            "latest_observation_success": facts.latest_observation_success,
            "latest_observation_comparable": facts.latest_observation_comparable,
            "latest_observation_error_code": facts.latest_observation_error_code,
            "classifier_version": CLASSIFIER_VERSION,
        },
    )


# --------------------------------------------------------------------
# Bounded, direct-only, robots-compliant live fetch (fallback only)
# --------------------------------------------------------------------


@dataclass
class RequestLogEntry:
    timestamp: str
    url: str
    status: int | None
    bytes: int
    note: str = ""


@dataclass
class LiveFetcher:
    """Direct-only (never proxied) HTTP fetcher with a hard request cap,
    per-domain rate limiting, and robots.txt compliance.

    Every call to :meth:`fetch` — success, failure, or robots-blocked —
    appends exactly one :class:`RequestLogEntry`, except the case where
    the budget is already exhausted (no request is made at all, nothing
    logged) or robots.txt itself needed a fetch (that fetch is logged as
    its own entry, in addition to the target URL's entry).
    """

    budget: int = REQUEST_HARD_CAP
    requests_made: int = 0
    log: list[RequestLogEntry] = field(default_factory=list)
    _robots_cache: dict[str, RobotFileParser | None] = field(default_factory=dict)
    _last_request_at: dict[str, float] = field(default_factory=dict)
    _opener: urllib.request.OpenerDirector = field(init=False)

    def __post_init__(self) -> None:
        # Explicitly disable any environment proxy (http_proxy/https_proxy/
        # ALL_PROXY) — direct fetches only, per the firewall.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def budget_remaining(self) -> int:
        return max(0, self.budget - self.requests_made)

    def _rate_limit(self, origin: str) -> None:
        last = self._last_request_at.get(origin)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < MIN_REQUEST_INTERVAL_SECONDS:
                time.sleep(MIN_REQUEST_INTERVAL_SECONDS - elapsed)
        self._last_request_at[origin] = time.monotonic()

    def _raw_get(self, url: str, note: str) -> tuple[int | None, bytes]:
        """One logged HTTP GET. Returns (status, body_bytes); never raises."""
        if self.requests_made >= self.budget:
            return None, b""
        self.requests_made += 1
        status: int | None = None
        body: bytes = b""
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with self._opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                status = response.status
                body = response.read(200_000)
        except urllib.error.HTTPError as exc:
            status = exc.code
        except Exception as exc:  # noqa: BLE001 - best-effort, always logged
            note = f"{note} error={exc.__class__.__name__}"
        self.log.append(
            RequestLogEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                url=url,
                status=status,
                bytes=len(body),
                note=note,
            )
        )
        return status, body

    def _robots_allows(self, url: str) -> bool:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots_cache:
            if self.requests_made >= self.budget:
                # Budget exhausted before we could even check robots —
                # fail closed (treat as disallowed, never fetch blind).
                self._robots_cache[origin] = "BUDGET_EXHAUSTED"  # type: ignore[assignment]
            else:
                self._rate_limit(origin)
                status, body = self._raw_get(f"{origin}/robots.txt", note="robots.txt")
                parser: RobotFileParser | None
                if status is not None and 200 <= status < 300:
                    try:
                        parser = RobotFileParser()
                        parser.parse(body.decode("utf-8", errors="replace").splitlines())
                    except Exception:  # noqa: BLE001 - fail open per convention
                        parser = None
                else:
                    parser = None  # no robots.txt -> conventional "allow"
                self._robots_cache[origin] = parser
        cached = self._robots_cache[origin]
        if cached == "BUDGET_EXHAUSTED":
            return False
        if cached is None:
            return True
        return cached.can_fetch(USER_AGENT, url)  # type: ignore[union-attr]

    def fetch(self, url: str) -> tuple[int | None, int, str]:
        """Fetch ``url`` if budget/robots allow. Returns (status, bytes, outcome_note)."""
        if self.requests_made >= self.budget:
            return None, 0, "budget_exhausted"
        if not self._robots_allows(url):
            return None, 0, "robots_disallowed"
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        self._rate_limit(origin)
        status, body = self._raw_get(url, note="target_fetch")
        return status, len(body), "fetched"


def domain_has_direct_precedent(domains_with_direct_success: set[str], domain: str) -> bool:
    """True if history shows at least one successful DIRECT (non-proxy) fetch
    on this domain — the bounded-fetch fallback never attempts a domain that
    has only ever been reached through a proxy (never spend proxy budget,
    and never guess that direct access will work where it never has)."""
    return domain in domains_with_direct_success


def enrich_never_observed_via_fetch(
    candidates: list[MatchFacts],
    *,
    fetcher: LiveFetcher,
    domains_with_direct_success: set[str],
    now: datetime,
) -> dict[uuid.UUID, MatchFacts]:
    """Attempt a bounded, direct, robots-compliant fetch for ``never_observed``
    matches only. Returns updated facts for matches actually fetched with a
    usable outcome; matches left alone (budget exhausted, proxy-only domain,
    robots-blocked, or an inconclusive fetch) are simply absent from the
    result and keep their original (never_observed) classification.
    """
    updated: dict[uuid.UUID, MatchFacts] = {}
    for facts in candidates:
        if fetcher.budget_remaining() <= 0:
            break
        if not facts.competitor_url.startswith(("http://", "https://")):
            continue
        if not domain_has_direct_precedent(domains_with_direct_success, facts.competitor_domain):
            # Never fetch a domain this workspace has only ever reached via
            # proxy — no proxy spend from this audit script (firewall).
            continue
        status, body_len, outcome = fetcher.fetch(facts.competitor_url)
        if outcome != "fetched" or status is None:
            continue
        if 200 <= status < 300 and body_len > 200:
            updated[facts.match_id] = MatchFacts(
                **{
                    **facts.__dict__,
                    "latest_observation_at": now,
                    "latest_observation_success": True,
                    "latest_observation_comparable": True,
                    "latest_observation_error_code": None,
                }
            )
        elif status in (404, 410):
            # A definite HTTP response (proving the store is reachable) at
            # the exact stored competitor_url (proving market/locale, since
            # that URL is the match's own configured target) that says the
            # resource is gone -- one validated-absence fetch. Never enough
            # alone (see classify_match).
            updated[facts.match_id] = MatchFacts(
                **{
                    **facts.__dict__,
                    "not_listed_at": facts.not_listed_at + (now,),
                }
            )
        # Any other status (403/429/5xx/etc.) is inconclusive -- leave as is.
    return updated


# --------------------------------------------------------------------
# Database glue (kept thin and separate from the pure classification core)
# --------------------------------------------------------------------


def _resolve_db_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    for env_var in ("MATCH_AUDIT_DATABASE_URL", "MIGRATION_DATABASE_URL"):
        value = os.environ.get(env_var)
        if value:
            return value
    raise RuntimeError(
        "No database URL: pass --db-url or set MATCH_AUDIT_DATABASE_URL / "
        "MIGRATION_DATABASE_URL."
    )


def load_match_facts(session, *, statuses: tuple[str, ...] = ("ACTIVE",)) -> list[MatchFacts]:
    """Load every match in ``statuses`` with its latest observation and
    NOT_LISTED history, from the real production schema. One round trip
    per query (three total), not one query per match."""
    from sqlalchemy import text

    matches_rows = session.execute(
        text(
            """
            SELECT cpm.id, cpm.competitor_id, c.domain, cpm.competitor_url,
                   cpm.competitor_variant_identifier, cpm.status
            FROM competitor_product_matches cpm
            JOIN competitors c ON c.id = cpm.competitor_id
            WHERE cpm.status = ANY(:statuses)
            """
        ),
        {"statuses": list(statuses)},
    ).all()

    latest_rows = session.execute(
        text(
            """
            SELECT DISTINCT ON (match_id)
                   match_id, scraped_at, success, comparable, error_code
            FROM price_observations
            ORDER BY match_id, scraped_at DESC
            """
        )
    ).all()
    latest_by_match = {row.match_id: row for row in latest_rows}

    not_listed_rows = session.execute(
        text(
            "SELECT match_id, scraped_at FROM price_observations WHERE error_code = 'NOT_LISTED'"
        )
    ).all()
    not_listed_by_match: dict[uuid.UUID, list[datetime]] = {}
    for row in not_listed_rows:
        not_listed_by_match.setdefault(row.match_id, []).append(row.scraped_at)

    facts: list[MatchFacts] = []
    for row in matches_rows:
        latest = latest_by_match.get(row.id)
        facts.append(
            MatchFacts(
                match_id=row.id,
                competitor_id=row.competitor_id,
                competitor_domain=row.domain,
                competitor_url=row.competitor_url or "",
                competitor_variant_identifier=row.competitor_variant_identifier,
                match_status=row.status,
                latest_observation_at=latest.scraped_at if latest else None,
                latest_observation_success=latest.success if latest else None,
                latest_observation_comparable=latest.comparable if latest else None,
                latest_observation_error_code=latest.error_code if latest else None,
                not_listed_at=tuple(not_listed_by_match.get(row.id, ())),
            )
        )
    return facts


def load_domains_with_direct_success(session) -> set[str]:
    from sqlalchemy import text

    rows = session.execute(
        text(
            """
            SELECT DISTINCT split_part(split_part(ra.url, '://', 2), '/', 1) AS host
            FROM request_attempts ra
            WHERE ra.success = true
              AND ra.access_method IN ('DIRECT_HTTP', 'DIRECT_HTTP_RETRY')
            """
        )
    ).all()
    return {row.host for row in rows if row.host}


# --------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------


def _chmod_0600(path: Path) -> None:
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def write_evidence_csv(
    path: Path,
    rows: Iterable[tuple[MatchFacts, ClassificationResult]],
    *,
    run_meta: dict,
) -> int:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(
            [
                "match_id",
                "competitor_id",
                "competitor_domain",
                "state",
                "classifier_version",
                "second_pass",
                "second_pass_after",
                "evidence_json",
                "effective_at",
            ]
        )
        for facts, result in rows:
            writer.writerow(
                [
                    str(facts.match_id),
                    str(facts.competitor_id),
                    facts.competitor_domain,
                    result.state,
                    CLASSIFIER_VERSION,
                    "true" if result.second_pass else "false",
                    SECOND_PASS_AFTER if result.second_pass else "",
                    json.dumps(result.evidence, sort_keys=True),
                    run_meta["effective_at"],
                ]
            )
            count += 1
    _chmod_0600(path)
    return count


def write_request_log_csv(path: Path, entries: list[RequestLogEntry]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["timestamp", "url", "status", "bytes", "note"])
        for entry in entries:
            writer.writerow([entry.timestamp, entry.url, entry.status, entry.bytes, entry.note])
    _chmod_0600(path)


def apply_classifications(
    session, rows: list[tuple[MatchFacts, ClassificationResult]], *, effective_at: datetime
) -> tuple[int, int]:
    """Append-only sidecar write: insert a fresh row per match; supersede
    (set superseded_at only) the previous current row, if any. Returns
    (inserted_count, superseded_count).

    Bulk, not per-row: over a networked (Railway proxy) connection, one
    round trip per match (a SELECT-then-INSERT loop across ~9k matches)
    is minutes of pure network latency for no reason — this does the
    "who's currently current" read as ONE query, the supersede as ONE
    batched UPDATE, and the fresh rows as ONE executemany-batched INSERT
    (psycopg3 pipelines a list-of-param-dicts `session.execute`
    automatically), so the whole apply is a handful of round trips
    regardless of match count.
    """
    import json

    from sqlalchemy import text

    from app_shared.ids import new_uuid7

    match_ids = [facts.match_id for facts, _ in rows]

    existing_rows = session.execute(
        text(
            "SELECT match_id FROM match_audit_classifications "
            "WHERE superseded_at IS NULL AND match_id = ANY(:match_ids)"
        ),
        {"match_ids": match_ids},
    ).all()
    to_supersede = [row.match_id for row in existing_rows]
    superseded = 0
    if to_supersede:
        result = session.execute(
            text(
                "UPDATE match_audit_classifications SET superseded_at = :now "
                "WHERE superseded_at IS NULL AND match_id = ANY(:match_ids)"
            ),
            {"now": effective_at, "match_ids": to_supersede},
        )
        superseded = result.rowcount or 0

    insert_params = [
        {
            "id": new_uuid7(),
            "match_id": facts.match_id,
            "state": result.state,
            "classifier_version": CLASSIFIER_VERSION,
            "evidence": json.dumps(result.evidence),
            "reviewer": None,
            "effective_at": effective_at,
        }
        for facts, result in rows
    ]
    session.execute(
        text(
            """
            INSERT INTO match_audit_classifications
                (id, match_id, state, classifier_version, evidence, reviewer,
                 effective_at, superseded_at)
            VALUES
                (:id, :match_id, :state, :classifier_version,
                 CAST(:evidence AS jsonb), :reviewer, :effective_at, NULL)
            """
        ),
        insert_params,
    )
    inserted = len(insert_params)
    session.commit()
    return inserted, superseded


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-url", default=None, help="SQLAlchemy DB URL (else env).")
    parser.add_argument(
        "--apply", action="store_true", help="Write to match_audit_classifications."
    )
    parser.add_argument(
        "--backup-id",
        default=None,
        help="Required with --apply: the pg_dump backup id this run is authorized against.",
    )
    parser.add_argument(
        "--evidence-csv",
        default="/srv/crawmatic/evidence/match-classification-2026-08-25.csv",
    )
    parser.add_argument(
        "--request-log-csv",
        default="/srv/crawmatic/evidence/match-classification-2026-08-25-request-log.csv",
    )
    parser.add_argument("--recent-days", type=int, default=DEFAULT_RECENT_DAYS)
    parser.add_argument(
        "--fetch-budget",
        type=int,
        default=REQUEST_HARD_CAP,
        help=f"Live-fetch request budget, hard-capped at {REQUEST_HARD_CAP} regardless.",
    )
    parser.add_argument(
        "--no-fetch", action="store_true", help="Classify from existing data only, no live fetch."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.apply and not args.backup_id:
        print("classify_match_set: --apply requires --backup-id", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_url = _resolve_db_url(args.db_url)
    engine = create_engine(db_url, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    now = datetime.now(timezone.utc)
    fetch_budget = min(args.fetch_budget, REQUEST_HARD_CAP)
    mode = "APPLY" if args.apply else "DRY-RUN"
    print(
        f"classify_match_set mode={mode} classifier_version={CLASSIFIER_VERSION} "
        f"now={now.isoformat()} recent_days={args.recent_days} "
        f"fetch_budget={fetch_budget} backup_id={args.backup_id or '-'}"
    )

    with Session() as session:
        facts_list = load_match_facts(session)
        print(f"loaded {len(facts_list)} ACTIVE matches")

        fetcher = LiveFetcher(budget=fetch_budget)
        if not args.no_fetch and fetch_budget > 0:
            never_observed = [f for f in facts_list if f.latest_observation_at is None]
            domains_with_direct = load_domains_with_direct_success(session)
            print(
                f"never_observed candidates={len(never_observed)} "
                f"domains_with_direct_precedent={len(domains_with_direct)}"
            )
            updated = enrich_never_observed_via_fetch(
                never_observed,
                fetcher=fetcher,
                domains_with_direct_success=domains_with_direct,
                now=now,
            )
            if updated:
                facts_list = [updated.get(f.match_id, f) for f in facts_list]
            print(
                f"live fetches issued={fetcher.requests_made} "
                f"facts_enriched={len(updated)}"
            )

        results = [
            (facts, classify_match(facts, now=now, recent_days=args.recent_days))
            for facts in facts_list
        ]

    distribution: dict[str, int] = {}
    second_pass_count = 0
    for _, result in results:
        distribution[result.state] = distribution.get(result.state, 0) + 1
        if result.second_pass:
            second_pass_count += 1

    print("classification distribution:")
    for state in ("ACTIVE", "CONFIRMED_DELISTED", "INVALID_IDENTITY", "UNKNOWN"):
        print(f"  {state}: {distribution.get(state, 0)}")
    print(f"  second_pass_after_{SECOND_PASS_AFTER}: {second_pass_count}")

    evidence_path = Path(args.evidence_csv)
    written = write_evidence_csv(
        evidence_path, results, run_meta={"effective_at": now.isoformat()}
    )
    print(f"evidence CSV written: {evidence_path} ({written} rows, mode 0600)")

    request_log_path = Path(args.request_log_csv)
    write_request_log_csv(request_log_path, fetcher.log)
    print(
        f"request log CSV written: {request_log_path} "
        f"({len(fetcher.log)} requests, mode 0600)"
    )

    if args.apply:
        with Session() as session:
            inserted, superseded = apply_classifications(session, results, effective_at=now)
        print(f"APPLY: inserted={inserted} superseded={superseded}")
    else:
        print("DRY-RUN: no database write performed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
