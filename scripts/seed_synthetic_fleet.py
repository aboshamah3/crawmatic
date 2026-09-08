#!/usr/bin/env python3
"""seed_synthetic_fleet.py — the 100 x 5,000 synthetic fleet (EPA D1, F22, audit §10/§13).

Writes the workload the controlled-origin fleet test offers: 100
workspaces x 5,000 products x the pilot's **1.185 matches/product**
(audit §10 — 3,690 products carried 4,372 matches), every match URL
pointing at ``apps/fixture-origin`` rather than at any real retailer,
plus one daily refresh rule per workspace with start times **jittered
14.4 minutes apart** (audit §10: "roughly one store's daily workload
introduced every 14.4 minutes, rather than all 100 starting at
midnight").

At the default size that is 100 workspaces, 500,000 products, 500,000
variants, **592,500 matches** and 100 refresh rules. This is a large,
destructive-by-volume write. Everything below is built around making it
impossible to aim at production by accident.

THE GUARD (why it is an allowlist, not a denylist)
--------------------------------------------------
:func:`require_staging` refuses unless ALL of:

1. ``--i-know-this-is-staging`` was passed. No default, no prompt, no
   environment variable that can supply it — it has to be typed.
2. ``--database-url`` was passed explicitly. This script NEVER reads
   ``$DATABASE_URL``, ``$MIGRATION_DATABASE_URL`` or
   ``app_shared.config.get_settings()``: an operator whose shell already
   carries a production credential must not be able to seed 1.6 million
   rows into it by omitting a flag.
3. The URL's host **matches an allowlist** — it contains ``staging`` (or
   ``fixture``/``fleet-test``), or it is a loopback/container-local host
   (``localhost``, ``127.x``, ``::1``, ``postgres``, ``pgbouncer``). A
   denylist of production hostnames would fail open the day production
   moves; an allowlist fails closed, and the failure message tells the
   operator exactly which flag to add if they really are on a
   differently-named staging host (``--host-allowlist-token``).
4. ``RAILWAY_ENVIRONMENT_NAME`` (read, never printed) does not name a
   production environment.

This mirrors ``tests/load/fault_injection/_common.py`` (EPA D2)
deliberately: same posture, same refusal-not-warning behaviour, same
"never falls back to ambient config" rule. It is a separate
implementation only because this file is a ``scripts/`` entry point and
that one is a test-package helper; the two guards are checked against
each other in ``tests/unit/test_seed_synthetic_fleet_shape.py``.

WHY THE URLS MUST BE THE FIXTURE ORIGIN'S *PUBLIC* DOMAIN
----------------------------------------------------------
``app_shared.url_safety.validate_competitor_url`` (the save-time SSRF
control, F01) rejects ``*.railway.internal``, ``localhost`` and every
private IP literal. Match rows carrying such a URL would either be
rejected here or, worse, be written by a path that skipped validation
and then behave differently from every real match. So the seeder
validates every URL it writes with the production validator, and the
staging runbook points ``--origin-base-url`` at the fixture service's
**public** Railway domain. That is safe precisely because the fixture
origin holds no data, has no credential and reaches nothing
(``apps/fixture-origin/app/main.py``).

The single exception is ``--allow-loopback-origin``, which widens the
validator to loopback addresses **only** (127.0.0.0/8 and ::1 — never a
private LAN range, never a metadata address, never a hostname). It
exists for the local smoke in ``docs/ops/FLEET_TEST_2026-09.md`` where
the fixture origin runs on the same box, and it cannot be used to reach
anything an SSRF would care about.

DETERMINISM
-----------
Same arguments in, byte-identical fleet out: which products carry the
second match, which SKU each match points at, and each workspace's
start offset are all derived from ``blake2b`` over stable strings, never
from ``random`` or the clock. Two runs of the fleet test therefore offer
the *same* workload, which is the only way the before/after numbers mean
anything. Row ids are UUIDv7 (time-ordered) and are the one thing that
legitimately differs between runs.

Also: one ``domain_rules`` row is written for the fixture origin's host
(``--fleet-concurrency`` / ``--fleet-rate-per-minute``). Without it the
whole 100-store fleet serialises behind ``FLEET_HOST_CONCURRENCY_DEFAULT``
= 6 concurrent requests against the single fixture host, and the test
would measure that one number instead of the platform.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_MATCHES_PER_PRODUCT",
    "DEFAULT_PRODUCTS_PER_STORE",
    "DEFAULT_SPACING_SECONDS",
    "DEFAULT_STORES",
    "FleetPlan",
    "StagingRefusal",
    "build_parser",
    "build_plan",
    "extra_match_product_indexes",
    "main",
    "match_urls_for_product",
    "require_staging",
    "store_slug",
    "validate_origin_base_url",
    "workspace_start_offsets",
]

#: Audit §10: 100 equally sized daily stores.
DEFAULT_STORES = 100
#: Audit §10: 500,000 products across the fleet.
DEFAULT_PRODUCTS_PER_STORE = 5_000
#: Audit §10: the pilot's 4,372 matches / 3,690 products.
DEFAULT_MATCHES_PER_PRODUCT = 1.185
#: Audit §10: 24 h / 100 stores = 14.4 min = 864 s between store starts.
DEFAULT_SPACING_SECONDS = 864
#: One refresh per day per workspace.
DAILY_INTERVAL_MINUTES = 1440

#: Fleet-wide host limits written for the fixture origin's domain.
#: Audit §10 puts the offered load at 30.85 physical attempts/second
#: over an 8-hour window at 1.5 attempts/check, i.e. ~1,851/minute. The
#: defaults below sit roughly 2x above that so the HOST cap is not the
#: binding constraint — the point of the run is to find the platform's
#: ceiling, not the fixture's.
DEFAULT_FLEET_CONCURRENCY = 240
DEFAULT_FLEET_RATE_PER_MINUTE = 4_000

#: Substrings that make a database host acceptable (case-insensitive).
STAGING_HOST_TOKENS: tuple[str, ...] = ("staging", "fixture", "fleet-test")
#: Container/loopback hosts that are acceptable without a token.
LOCAL_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "::1", "postgres", "pgbouncer"}
)
#: ``RAILWAY_ENVIRONMENT_NAME`` values that refuse outright.
PRODUCTION_ENVIRONMENT_NAMES: frozenset[str] = frozenset({"production", "prod"})

#: Chunk size for the executemany inserts. Large enough that the round
#: trips disappear, small enough that one failed chunk is a small redo
#: and peak client memory stays flat regardless of fleet size.
INSERT_CHUNK = 5_000


class StagingRefusal(RuntimeError):
    """Raised for every refusal reason. ``main()`` prints it and exits 2."""


# --------------------------------------------------------------------------
# Deterministic derivation (pure; no I/O, no clock, no `random`)
# --------------------------------------------------------------------------


def _digest(*parts: str) -> bytes:
    return hashlib.blake2b("\x1f".join(parts).encode("utf-8"), digest_size=16).digest()


def _hash_fraction(*parts: str) -> float:
    """Stable value in ``[0, 1)``. ``blake2b``, not ``hash()``: the
    builtin is per-process randomised and would make the fleet differ
    between the seeder run and any later verification."""
    return int.from_bytes(_digest(*parts)[:8], "big") / float(1 << 64)


def store_slug(index: int) -> str:
    """``0 -> "fleet-000"`` — the same id ``apps/fixture-origin`` uses."""
    return f"fleet-{index:03d}"


def _sku(number: int) -> str:
    return f"p{number:06d}"


def build_plan(
    *,
    stores: int,
    products_per_store: int,
    matches_per_product: float,
) -> "FleetPlan":
    """Resolve the counts, rounding matches ONCE at the store level.

    Rounding per store (not per product, and not over the whole fleet)
    keeps every store identical in size — the audit's "100 equally sized
    daily stores" — and makes the fleet total exactly
    ``stores * round(products_per_store * matches_per_product)``.
    """
    if stores < 1:
        raise ValueError("stores must be >= 1")
    if products_per_store < 1:
        raise ValueError("products_per_store must be >= 1")
    if matches_per_product < 1.0:
        raise ValueError(
            "matches_per_product must be >= 1.0 (every product carries at least one match)"
        )
    matches_per_store = round(products_per_store * matches_per_product)
    extra = matches_per_store - products_per_store
    if extra > products_per_store:
        raise ValueError(
            "matches_per_product > 2.0 is not supported by this seeder: a product "
            "would need a third distinct competitor URL, which the fixture origin "
            "models but this deterministic layout does not."
        )
    return FleetPlan(
        stores=stores,
        products_per_store=products_per_store,
        matches_per_product=matches_per_product,
        matches_per_store=matches_per_store,
        extra_matches_per_store=extra,
    )


@dataclass(frozen=True)
class FleetPlan:
    """The resolved shape of one seeding run."""

    stores: int
    products_per_store: int
    matches_per_product: float
    matches_per_store: int
    extra_matches_per_store: int

    @property
    def total_products(self) -> int:
        return self.stores * self.products_per_store

    @property
    def total_variants(self) -> int:
        # One variant per product: the audit counts PRODUCTS, and a
        # variant fan-out would silently multiply the offered load
        # without any line in the plan asking for it.
        return self.total_products

    @property
    def total_matches(self) -> int:
        return self.stores * self.matches_per_store

    def as_dict(self) -> dict[str, Any]:
        return {
            "stores": self.stores,
            "products_per_store": self.products_per_store,
            "matches_per_product": self.matches_per_product,
            "matches_per_store": self.matches_per_store,
            "extra_matches_per_store": self.extra_matches_per_store,
            "total_products": self.total_products,
            "total_variants": self.total_variants,
            "total_matches": self.total_matches,
        }


def extra_match_product_indexes(store: str, plan: FleetPlan) -> frozenset[int]:
    """Which of the store's products carry a SECOND match.

    Chosen as the ``extra_matches_per_store`` products with the lowest
    ``blake2b(store, index)`` draw — deterministic, stable under a
    change to any OTHER store, and different per store (so the fleet is
    not 100 identical catalogues, which would make one hot key look like
    a fleet-wide effect).
    """
    if plan.extra_matches_per_store <= 0:
        return frozenset()
    ranked = sorted(
        range(plan.products_per_store),
        key=lambda index: (_hash_fraction("extra", store, str(index)), index),
    )
    return frozenset(ranked[: plan.extra_matches_per_store])


def match_urls_for_product(
    base_url: str, store: str, product_index: int, *, extra: bool
) -> list[str]:
    """The competitor URLs for one product.

    Primary: the HTML page ``/p/{store}/{sku}``. A product carrying a
    second match gets the JSON endpoint variant for a DIFFERENT SKU
    (``/api/p/...``), so the fleet exercises both extraction paths and
    the two matches on one variant have distinct
    ``normalized_competitor_url`` values — which the match unique key
    ``(workspace_id, product_variant_id, competitor_id,
    normalized_competitor_url)`` requires.
    """
    base = base_url.rstrip("/")
    sku = _sku(product_index + 1)
    urls = [f"{base}/p/{store}/{sku}"]
    if extra:
        # A well-separated second SKU: same deterministic space, no
        # collision with any primary SKU in a store of <= 250,000
        # products.
        secondary = _sku(500_000 - product_index)
        urls.append(f"{base}/api/p/{store}/{secondary}")
    return urls


def workspace_start_offsets(
    stores: int, spacing_seconds: int = DEFAULT_SPACING_SECONDS
) -> list[int]:
    """Per-store start offset in seconds: ``[0, 864, 1728, ...]``.

    Audit §10's 14.4-minute stagger, expressed in seconds because 14.4
    minutes is not an integer number of minutes and a 5-field cron
    expression could not say it. See :func:`refresh_rule_row` for why
    this seeder uses ``interval_minutes`` + a jittered ``next_run_at``
    rather than ``cron_expression``.
    """
    return [index * spacing_seconds for index in range(stores)]


# --------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------


def _host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").strip().lower()
    except ValueError:
        return ""


def require_staging(
    args: argparse.Namespace, *, env: dict[str, str] | None = None
) -> None:
    """Enforce the four-part guard in the module docstring.

    Raises :class:`StagingRefusal` on any failure; a bare return means
    "proceed". Never prints the URL (it carries a password) — only the
    host, which is what the operator needs to see to fix the mistake.
    """
    source = os.environ if env is None else env

    if not getattr(args, "i_know_this_is_staging", False):
        raise StagingRefusal(
            "refusing: --i-know-this-is-staging is required. This script writes "
            "hundreds of thousands of rows and must never run unattended against "
            "an environment nobody named."
        )

    url = getattr(args, "database_url", None)
    if not url:
        raise StagingRefusal(
            "refusing: --database-url is required and is never read from the "
            "environment or app settings (or pass --dry-run to print the plan only)."
        )

    railway_env = source.get("RAILWAY_ENVIRONMENT_NAME", "").strip().lower()
    if railway_env in PRODUCTION_ENVIRONMENT_NAMES:
        raise StagingRefusal(
            f"refusing: RAILWAY_ENVIRONMENT_NAME={railway_env!r} names a production "
            "environment."
        )

    host = _host_of(url)
    if not host:
        raise StagingRefusal("refusing: --database-url has no parseable host.")

    tokens = list(STAGING_HOST_TOKENS) + [
        token.strip().lower()
        for token in (getattr(args, "host_allowlist_token", None) or [])
        if token.strip()
    ]
    if host in LOCAL_HOSTS or any(token in host for token in tokens):
        return
    raise StagingRefusal(
        f"refusing: database host {host!r} is not on the staging allowlist "
        f"({', '.join(sorted(LOCAL_HOSTS))} or a host containing one of "
        f"{', '.join(tokens)}). If this really is a staging host under another "
        "name, pass --host-allowlist-token <substring>; never widen this list to "
        "make a production host pass."
    )


class OriginUrlRefusal(StagingRefusal):
    """Raised when ``--origin-base-url`` is not a safe competitor URL."""


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return False  # a name, not an address — resolution is not ours to assume
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_origin_base_url(base_url: str, *, allow_loopback: bool = False) -> str:
    """Validate ``base_url`` with the PRODUCTION save-time SSRF validator.

    Returns the URL with any trailing slash removed.

    ``allow_loopback`` widens acceptance to 127.0.0.0/8 and ``::1``
    **only** — it is checked against the parsed IP address, so no
    hostname, private LAN range or cloud-metadata address can slip
    through it. Everything else still goes through
    :func:`app_shared.url_safety.validate_competitor_url` unchanged, so
    a URL this seeder writes is exactly a URL the API would have
    accepted.
    """
    from app_shared.url_safety import UnsafeUrlError, validate_competitor_url

    cleaned = base_url.rstrip("/")
    probe = f"{cleaned}/p/fleet-000/p000001"
    try:
        validate_competitor_url(probe)
    except UnsafeUrlError as exc:
        if allow_loopback and _is_loopback_host(_host_of(cleaned)):
            return cleaned
        raise OriginUrlRefusal(
            f"refusing: --origin-base-url is not a valid competitor URL ({exc.reason}). "
            "The fixture origin must be reachable at its PUBLIC domain — "
            "*.railway.internal, localhost and private IPs are rejected by the "
            "engine's own save-time SSRF validator, so matches pointing at them "
            "would not behave like real matches. For a loopback smoke run only, "
            "pass --allow-loopback-origin."
        ) from exc
    return cleaned


# --------------------------------------------------------------------------
# Row builders (pure: dicts in, dicts out — the live path just executes them)
# --------------------------------------------------------------------------


def _new_id() -> uuid.UUID:
    from app_shared.ids import new_uuid7

    return new_uuid7()


def workspace_row(index: int, *, prefix: str) -> dict[str, Any]:
    from app_shared.enums import WorkspaceStatus

    return {
        "id": _new_id(),
        "name": f"Fleet test store {index:03d}",
        "slug": f"{prefix}-{index:03d}",
        "status": WorkspaceStatus.ACTIVE,
    }


def competitor_row(workspace_id: uuid.UUID, domain: str) -> dict[str, Any]:
    from app_shared.enums import CompetitorStatus, LegalStatus, RobotsPolicy

    return {
        "id": _new_id(),
        "workspace_id": workspace_id,
        "name": "Fixture origin",
        "domain": domain,
        "status": CompetitorStatus.ACTIVE,
        # APPROVED + IGNORE_AFTER_APPROVAL is correct HERE and only here:
        # the "competitor" is our own fixture service, so there is no
        # third party whose robots.txt or legal position is at stake.
        # Leaving it REVIEW_REQUIRED would make the whole fleet
        # ineligible and the test would measure nothing.
        "legal_status": LegalStatus.APPROVED,
        "robots_policy": RobotsPolicy.IGNORE_AFTER_APPROVAL,
    }


def refresh_rule_row(
    workspace_id: uuid.UUID, *, index: int, anchor: datetime, spacing_seconds: int
) -> dict[str, Any]:
    """One WORKSPACE-scope daily rule, started ``index * spacing`` after
    ``anchor``.

    ``interval_minutes=1440`` + a jittered ``next_run_at``, NOT a
    ``cron_expression``: audit §10's stagger is 14.4 minutes, which a
    5-field UTC cron cannot express (minute granularity only), and
    rounding it to 14 or 15 would either bunch the fleet or drift it
    out of a 24-hour cycle. The table's ``exactly_one_cadence`` CHECK
    means these two columns are mutually exclusive, so this is a real
    choice, made here and documented.
    """
    from app_shared.enums import ScrapeScope

    return {
        "id": _new_id(),
        "workspace_id": workspace_id,
        "name": "fleet-test daily refresh",
        "scope": ScrapeScope.WORKSPACE,
        "cron_expression": None,
        "interval_minutes": DAILY_INTERVAL_MINUTES,
        "priority": 0,
        "enabled": True,
        "next_run_at": anchor + timedelta(seconds=index * spacing_seconds),
        "consecutive_failures": 0,
    }


def _product_rows(
    workspace_id: uuid.UUID, store: str, plan: FleetPlan
) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """``(product, variant)`` pairs for one store, one variant each."""
    from app_shared.enums import ProductStatus, VariantStatus

    for index in range(plan.products_per_store):
        sku = _sku(index + 1)
        product_id = _new_id()
        variant_id = _new_id()
        # Deterministic client price so the rollup/alerting paths see a
        # real comparison rather than every product priced identically.
        price = Decimal(999 + int(_hash_fraction("price", store, sku) * 200_000)) / 100
        yield (
            {
                "id": product_id,
                "workspace_id": workspace_id,
                "external_id": sku,
                "sku": sku,
                "title": f"{store} product {sku}",
                "status": ProductStatus.ACTIVE,
            },
            {
                "id": variant_id,
                "workspace_id": workspace_id,
                "product_id": product_id,
                "external_id": sku,
                "sku": sku,
                "title": "default",
                "current_price": price.quantize(Decimal("0.0001")),
                "currency": "SAR",
                "status": VariantStatus.ACTIVE,
            },
        )


def _match_rows(
    workspace_id: uuid.UUID,
    competitor_id: uuid.UUID,
    store: str,
    plan: FleetPlan,
    base_url: str,
    pairs: Sequence[tuple[dict[str, Any], dict[str, Any]]],
) -> Iterator[dict[str, Any]]:
    from app_shared.enums import HealthStatus, MatchPriority, MatchStatus
    from app_shared.url_pattern import derive_match_url_fields

    extras = extra_match_product_indexes(store, plan)
    for index, (product, variant) in enumerate(pairs):
        for url in match_urls_for_product(
            base_url, store, index, extra=index in extras
        ):
            normalized, pattern, pattern_version = derive_match_url_fields(url)
            yield {
                "id": _new_id(),
                "workspace_id": workspace_id,
                "product_id": product["id"],
                "product_variant_id": variant["id"],
                "competitor_id": competitor_id,
                "competitor_url": url,
                "normalized_competitor_url": normalized,
                "url_pattern": pattern,
                "url_pattern_version": pattern_version,
                "priority": MatchPriority.NORMAL,
                "status": MatchStatus.ACTIVE,
                "health_status": HealthStatus.UNKNOWN,
                "consecutive_failures": 0,
            }


def _chunks(items: Iterable[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    batch: list[dict[str, Any]] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


# --------------------------------------------------------------------------
# Live seeding
# --------------------------------------------------------------------------


@dataclass
class SeedResult:
    """What one run actually wrote."""

    plan: FleetPlan
    workspaces: int = 0
    competitors: int = 0
    products: int = 0
    variants: int = 0
    matches: int = 0
    refresh_rules: int = 0
    domain_rules: int = 0
    seconds: float = 0.0
    skipped_existing: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.as_dict(),
            "workspaces": self.workspaces,
            "competitors": self.competitors,
            "products": self.products,
            "variants": self.variants,
            "matches": self.matches,
            "refresh_rules": self.refresh_rules,
            "domain_rules": self.domain_rules,
            "seconds": round(self.seconds, 1),
            "skipped_existing": list(self.skipped_existing),
        }


def seed_fleet(
    engine: Any,
    plan: FleetPlan,
    *,
    base_url: str,
    slug_prefix: str,
    spacing_seconds: int,
    anchor: datetime,
    fleet_concurrency: int,
    fleet_rate_per_minute: int,
    progress: Any = None,
) -> SeedResult:
    """Write the whole fleet. One transaction PER WORKSPACE.

    Per-workspace transactions, not one giant one: a 1.6-million-row
    single transaction would hold locks and bloat WAL for the entire
    run, and a failure at store 97 would throw away 96 stores of work.
    Per-workspace commits mean an interrupted run is resumable by simply
    re-running — each store is skipped if its slug already exists.
    """
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app_shared.database import set_workspace_context
    from app_shared.models.catalog import Product, ProductVariant
    from app_shared.models.competitors_matches import (
        Competitor,
        CompetitorProductMatch,
    )
    from app_shared.models.domain_rules import DomainRule
    from app_shared.models.identity import Workspace
    from app_shared.models.refresh_rules import RefreshRule
    from sqlalchemy.orm import Session

    result = SeedResult(plan=plan)
    started = time.monotonic()
    domain = _host_of(base_url)

    with Session(engine) as session:
        # `domain_rules` is a fleet-wide (workspace-less) table: without
        # this row the whole 100-store fleet contends for
        # FLEET_HOST_CONCURRENCY_DEFAULT=6 slots against one host.
        stmt = pg_insert(DomainRule.__table__).values(
            id=_new_id(),
            domain=domain,
            fleet_concurrency=fleet_concurrency,
            fleet_rate_per_minute=fleet_rate_per_minute,
            notes=(
                "EPA D1 fleet test: controlled fixture origin, not a real host. "
                "Delete with the staging environment."
            ),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["domain"],
            set_={
                "fleet_concurrency": fleet_concurrency,
                "fleet_rate_per_minute": fleet_rate_per_minute,
            },
        )
        session.execute(stmt)
        session.commit()
        result.domain_rules = 1

    offsets = workspace_start_offsets(plan.stores, spacing_seconds)
    for index in range(plan.stores):
        store = store_slug(index)
        slug = f"{slug_prefix}-{index:03d}"
        with Session(engine) as session:
            existing = session.execute(
                select(Workspace.id).where(Workspace.slug == slug)
            ).first()
            if existing is not None:
                result.skipped_existing.append(slug)
                continue

            workspace = workspace_row(index, prefix=slug_prefix)
            session.execute(pg_insert(Workspace.__table__).values(**workspace))
            workspace_id = workspace["id"]
            # Every subsequent write in this transaction is
            # workspace-scoped; set the GUC the RLS policies read so the
            # seeder behaves identically under FORCE ROW LEVEL SECURITY
            # (it may legitimately be run as either the owner or the app
            # role, and only one of those bypasses RLS).
            set_workspace_context(session, workspace_id)

            competitor = competitor_row(workspace_id, domain)
            session.execute(pg_insert(Competitor.__table__).values(**competitor))

            pairs = list(_product_rows(workspace_id, store, plan))
            for batch in _chunks((p for p, _ in pairs), INSERT_CHUNK):
                session.execute(pg_insert(Product.__table__), batch)
                result.products += len(batch)
            for batch in _chunks((v for _, v in pairs), INSERT_CHUNK):
                session.execute(pg_insert(ProductVariant.__table__), batch)
                result.variants += len(batch)
            for batch in _chunks(
                _match_rows(
                    workspace_id, competitor["id"], store, plan, base_url, pairs
                ),
                INSERT_CHUNK,
            ):
                session.execute(pg_insert(CompetitorProductMatch.__table__), batch)
                result.matches += len(batch)

            session.execute(
                pg_insert(RefreshRule.__table__).values(
                    **refresh_rule_row(
                        workspace_id,
                        index=index,
                        anchor=anchor,
                        spacing_seconds=spacing_seconds,
                    )
                )
            )
            session.commit()

        result.workspaces += 1
        result.competitors += 1
        result.refresh_rules += 1
        if progress is not None:
            print(
                f"seeded {slug}: products={plan.products_per_store} "
                f"matches={plan.matches_per_store} "
                f"start_offset_s={offsets[index]}",
                file=progress,
                flush=True,
            )

    result.seconds = time.monotonic() - started
    return result


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seed_synthetic_fleet.py",
        description=(
            "Seed the EPA D1 synthetic fleet (default 100 workspaces x 5,000 "
            "products x 1.185 matches) against a STAGING database only."
        ),
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "Staging Postgres DSN. Required unless --dry-run. Never read from "
            "$DATABASE_URL or app settings."
        ),
    )
    parser.add_argument(
        "--i-know-this-is-staging",
        action="store_true",
        help="Required acknowledgement. There is no default and no env fallback.",
    )
    parser.add_argument(
        "--host-allowlist-token",
        action="append",
        default=[],
        help=(
            "Extra substring that makes a database host acceptable. Use only for "
            "a staging host whose name does not contain 'staging'."
        ),
    )
    parser.add_argument(
        "--origin-base-url",
        default=None,
        help=(
            "Public base URL of the fixture-origin service, e.g. "
            "https://fixture-origin-staging.up.railway.app. Required unless --dry-run."
        ),
    )
    parser.add_argument(
        "--allow-loopback-origin",
        action="store_true",
        help=(
            "Permit a 127.0.0.0/8 or ::1 origin URL (local smoke only). Widens "
            "nothing else: private LAN ranges and metadata addresses stay rejected."
        ),
    )
    parser.add_argument("--stores", type=int, default=DEFAULT_STORES)
    parser.add_argument(
        "--products-per-store", type=int, default=DEFAULT_PRODUCTS_PER_STORE
    )
    parser.add_argument(
        "--matches-per-product", type=float, default=DEFAULT_MATCHES_PER_PRODUCT
    )
    parser.add_argument(
        "--spacing-seconds",
        type=int,
        default=DEFAULT_SPACING_SECONDS,
        help="Seconds between store start times (audit §10: 864 = 14.4 min).",
    )
    parser.add_argument(
        "--slug-prefix",
        default="fleet",
        help="Workspace slug prefix; also how a re-run recognises what it already seeded.",
    )
    parser.add_argument(
        "--start-at",
        default=None,
        help=(
            "ISO-8601 UTC anchor for the first workspace's next_run_at. "
            "Default: the next whole hour after now."
        ),
    )
    parser.add_argument(
        "--fleet-concurrency", type=int, default=DEFAULT_FLEET_CONCURRENCY
    )
    parser.add_argument(
        "--fleet-rate-per-minute", type=int, default=DEFAULT_FLEET_RATE_PER_MINUTE
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved plan and a sample of the generated rows; connect to nothing.",
    )
    parser.add_argument("--quiet", action="store_true", help="No per-store progress lines.")
    return parser


def _resolve_anchor(raw: str | None) -> datetime:
    if raw:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    return (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        plan = build_plan(
            stores=args.stores,
            products_per_store=args.products_per_store,
            matches_per_product=args.matches_per_product,
        )
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    anchor = _resolve_anchor(args.start_at)

    if args.dry_run:
        print("DRY RUN — nothing is connected to and nothing is written.")
        for key, value in plan.as_dict().items():
            print(f"  {key}: {value}")
        base = (args.origin_base_url or "https://fixture-origin.example").rstrip("/")
        store = store_slug(0)
        extras = extra_match_product_indexes(store, plan)
        print(f"  anchor: {anchor.isoformat()}")
        print(f"  start offsets (s): {workspace_start_offsets(plan.stores, args.spacing_seconds)[:4]} ...")
        print("  sample match urls:")
        for index in range(min(3, plan.products_per_store)):
            for url in match_urls_for_product(
                base, store, index, extra=index in extras
            ):
                print(f"    {url}")
        return 0

    try:
        require_staging(args)
        base_url = validate_origin_base_url(
            args.origin_base_url or "",
            allow_loopback=args.allow_loopback_origin,
        )
    except StagingRefusal as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    from sqlalchemy import create_engine

    engine = create_engine(args.database_url, future=True, pool_pre_ping=True)
    try:
        result = seed_fleet(
            engine,
            plan,
            base_url=base_url,
            slug_prefix=args.slug_prefix,
            spacing_seconds=args.spacing_seconds,
            anchor=anchor,
            fleet_concurrency=args.fleet_concurrency,
            fleet_rate_per_minute=args.fleet_rate_per_minute,
            progress=None if args.quiet else sys.stdout,
        )
    finally:
        engine.dispose()

    print("SEEDED")
    for key, value in result.as_dict().items():
        print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
