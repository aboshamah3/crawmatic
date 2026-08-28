#!/usr/bin/env python3
"""run_gate_d_canary.py — EPA Task D1: Run Gate D 199-target production canary.

Audit refs: ``PRODUCTION_READINESS_IMPLEMENTATION_PLAN_2026-08-25.md`` Task D1
(Steps 1-7) and ``PRODUCTION_READINESS_REPORT_2026-08-24.md`` §12.6.

WHAT THIS SCRIPT IS — AND WHAT IT DELIBERATELY IS NOT
=====================================================

Run Gate D is the first promotion since the freeze. It spends real money
against real merchants from a production deploy. This script therefore
splits into two halves that never blur into each other:

**The offline half runs for real, unattended.** Building the candidate build
manifest, drawing the stratified 199-target sample, hashing the target set
and precomputing the completion SLA are all pure computations over committed
source and a local database restore. They produce evidence the owner reads
*before* deciding anything.

**The live half refuses to run without an explicit owner acknowledgment.**
Deploying the manifest, capturing production baselines, enqueuing the job,
evaluating a live run and signing the bundle each demand
``--owner-go <token>`` with the step's exact token AND an assertion that
every deploy gate in :data:`DEPLOY_GATES` has been closed. A missing flag is
a hard refusal (:class:`OwnerGateRefusal`, exit 3) — never a warning, never a
prompt, never a default-yes. The refusal names what is missing so the owner
can close it, which is the only useful thing an automated step can do at a
decision it is not entitled to make.

That split is the whole design. An orchestrator can run the offline half in
CI; nothing it can do reaches production.

THE SAMPLE IS BUILT FROM A RESTORE, NOT FROM PRODUCTION
=======================================================

``build-sample`` reads a database. During preparation that database is the
Task A4 scratch restore of ``engine-railway-2026-08-25.dump`` — an isolated,
loopback-only container, never production. The resulting artifact is stamped
``BUILT FROM 2026-08-25 RESTORE — REBUILD against live prod at deploy time``
and carries the source fingerprint in its header, because a sample drawn
from a 24-hour-old snapshot is a *proof the builder works*, not the sample
the canary runs. Re-running ``build-sample`` against production at deploy
time produces the set that actually gets enqueued, and its hash is what the
evidence bundle must carry.

A6's ``match_audit_classifications`` sidecar does not exist in the
2026-08-25 dump (the dump predates the migration), so the builder accepts
``--classifications-csv`` — A6's own evidence export — as the classification
source, and prefers the live table whenever it exists. Both paths feed the
identical pure core.

WHY THE ENGINE PIN IS RESOLVED AT RUNTIME (2026-08-28)
======================================================

The other three repositories are pinned to hardcoded short shas, which is
exactly what a cross-repo pin is for: this script cannot be edited by a
commit in ``saas``, so a constant here is an independent statement about a
repository elsewhere, and drift from it is real drift.

The engine pin is different, and was broken from the day this tool was
committed. The constant lived *inside the repository it pinned*, so any
commit that wrote or updated it necessarily moved the engine's HEAD past the
value it had just recorded. ``head.startswith(pin["commit"])`` could
therefore never hold for the engine — not after a stale commit, but
structurally, for every possible value of the constant. ``manifest`` refused
with PIN DRIFT on every invocation since 3f3ea52, which made a certified
gate tool unusable except via ``--allow-pin-drift``, i.e. via the flag that
turns the check off.

The fix is to stop asserting a self-referential fact. The engine entry is
resolved from ``git rev-parse HEAD`` of this repository at run time and
recorded (``pin_source: "runtime-head"``) rather than compared. Nothing is
lost by this, because the engine's candidate identity was never carried by
that sha in the first place: the manifest's ``source.digest`` is derived
from the engine tree itself, and *that* is what proves deployed == candidate.
A sha the tool copies out of its own source proves only that the file was
saved.

The two engine checks that were never self-referential stay, and stay hard:
a working tree that is dirty is still a refusal (a digest over uncommitted
edits names a build nobody else can reproduce), and a path that is not a
readable git repository is still a refusal. Owner decision, 2026-08-28:
fix the tool rather than routinely pass ``--allow-pin-drift``, because a
gate whose normal operation requires its own override is not a gate.

SECRET DISCIPLINE
=================

No DSN, password or provider credential is ever printed. Database URLs come
from ``--db-url`` or the ``GATE_D_DATABASE_URL`` / ``MATCH_AUDIT_DATABASE_URL``
/ ``MIGRATION_DATABASE_URL`` environment variables and are never echoed back,
not even redacted-with-a-hint. Evidence files carry host-free provenance
(server version, row counts, restore digest), which is what a reader needs.

USAGE
=====

Offline (safe, unattended)::

    uv run python scripts/run_gate_d_canary.py manifest --out-dir <evidence>
    uv run python scripts/run_gate_d_canary.py build-sample --out-dir <evidence> \\
        --db-url <scratch restore> --classifications-csv <A6 export> \\
        --source-label "2026-08-25 restore"
    uv run python scripts/run_gate_d_canary.py precompute-sla --out-dir <evidence>
    uv run python scripts/run_gate_d_canary.py evaluate --facts <facts.json>

Owner-gated (refuses without acknowledgment + closed deploy gates)::

    ... deploy-manifest --owner-go OWNER-GO:deploy-manifest --gate-met <each>
    ... preflight       --owner-go OWNER-GO:preflight       --gate-met <each>
    ... enqueue         --owner-go OWNER-GO:enqueue         --gate-met <each>
    ... evaluate --live --owner-go OWNER-GO:evaluate-live   --gate-met <each>
    ... sign            --owner-go OWNER-GO:sign --approver <name>
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import subprocess
import sys
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import MISSING, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "libs" / "shared"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from app_shared.release import canonical_json, sha256_digest  # noqa: E402

GATE_D_TOOL_VERSION = "1"
DEFAULT_SAMPLE_SIZE = 199
DEFAULT_SELECTION_SEED = "run-gate-d-2026-08-26"
RESTORE_PROVENANCE_BANNER = (
    "BUILT FROM 2026-08-25 RESTORE — REBUILD against live prod at deploy time"
)

# --------------------------------------------------------------------------
# Candidate release identity (Step 1 inputs)
# --------------------------------------------------------------------------

#: Sentinel for `commit`: this repository's pin is read from `git rev-parse
#: HEAD` when the manifest is built, not compared against a constant. See the
#: module docstring, "WHY THE ENGINE PIN IS RESOLVED AT RUNTIME (2026-08-28)":
#: a pin stored inside the repo it pins is moved by the very commit that
#: writes it, so it can never match, and the engine's candidate identity is
#: carried by the manifest's tree-derived `source.digest` regardless.
RUNTIME_HEAD = "RUNTIME_HEAD"

#: The four repositories that make up one Run Gate D release. `path` is where
#: the repo lives on the build host; `commit` is the short sha the run
#: committed, or :data:`RUNTIME_HEAD` for this repository. `verify-pins`
#: (folded into `manifest`) re-reads git and refuses on any drift — a manifest
#: that names a commit nobody can reproduce is worse than no manifest. Drift
#: refusal applies to the three cross-repo pins; the engine entry still
#: refuses on a dirty tree or an unreadable repository.
REPO_PINS: tuple[dict[str, Any], ...] = (
    {
        "name": "engine",
        "path": "/srv/crawmatic/crawmatic",
        # Self-referential — resolved from HEAD at run time (2026-08-28).
        "commit": RUNTIME_HEAD,
        "role": "scrape engine + API + workers (this repository)",
        "tag": None,
    },
    {
        "name": "saas",
        "path": "/srv/crawmatic/saas",
        "commit": "283ee52",
        "role": "SaaS control plane, canonical main",
        "tag": None,
    },
    {
        "name": "saas-wt-w51",
        "path": "/srv/crawmatic/saas-wt-w51",
        # Ratified 2026-08-28: advanced 8b543c2 -> adc3631 by one docs-only
        # commit (app/docs/SALLA_MCP_LOGIN_INCIDENT_2026-08-28.md), verified
        # additive, owner-ratified. Still a hardcoded cross-repo pin.
        "commit": "adc3631",
        "role": "W5.1 integration branch (owner merge gate — NOT auto-merged)",
        "tag": None,
    },
    {
        "name": "plugin",
        "path": "/srv/crawmatic/crawmatic-price-watch",
        "commit": "c616bd0",
        "role": "WooCommerce plugin",
        "tag": "v0.9.3",
    },
)


def pinned_commit(name: str) -> str:
    """Short sha the release pins `name` to.

    Only defined for the hardcoded cross-repo pins; asking for a
    runtime-resolved pin is a programming error, not a fallback.
    """
    for pin in REPO_PINS:
        if pin["name"] == name:
            if pin["commit"] == RUNTIME_HEAD:
                raise KeyError(f"{name} is resolved from HEAD at run time, not pinned")
            return str(pin["commit"])
    raise KeyError(f"no repo pin named {name}")

#: sha256 of the byte-reproducible release ZIP built by the plugin repo's
#: `scripts/build_release_zip.py --ref v0.9.3`. Recorded as a constant so the
#: manifest step can *verify* rather than *accept* whatever ZIP happens to be
#: lying around the build host — see the runbook's ZIP warning.
PLUGIN_RELEASE_ZIP_SHA256 = (
    "3c9a8d1c52c3b9602809b1eb2607e75d20a3ea024460de42ad7d5795f849ef1f"
)
PLUGIN_VERSION_MATRIX = "0.9.3:wp>=6.0,woo>=7.0,php>=7.4"

# --------------------------------------------------------------------------
# Deploy gates — the preconditions every live step demands
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeployGate:
    """One precondition that must be closed before any live Gate-D step.

    Every entry is a *recorded blocker* from
    ``.epa/prod-readiness-2026-08-25/BLOCKERS.md``, not a checklist item this
    script invented. `assertion` is what the owner is confirming when they
    pass ``--gate-met <name>``; it is deliberately phrased as a statement of
    fact about production so that asserting it falsely is a lie about a
    specific thing, not a vague sign-off.
    """

    name: str
    blocker_ref: str
    assertion: str


DEPLOY_GATES: tuple[DeployGate, ...] = (
    DeployGate(
        name="entitlement-writer",
        blocker_ref="2026-08-26 [C3/INTEGRATION GAP]",
        assertion=(
            "C3's engine-side workspace_entitlements table has a writer (an "
            "engine entitlement-ingest route with SaaS wiring) OR an explicit "
            "seeding/disable strategy is in place — otherwise every workspace "
            "goes stale-deny 24h after deploy and the canary denies its own work."
        ),
    ),
    DeployGate(
        name="budget-seeding",
        blocker_ref="2026-08-26 [PHASE-C-GATE/DEPLOY GATES] (1)",
        assertion=(
            "Budget limit rows are seeded with an owner-decided posture (fleet "
            "cap and/or per-plan tenant caps). All C3 limit columns default to "
            "NULL, so an unseeded deploy enforces NO money cap at all."
        ),
    ),
    DeployGate(
        name="role-ordering",
        blocker_ref="2026-08-26 [PHASE-C-GATE/DEPLOY GATES] (2)",
        assertion=(
            "The migrate job provisions database roles BEFORE alembic runs "
            "(scripts/provision_db_roles.sql). `alembic upgrade` aborts at "
            "e92029e9902c on any database lacking crawmatic_app."
        ),
    ),
    DeployGate(
        name="scraper-system-dsn",
        blocker_ref="2026-08-26 [PHASE-C-GATE/DEPLOY GATES] (3)",
        assertion=(
            "The new scraper-node credential surface is reviewed and named in "
            "the deploy bundle: C4's recorder gives every Scrapyd node the "
            "BYPASSRLS SYSTEM DSN. Human security gate, not an automated one."
        ),
    ),
    DeployGate(
        name="netledger-buffer-path",
        blocker_ref="2026-08-26 [PHASE-C-GATE/DEPLOY GATES] (3)",
        assertion=(
            "Every Scrapyd node has a writable, ideally persistent "
            "NETLEDGER_BUFFER_PATH. Without it the C4 subresource buffer "
            "cannot flush and browser byte accounting is lost."
        ),
    ),
)

GATE_NAMES = tuple(gate.name for gate in DEPLOY_GATES)


class OwnerGateRefusal(RuntimeError):
    """Raised when a live step is invoked without full owner authorization.

    Deliberately its own exception type rather than ``SystemExit``: the unit
    tests assert on it directly, and a caller embedding this module gets a
    refusal it cannot mistake for an ordinary failure.
    """


#: Every live step and the exact acknowledgment token it demands. The token
#: is step-specific on purpose — copying the deploy acknowledgment into the
#: enqueue command must not work, because they are different decisions with
#: different blast radii.
LIVE_STEPS: dict[str, str] = {
    "deploy-manifest": "OWNER-GO:deploy-manifest",
    "preflight": "OWNER-GO:preflight",
    "enqueue": "OWNER-GO:enqueue",
    "evaluate-live": "OWNER-GO:evaluate-live",
    "sign": "OWNER-GO:sign",
}


def require_owner_go(
    step: str,
    *,
    owner_go: str | None,
    gates_met: Sequence[str] = (),
    required_gates: Sequence[str] = GATE_NAMES,
) -> None:
    """Hard-refuse a live step unless the owner authorized *this* step.

    Three ways to fail, all refusals rather than warnings:

    1. the step is not a known live step (a typo must never fall through to
       "no gate configured, therefore allowed");
    2. ``--owner-go`` is absent or does not equal the step's own token;
    3. any deploy gate in ``required_gates`` was not asserted met.

    There is no ``--force``, no environment-variable bypass and no
    interactive prompt. The only way past this function is for a human to
    type the step's token and name every gate.
    """
    expected = LIVE_STEPS.get(step)
    if expected is None:
        raise OwnerGateRefusal(
            f"unknown live step {step!r} — refusing (known steps: "
            f"{', '.join(sorted(LIVE_STEPS))})"
        )
    if owner_go != expected:
        got = "absent" if not owner_go else "a different token"
        raise OwnerGateRefusal(
            f"REFUSED: live step {step!r} requires explicit owner acknowledgment "
            f"--owner-go {expected} ({got} was supplied). This step touches "
            "production; the run pauses here by design."
        )
    missing = [name for name in required_gates if name not in set(gates_met)]
    if missing:
        details = "\n".join(
            f"  - {gate.name}  [{gate.blocker_ref}]\n      {gate.assertion}"
            for gate in DEPLOY_GATES
            if gate.name in missing
        )
        raise OwnerGateRefusal(
            f"REFUSED: live step {step!r} has unclosed deploy gates. Assert each "
            f"with --gate-met <name> only once it is true in production:\n{details}"
        )


# --------------------------------------------------------------------------
# Step 2 — stratified sample: pure core
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetCandidate:
    """One candidate target, as the stratifier sees it.

    Everything the strata predicates need, and nothing else — so the core is
    testable from literals with no database, no ORM and no clock. Whether a
    field came from production, from the A4 restore or from a fixture is not
    the stratifier's business.
    """

    match_id: str
    competitor_id: str
    domain: str
    competitor_url: str
    classification: str
    variant_identifier: str | None
    previously_successful: bool
    previously_failed: bool
    attempts_p95: int
    used_direct: bool
    used_proxy: bool
    used_browser: bool
    labeled_stech: bool = False


@dataclass(frozen=True)
class Stratum:
    """A named coverage requirement with a floor.

    `floor` is a MINIMUM, not a quota: one target can satisfy several strata
    at once (an amazon.sa target is simultaneously `amazon`, `browser`,
    `proxy` and probably `fallback_heavy`), so quotas that summed to 199
    would be arithmetic fiction. Floors say what the sample must *cover*;
    the remainder is filled proportionally.
    """

    name: str
    floor: int
    predicate: Callable[[TargetCandidate], bool]
    rationale: str


#: Domains whose certification thresholds §12.6 names explicitly.
CERTIFIED_DOMAINS: tuple[str, ...] = ("amazon.sa", "noon.com")
#: Domains whose matches carry a Shopify variant identifier — the 2026-08-24
#: false-NOT_LISTED bug's blast zone (A6 `INVALID_IDENTITY_DOMAINS`).
SHOPIFY_VARIANT_DOMAINS: tuple[str, ...] = ("stech.ink",)
#: A target is "fallback-heavy" when its historical p95 attempts-per-target
#: for one job is >= this. 3 = the first attempt plus two escalations, i.e.
#: the point at which a target reliably exercises the fallback chain rather
#: than occasionally retrying once.
FALLBACK_HEAVY_ATTEMPTS = 3


def default_strata() -> tuple[Stratum, ...]:
    """The §12.6 coverage list, with floors.

    §12.6 names the nine dimensions but no numbers, so the floors below are
    this task's documented conservative choice. Two rules generated them:

    * **A certified-domain floor must make its own threshold measurable.**
      §12.6 requires "no certified domain below 90%". At 15 amazon.sa targets
      a single failure is 6.7% — under the bar; at 5 targets one failure is
      20% and the check becomes noise. 15/18 are the smallest sizes at which
      the 90% gate discriminates, and both are within the available ACTIVE
      pool (19 / 23 as of the 2026-08-25 restore).
    * **Every other floor is the smaller of "enough to see a 2% technical
      failure rate move" and "what the pool can actually supply".** Floors
      that exceed supply are reported as capped rather than failing the
      build — a sample that refuses to exist teaches nobody anything.
    """
    return (
        Stratum(
            "amazon",
            15,
            lambda c: c.domain == "amazon.sa",
            "certified domain (B5, Amazon CSS 5/5) — §12.6 certified threshold",
        ),
        Stratum(
            "noon",
            18,
            lambda c: c.domain == "noon.com",
            "certified domain (B5b, Noon proxy 21/21) — §12.6 certified threshold",
        ),
        Stratum(
            "browser",
            12,
            lambda c: c.used_browser,
            "browser transport — the most expensive path, must be exercised",
        ),
        Stratum(
            "shopify_variant",
            25,
            lambda c: c.domain in SHOPIFY_VARIANT_DOMAINS
            and c.variant_identifier is not None,
            "the 2026-08-24 false-NOT_LISTED identity bug's regression surface",
        ),
        Stratum(
            "previously_failed",
            20,
            lambda c: c.previously_failed,
            "targets with a failure in history — recovery must be observable",
        ),
        Stratum(
            "fallback_heavy",
            40,
            lambda c: c.attempts_p95 >= FALLBACK_HEAVY_ATTEMPTS,
            "targets that reliably escalate — dispatch-integrity blast zone",
        ),
        Stratum(
            "proxy",
            30,
            lambda c: c.used_proxy,
            "paid proxy transport — the ledger/reconciliation surface",
        ),
        Stratum(
            "direct",
            30,
            lambda c: c.used_direct,
            "unpaid direct transport — the control group for cost gates",
        ),
        Stratum(
            "previously_successful",
            90,
            lambda c: c.previously_successful,
            "the freshness denominator — most of the sample by design",
        ),
    )


def _selection_key(seed: str, match_id: str) -> str:
    """Deterministic, seed-dependent, uniformly-spread ordering key.

    Not ``random.shuffle``: a hash of ``seed:match_id`` reproduces byte-identically
    on any machine and any Python build, which is what lets two people verify
    the same target-set hash. Changing the seed changes the draw; keeping it
    fixed makes a no-go repeat run the *same* sample, as §12.6 demands.
    """
    return hashlib.sha256(f"{seed}:{match_id}".encode()).hexdigest()


@dataclass(frozen=True)
class SampleResult:
    targets: tuple[TargetCandidate, ...]
    coverage: dict[str, int]
    capped_strata: tuple[str, ...]
    pool_size: int
    mandatory_count: int
    seed: str

    @property
    def target_set_hash(self) -> str:
        return target_set_hash(self.targets)


def target_set_hash(targets: Iterable[TargetCandidate]) -> str:
    """SHA-256 over the canonical, sorted identity of the target set.

    Only ``(match_id, domain, competitor_url)`` participate. Selection
    metadata, attempt statistics and classification state are deliberately
    excluded: the hash answers "is this the same set of targets?", and it
    must keep answering yes when a target's history changes between the
    build and the run.
    """
    payload = [
        {"match_id": match_id, "domain": domain, "url": url}
        for match_id, domain, url in sorted(
            (t.match_id, t.domain, t.competitor_url) for t in targets
        )
    ]
    return sha256_digest(canonical_json(payload))


def build_stratified_sample(
    pool: Sequence[TargetCandidate],
    *,
    size: int = DEFAULT_SAMPLE_SIZE,
    seed: str = DEFAULT_SELECTION_SEED,
    strata: Sequence[Stratum] | None = None,
) -> SampleResult:
    """Draw ``size`` targets covering every stratum, deterministically.

    The pool must already be restricted to what D1 authorizes: ACTIVE-classified
    matches (A6) plus the labeled S-Tech subset. This function does not widen
    it — passing a candidate whose ``classification`` is not ``ACTIVE`` and
    whose ``labeled_stech`` is false raises, because a sample that quietly
    reached outside the authorized pool would invalidate every rate in §12.6.

    Order of operations, and why:

    1. **Mandatory first.** The labeled S-Tech subset is the regression corpus
       for the incident that motivated this gate. It is not sampled; it is
       included whole.
    2. **Scarcest stratum first.** Filling `previously_successful` (2,000+
       candidates) before `browser` (19) would consume the pool and then find
       the browser floor unreachable. Sorting by available supply makes the
       greedy pass optimal for floors that a single pass can satisfy at all.
    3. **Proportional remainder.** Whatever slots the floors leave are filled
       by largest-remainder apportionment over the pool's domain mix, so the
       sample's domain shape resembles production instead of being whatever
       the floors happened to drag in.
    """
    strata = tuple(strata if strata is not None else default_strata())
    if size <= 0:
        raise ValueError("sample size must be positive")

    for candidate in pool:
        if candidate.classification != "ACTIVE" and not candidate.labeled_stech:
            raise ValueError(
                f"candidate {candidate.match_id} is neither ACTIVE-classified nor "
                "part of the labeled S-Tech subset — D1 forbids widening the pool"
            )

    by_id: dict[str, TargetCandidate] = {}
    for candidate in pool:
        by_id.setdefault(candidate.match_id, candidate)
    ordered = sorted(by_id.values(), key=lambda c: (_selection_key(seed, c.match_id), c.match_id))

    if len(ordered) < size:
        raise ValueError(
            f"pool holds {len(ordered)} distinct candidates, fewer than the "
            f"requested sample size {size} — never pad a canary sample"
        )

    selected: dict[str, TargetCandidate] = {}
    mandatory = [c for c in ordered if c.labeled_stech]
    for candidate in mandatory[:size]:
        selected[candidate.match_id] = candidate

    supply = {
        stratum.name: sum(1 for c in ordered if stratum.predicate(c)) for stratum in strata
    }
    capped: list[str] = []
    for stratum in sorted(strata, key=lambda s: (supply[s.name], s.name)):
        target_floor = min(stratum.floor, supply[stratum.name])
        if target_floor < stratum.floor:
            capped.append(stratum.name)
        have = sum(1 for c in selected.values() if stratum.predicate(c))
        if have >= target_floor:
            continue
        for candidate in ordered:
            if len(selected) >= size:
                break
            if candidate.match_id in selected or not stratum.predicate(candidate):
                continue
            selected[candidate.match_id] = candidate
            have += 1
            if have >= target_floor:
                break

    remaining = size - len(selected)
    if remaining > 0:
        selected.update(
            {c.match_id: c for c in _proportional_fill(ordered, selected, remaining)}
        )

    if len(selected) != size:
        raise ValueError(
            f"stratifier produced {len(selected)} targets, expected {size} — refusing "
            "to emit a sample of the wrong size"
        )

    targets = tuple(
        sorted(selected.values(), key=lambda c: (_selection_key(seed, c.match_id), c.match_id))
    )
    coverage = {s.name: sum(1 for c in targets if s.predicate(c)) for s in strata}
    return SampleResult(
        targets=targets,
        coverage=coverage,
        capped_strata=tuple(sorted(capped)),
        pool_size=len(ordered),
        mandatory_count=len(mandatory),
        seed=seed,
    )


def _proportional_fill(
    ordered: Sequence[TargetCandidate],
    selected: Mapping[str, TargetCandidate],
    remaining: int,
) -> list[TargetCandidate]:
    """Largest-remainder apportionment of the leftover slots across domains.

    Floors pull the sample toward scarce, expensive domains. Without this
    pass a 199-target canary could end up 80% amazon.sa/noon.com and tell
    you nothing about the eight ordinary sites that carry most of the real
    traffic. Largest-remainder (rather than rounding each share) keeps the
    allotments summing exactly to ``remaining`` with no drift.
    """
    available = [c for c in ordered if c.match_id not in selected]
    if not available or remaining <= 0:
        return []

    by_domain: dict[str, list[TargetCandidate]] = {}
    for candidate in available:
        by_domain.setdefault(candidate.domain, []).append(candidate)

    total = len(available)
    quotas: dict[str, float] = {
        domain: remaining * len(members) / total for domain, members in by_domain.items()
    }
    allotted = {domain: min(int(q), len(by_domain[domain])) for domain, q in quotas.items()}
    leftover = remaining - sum(allotted.values())
    ranked = sorted(
        by_domain,
        key=lambda d: (-(quotas[d] - int(quotas[d])), -len(by_domain[d]), d),
    )
    index = 0
    while leftover > 0 and ranked:
        domain = ranked[index % len(ranked)]
        if allotted[domain] < len(by_domain[domain]):
            allotted[domain] += 1
            leftover -= 1
        index += 1
        if index > len(ranked) * (remaining + 1):  # pragma: no cover - exhausted supply
            break

    picked: list[TargetCandidate] = []
    for domain, count in allotted.items():
        picked.extend(by_domain[domain][:count])
    return picked[:remaining]


def validate_sample(result: SampleResult, *, size: int, strata: Sequence[Stratum] | None = None) -> list[str]:
    """Return the list of validity problems — empty means the sample is valid.

    Returned rather than raised so a caller can report every problem at once;
    the CLI turns a non-empty list into a non-zero exit.
    """
    strata = tuple(strata if strata is not None else default_strata())
    problems: list[str] = []
    if len(result.targets) != size:
        problems.append(f"size {len(result.targets)} != {size}")
    ids = [t.match_id for t in result.targets]
    if len(set(ids)) != len(ids):
        problems.append("duplicate match_id in sample")
    for stratum in strata:
        have = result.coverage.get(stratum.name, 0)
        if have == 0:
            problems.append(f"stratum {stratum.name!r} is empty")
        elif have < stratum.floor and stratum.name not in result.capped_strata:
            problems.append(
                f"stratum {stratum.name!r} has {have}, floor {stratum.floor}"
            )
    for target in result.targets:
        if target.classification != "ACTIVE" and not target.labeled_stech:
            problems.append(f"target {target.match_id} outside the authorized pool")
    return problems


# --------------------------------------------------------------------------
# Step 3 — completion SLA precomputation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DomainRateLimit:
    """Certified (or assumed) request ceiling for one merchant domain."""

    domain: str
    requests_per_second: float
    certified: bool
    source: str


#: Certified merchant rate limits. Each number is the rate the certification
#: run ACTUALLY held itself to, not the rate the site was observed to
#: tolerate — a certification is only evidence for the discipline it
#: exercised. amazon.sa and stech.ink certified at 0.6s spacing under a
#: 2 req/domain/s cap; noon.com's B5b proxy capture states <=1.67 req/s.
CERTIFIED_RATE_LIMITS: dict[str, DomainRateLimit] = {
    "amazon.sa": DomainRateLimit(
        "amazon.sa", 1.667, True, "B5 certification (Amazon CSS 5/5, 0.6s spacing)"
    ),
    "noon.com": DomainRateLimit(
        "noon.com", 1.667, True, "B5b certification (Noon proxy 21/21, <=1.67 req/s)"
    ),
    "stech.ink": DomainRateLimit(
        "stech.ink", 2.0, True, "B4 identifier capture (<=2 req/domain/s, hard cap 40)"
    ),
}

#: Uncertified domains inherit the fleet's own client-side discipline — the
#: <=2 req/domain/s every EPA capture held to, which is also what
#: CONCURRENT_REQUESTS_PER_DOMAIN=4 permits at the latencies measured. Flagged
#: `certified=False` so the SLA record says how much of it rests on assumption.
DEFAULT_UNCERTIFIED_RPS = 2.0

#: apps/scrapers/price_monitor/settings.py CONCURRENT_REQUESTS_PER_DOMAIN.
PER_DOMAIN_CONCURRENCY = 4
#: app_shared.scheduling.fair_queue.DEFAULT_FLEET_CONCURRENCY.
DEFAULT_FLEET_CONCURRENCY = 64
#: Enqueue->dispatch latency plus finalization, settlement drain and the
#: netledger buffer flush. 120s is deliberately generous: under-budgeting the
#: non-fetch overhead would make the SLA fail on bookkeeping rather than on
#: scraping, which teaches nothing about the canary.
FIXED_OVERHEAD_SECONDS = 120
#: Multiplier over the critical path. 1.25 matches the safety buffer §12.7
#: already uses for the cost ceiling; reusing it keeps one number to argue
#: about rather than two.
SLA_SAFETY_FACTOR = 1.25
#: §12.6's stated target.
SECTION_12_6_TARGET_SECONDS = 600


@dataclass(frozen=True)
class DomainWorkload:
    domain: str
    targets: int
    attempts_p95_per_target: float
    p95_latency_seconds: float


def compute_completion_sla(
    workloads: Sequence[DomainWorkload],
    *,
    rate_limits: Mapping[str, DomainRateLimit] | None = None,
    per_domain_concurrency: int = PER_DOMAIN_CONCURRENCY,
    fleet_concurrency: int = DEFAULT_FLEET_CONCURRENCY,
    fixed_overhead_seconds: int = FIXED_OVERHEAD_SECONDS,
    safety_factor: float = SLA_SAFETY_FACTOR,
    target_seconds: int = SECTION_12_6_TARGET_SECONDS,
) -> dict[str, Any]:
    """Precompute the canary's completion SLA from the sample's domain mix.

    The model, stated plainly so it can be argued with:

    * Work is **physical attempts**, not targets. A target that escalates
      three times costs three fetches, so each domain's load is
      ``targets x p95 attempts-per-target`` — p95 rather than the mean
      because an SLA sized on the mean is wrong half the time.
    * Each domain's throughput is ``min(rate ceiling, concurrency / latency)``.
      Both limits are real and either can bind: noon.com's certified 1.667/s
      is the ceiling, but at a 30s p95 latency four concurrent slots only
      deliver 0.13/s, so latency governs and pretending otherwise would
      promise an SLA the fleet cannot meet.
    * Domains run **in parallel** (the fleet has more slots than the sample
      has domains), so the per-domain critical path is the max, not the sum.
      A separate fleet-level term catches the case where total concurrency,
      not any single domain, is the constraint.
    * The larger of the two, times the safety factor, plus fixed overhead.

    Returns the full working — every intermediate per domain — because an SLA
    whose derivation is not inspectable is a number someone will later have
    to take on faith at exactly the wrong moment.
    """
    limits = dict(rate_limits if rate_limits is not None else CERTIFIED_RATE_LIMITS)
    if fleet_concurrency <= 0 or per_domain_concurrency <= 0:
        raise ValueError("concurrency must be positive")

    per_domain: list[dict[str, Any]] = []
    total_attempts = 0.0
    latency_weighted = 0.0
    for workload in workloads:
        if workload.targets <= 0:
            continue
        if workload.p95_latency_seconds <= 0:
            raise ValueError(f"{workload.domain}: p95 latency must be positive")
        limit = limits.get(
            workload.domain,
            DomainRateLimit(
                workload.domain,
                DEFAULT_UNCERTIFIED_RPS,
                False,
                "uncertified — fleet client-side discipline (<=2 req/domain/s)",
            ),
        )
        attempts = workload.targets * workload.attempts_p95_per_target
        concurrency_rps = per_domain_concurrency / workload.p95_latency_seconds
        effective_rps = min(limit.requests_per_second, concurrency_rps)
        seconds = attempts / effective_rps
        total_attempts += attempts
        latency_weighted += attempts * workload.p95_latency_seconds
        per_domain.append(
            {
                "domain": workload.domain,
                "targets": workload.targets,
                "attempts_p95_per_target": round(workload.attempts_p95_per_target, 3),
                "planned_attempts": round(attempts, 2),
                "p95_latency_seconds": round(workload.p95_latency_seconds, 3),
                "certified_rps": limit.requests_per_second,
                "rate_limit_certified": limit.certified,
                "rate_limit_source": limit.source,
                "concurrency_rps": round(concurrency_rps, 4),
                "effective_rps": round(effective_rps, 4),
                "binding_constraint": (
                    "rate_limit" if limit.requests_per_second <= concurrency_rps else "concurrency"
                ),
                "domain_seconds": round(seconds, 1),
            }
        )

    if not per_domain:
        raise ValueError("no workload — cannot precompute an SLA for an empty sample")

    mean_latency = latency_weighted / total_attempts
    fleet_rps = fleet_concurrency / mean_latency
    fleet_seconds = total_attempts / fleet_rps
    domain_critical = max(entry["domain_seconds"] for entry in per_domain)
    critical_path = max(domain_critical, fleet_seconds)
    computed = int(math.ceil(critical_path * safety_factor)) + fixed_overhead_seconds
    governing = max(target_seconds, computed)

    uncertified = sorted(
        entry["domain"] for entry in per_domain if not entry["rate_limit_certified"]
    )
    return {
        "record_type": "gate_d_completion_sla",
        "model_version": GATE_D_TOOL_VERSION,
        "inputs": {
            "per_domain_concurrency": per_domain_concurrency,
            "fleet_concurrency": fleet_concurrency,
            "fixed_overhead_seconds": fixed_overhead_seconds,
            "safety_factor": safety_factor,
            "section_12_6_target_seconds": target_seconds,
        },
        "per_domain": sorted(per_domain, key=lambda e: -e["domain_seconds"]),
        "totals": {
            "targets": sum(entry["targets"] for entry in per_domain),
            "planned_attempts": round(total_attempts, 2),
            "attempt_weighted_mean_latency_seconds": round(mean_latency, 3),
            "fleet_rps": round(fleet_rps, 4),
            "fleet_seconds": round(fleet_seconds, 1),
            "domain_critical_path_seconds": round(domain_critical, 1),
            "critical_path_seconds": round(critical_path, 1),
        },
        "computed_sla_seconds": computed,
        "governing_sla_seconds": governing,
        "governing_source": (
            "section-12.6-target"
            if governing == target_seconds and computed <= target_seconds
            else "precomputed-sla"
        ),
        "ten_minute_target_feasible": computed <= target_seconds,
        "uncertified_rate_limit_domains": uncertified,
        "note": (
            "Domains without a B4/B5/B5b certification use the fleet's own "
            "client-side discipline (<=2 req/domain/s). That assumption is "
            "listed above so an owner can see how much of the SLA rests on it."
        ),
    }


# --------------------------------------------------------------------------
# Steps 4-7 — evaluation of every §12.6 condition
# --------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
PENDING = "PENDING"


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    status: str
    detail: str
    observed: Any = None
    threshold: Any = None


@dataclass(frozen=True)
class CanaryFacts:
    """Everything the §12.6 evaluation needs about a finished canary run.

    Populated from the live database by ``evaluate --live`` (owner-gated) or
    from a JSON fixture by ``evaluate --facts`` (offline, how the unit tests
    drive it). Keeping it a plain dataclass is what makes every check a pure
    function: no check may query anything, so no check can pass because a
    query silently returned nothing.
    """

    job_terminal: bool
    elapsed_seconds: int
    governing_sla_seconds: int
    targets_total: int
    targets_pending: int
    targets_started: int
    targets_deferred: int
    targets_unaccounted: int
    targets_with_one_terminal_outcome: int
    successful_observations_labeled_failed: int
    eligible_active_targets: int
    fresh_comparable_offers: int
    per_domain_fresh_rate: Mapping[str, float] = field(default_factory=dict)
    certified_domains: Sequence[str] = CERTIFIED_DOMAINS
    technical_failures: int = 0
    confirmed_delisted: int = 0
    stech_labeled_targets: int = 0
    stech_false_not_listed: int = 0
    duplicate_physical_dispatches: int = 0
    shared_guard_identities: int = 0
    paid_operations: int = 0
    operations_missing_authorization: int = 0
    operations_missing_reservation: int = 0
    operations_missing_settlement: int = 0
    operations_missing_allocation: int = 0
    grants_issued: int = 0
    grants_with_exactly_one_operation: int = 0
    grants_still_reserved_after_drain: int = 0
    budget_counter_total: float = 0.0
    ledger_settled_total: float = 0.0
    app_bytes: int = 0
    provider_bytes: int = 0
    proxy_cost_usd: float = 0.0
    combined_cost_usd: float = 0.0
    railway_billing_confirmed: bool | None = None
    railway_confirmed_combined_cost_usd: float | None = None
    canary_attributable_alerts: Sequence[str] = ()
    deliberate_test_alert_fired: bool = False
    evidence_bundle_signed: bool = False
    approved_proxy_cost_ceiling_usd: float = 0.05
    approved_combined_cost_ceiling_usd: float = 0.08

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "CanaryFacts":
        known = {f for f in cls.__dataclass_fields__}
        unknown = sorted(set(payload) - known)
        if unknown:
            raise ValueError(
                f"unknown fact keys {unknown} — refusing to evaluate a canary from "
                "facts this evaluator does not understand"
            )
        missing = sorted(
            name
            for name, spec in cls.__dataclass_fields__.items()
            if name not in payload
            and spec.default is MISSING
            and spec.default_factory is MISSING
        )
        if missing:
            raise ValueError(f"missing required fact keys: {missing}")
        return cls(**payload)


def _rate(numerator: float, denominator: float) -> float:
    return 0.0 if denominator <= 0 else numerator / denominator


def _variance_pct(a: float, b: float) -> float:
    """Symmetric relative variance, in percent, against the larger side.

    Dividing by the larger of the two rather than by "the provider's number"
    means a 2% gate cannot be gamed by whichever side happens to be smaller.
    """
    scale = max(abs(a), abs(b))
    return 0.0 if scale == 0 else abs(a - b) / scale * 100.0


def evaluate_canary(facts: CanaryFacts) -> list[CheckOutcome]:
    """Every §12.6 condition, plus the Gate-C rescope additions, as named checks.

    One function per condition would be twenty near-identical functions; one
    list of closures keeps the conditions readable side by side, which is how
    they are read in the report. Each check states its threshold in its own
    outcome so the evidence bundle records what the bar WAS, not just whether
    it was cleared — thresholds change, and old evidence must stay legible.
    """
    checks: list[CheckOutcome] = []

    def add(name: str, ok: bool, detail: str, observed: Any = None, threshold: Any = None) -> None:
        checks.append(CheckOutcome(name, PASS if ok else FAIL, detail, observed, threshold))

    # --- terminal state and target accounting -----------------------------
    add(
        "terminal_within_sla",
        facts.job_terminal and facts.elapsed_seconds <= facts.governing_sla_seconds,
        "job reached a terminal state within the governing SLA",
        {"terminal": facts.job_terminal, "elapsed_seconds": facts.elapsed_seconds},
        facts.governing_sla_seconds,
    )
    unaccounted = (
        facts.targets_pending
        + facts.targets_started
        + facts.targets_deferred
        + facts.targets_unaccounted
    )
    add(
        "zero_unaccounted_targets",
        unaccounted == 0,
        "no PENDING/STARTED/DEFERRED/unaccounted targets after finalization",
        {
            "pending": facts.targets_pending,
            "started": facts.targets_started,
            "deferred": facts.targets_deferred,
            "unaccounted": facts.targets_unaccounted,
        },
        0,
    )
    add(
        "one_explainable_terminal_outcome",
        facts.targets_with_one_terminal_outcome == facts.targets_total,
        "every target has exactly one explainable terminal outcome",
        facts.targets_with_one_terminal_outcome,
        facts.targets_total,
    )
    add(
        "no_success_labeled_failed",
        facts.successful_observations_labeled_failed == 0,
        "no successful observation is labeled failed/skipped",
        facts.successful_observations_labeled_failed,
        0,
    )

    # --- outcome quality ---------------------------------------------------
    fresh_rate = _rate(facts.fresh_comparable_offers, facts.eligible_active_targets)
    add(
        "fresh_comparable_offer_rate",
        fresh_rate >= 0.95,
        "fresh comparable offers on eligible active targets",
        round(fresh_rate, 4),
        0.95,
    )
    below = {
        domain: round(rate, 4)
        for domain, rate in facts.per_domain_fresh_rate.items()
        if domain in set(facts.certified_domains) and rate < 0.90
    }
    add(
        "certified_domain_floor",
        not below,
        "no certified domain below 90% fresh comparable offers",
        below or {d: round(facts.per_domain_fresh_rate.get(d, 0.0), 4) for d in facts.certified_domains},
        0.90,
    )
    tech_rate = _rate(facts.technical_failures, facts.targets_total)
    add(
        "technical_failure_rate",
        tech_rate <= 0.02,
        "explicit technical failure rate (confirmed-delisted reported separately: "
        f"{facts.confirmed_delisted})",
        round(tech_rate, 4),
        0.02,
    )
    add(
        "stech_zero_false_not_listed",
        facts.stech_false_not_listed == 0,
        f"labeled S-Tech subset ({facts.stech_labeled_targets} targets) has zero false NOT_LISTED",
        facts.stech_false_not_listed,
        0,
    )

    # --- dispatch integrity ------------------------------------------------
    add(
        "no_duplicate_physical_dispatch",
        facts.duplicate_physical_dispatches == 0,
        "the same logical batch is never physically dispatched twice",
        facts.duplicate_physical_dispatches,
        0,
    )
    add(
        "no_shared_guard_identity",
        facts.shared_guard_identities == 0,
        "distinct fallback work never shares a guard identity",
        facts.shared_guard_identities,
        0,
    )

    # --- ledger, grants and money -----------------------------------------
    missing_ops = {
        "authorization": facts.operations_missing_authorization,
        "reservation": facts.operations_missing_reservation,
        "settlement": facts.operations_missing_settlement,
        "allocation": facts.operations_missing_allocation,
    }
    add(
        "no_unledgered_paid_operations",
        all(count == 0 for count in missing_ops.values()),
        f"every one of {facts.paid_operations} paid operations carries authorization, "
        "reservation, settlement and workspace allocation",
        missing_ops,
        0,
    )
    byte_variance = _variance_pct(facts.app_bytes, facts.provider_bytes)
    add(
        "provider_reconciliation_within_2pct",
        byte_variance <= 2.0,
        "application attempts + browser subresources reconcile to provider traffic",
        round(byte_variance, 3),
        2.0,
    )
    # Gate-C rescope additions (BLOCKERS 2026-08-26 [PHASE-C-GATE] item 4).
    add(
        "grant_operation_cardinality",
        facts.grants_issued == facts.grants_with_exactly_one_operation,
        "every authorization grant maps to exactly one opened network operation",
        {
            "grants": facts.grants_issued,
            "with_exactly_one_operation": facts.grants_with_exactly_one_operation,
        },
        "equal",
    )
    add(
        "no_grant_left_reserved_after_drain",
        facts.grants_still_reserved_after_drain == 0,
        "no grant remains RESERVED after the queue drains",
        facts.grants_still_reserved_after_drain,
        0,
    )
    budget_variance = _variance_pct(facts.budget_counter_total, facts.ledger_settled_total)
    add(
        "budget_vs_ledger_reconciliation",
        budget_variance <= 2.0,
        "post-run budget counters reconcile against settled ledger totals",
        round(budget_variance, 3),
        2.0,
    )
    add(
        "proxy_cost_ceiling",
        facts.proxy_cost_usd <= facts.approved_proxy_cost_ceiling_usd,
        "proxy cost, judged immediately from the internal ledger + provider live usage",
        facts.proxy_cost_usd,
        facts.approved_proxy_cost_ceiling_usd,
    )
    add(
        "combined_cost_ceiling_provisional",
        facts.combined_cost_usd <= facts.approved_combined_cost_ceiling_usd,
        "combined variable cost from internal compute metering (immediate go/no-go)",
        facts.combined_cost_usd,
        facts.approved_combined_cost_ceiling_usd,
    )
    # Split by measurement latency, exactly as D1 Step 5 requires: Railway
    # billing lags, so PENDING is a legitimate third state here and a later
    # confirmation failure REOPENS the gate rather than being written off.
    if facts.railway_billing_confirmed is None:
        checks.append(
            CheckOutcome(
                "combined_cost_railway_confirmation",
                PENDING,
                "Railway billing has not reported yet — the gate stays reopenable "
                "until this confirms; a failure here reopens Gate D",
                None,
                facts.approved_combined_cost_ceiling_usd,
            )
        )
    else:
        confirmed = facts.railway_confirmed_combined_cost_usd
        add(
            "combined_cost_railway_confirmation",
            bool(facts.railway_billing_confirmed)
            and confirmed is not None
            and confirmed <= facts.approved_combined_cost_ceiling_usd,
            "combined cost confirmed against Railway billing after its reporting lag",
            confirmed,
            facts.approved_combined_cost_ceiling_usd,
        )

    # --- alerting ----------------------------------------------------------
    add(
        "no_canary_attributable_invariant_alert",
        not facts.canary_attributable_alerts,
        "no RLS/outbox/billing/breaker/queue/dispatch-integrity/stuck-job alert "
        "attributable to this run (unrelated background warnings do not count)",
        list(facts.canary_attributable_alerts),
        [],
    )
    add(
        "deliberate_test_alert_fired",
        facts.deliberate_test_alert_fired,
        "one deliberate test alert proves alerting is operational",
        facts.deliberate_test_alert_fired,
        True,
    )
    add(
        "evidence_bundle_signed",
        facts.evidence_bundle_signed,
        "signed evidence bundle (SHA256SUMS + approver line) attached to the manifest",
        facts.evidence_bundle_signed,
        True,
    )
    return checks


def verdict(checks: Sequence[CheckOutcome]) -> str:
    """GO / GO-PENDING-CONFIRMATION / NO-GO.

    Any FAIL is a no-go, per §12.6 ("Any failed condition is a no-go") — there
    is no weighting, no majority and no "minor" failure. A PENDING check
    cannot produce a GO either: it produces GO-PENDING-CONFIRMATION, which is
    a real state the owner must later close, not a softer pass.
    """
    if any(check.status == FAIL for check in checks):
        return "NO-GO"
    if any(check.status == PENDING for check in checks):
        return "GO-PENDING-CONFIRMATION"
    return "GO"


# --------------------------------------------------------------------------
# Database loading (thin; the pure core above never touches it)
# --------------------------------------------------------------------------


def _resolve_db_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    for env_var in (
        "GATE_D_DATABASE_URL",
        "MATCH_AUDIT_DATABASE_URL",
        "MIGRATION_DATABASE_URL",
    ):
        value = os.environ.get(env_var)
        if value:
            return value
    raise SystemExit(
        "No database URL: pass --db-url or set GATE_D_DATABASE_URL / "
        "MATCH_AUDIT_DATABASE_URL / MIGRATION_DATABASE_URL. "
        "(The value is never printed by this script.)"
    )


def _open_session(db_url: str):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(db_url, future=True)
    return sessionmaker(bind=engine, future=True)()


_POOL_SQL = """
SELECT m.id::text                         AS match_id,
       m.competitor_id::text              AS competitor_id,
       c.domain                           AS domain,
       m.competitor_url                   AS competitor_url,
       m.competitor_variant_identifier    AS variant_identifier,
       COALESCE(o.any_success, FALSE)     AS previously_successful,
       COALESCE(o.any_failure, FALSE)     AS previously_failed,
       COALESCE(a.attempts_p95, 1)        AS attempts_p95,
       COALESCE(a.used_direct, FALSE)     AS used_direct,
       COALESCE(a.used_proxy, FALSE)      AS used_proxy,
       COALESCE(a.used_browser, FALSE)    AS used_browser
FROM competitor_product_matches m
JOIN competitors c ON c.id = m.competitor_id
LEFT JOIN (
    SELECT match_id,
           bool_or(success)      AS any_success,
           bool_or(NOT success)  AS any_failure
    FROM price_observations
    GROUP BY match_id
) o ON o.match_id = m.id
LEFT JOIN (
    SELECT match_id,
           GREATEST(COALESCE(percentile_disc(0.95) WITHIN GROUP (ORDER BY n), 1), 1)::int
               AS attempts_p95,
           bool_or(used_direct)  AS used_direct,
           bool_or(used_proxy)   AS used_proxy,
           bool_or(used_browser) AS used_browser
    FROM (
        SELECT match_id, scrape_job_id, count(*) AS n,
               bool_or(access_method IN ('DIRECT_HTTP','DIRECT_HTTP_RETRY')) AS used_direct,
               bool_or(access_method = 'PROXY_HTTP')                          AS used_proxy,
               bool_or(access_method LIKE 'PLAYWRIGHT%')                      AS used_browser
        FROM request_attempts
        WHERE scrape_job_id IS NOT NULL
        GROUP BY match_id, scrape_job_id
    ) per_job
    GROUP BY match_id
) a ON a.match_id = m.id
WHERE m.status = 'ACTIVE'
"""

#: Appended to :data:`_POOL_SQL` when the sample is restricted to one
#: workspace. Bound parameter, never interpolated.
_POOL_WORKSPACE_PREDICATE = "  AND m.workspace_id = :workspace_id\n"


def pool_sql(workspace_id: str | None) -> str:
    """The candidate-pool query, optionally restricted to one workspace.

    ``workspace_id=None`` reproduces the original fleet-wide pool exactly, so
    every previously-built sample remains byte-reproducible. A non-``None``
    value appends a bound-parameter predicate — the sample is then drawn only
    from that workspace's ACTIVE matches.

    Restricting the pool is a *sample design* decision with certification
    consequences: whatever is excluded is not covered by the run. The chosen
    workspace is recorded in ``target_set.json`` (``workspace_filter``) so no
    reader has to infer the sample's tenancy from the targets themselves.
    """
    if workspace_id is None:
        return _POOL_SQL
    return _POOL_SQL.rstrip("\n") + "\n" + _POOL_WORKSPACE_PREDICATE


#: Per-domain SLA inputs, measured from history rather than assumed.
#: `attempts_p95` is the p95 of attempts-per-(target, job) — the fan-out one
#: target costs in one run. `p95_latency_seconds` is the p95 of raw per-attempt
#: response time; the two are measured over different units on purpose (a run's
#: duration is attempts x per-attempt latency, not per-target latency), so they
#: are computed as two independent aggregates and joined by domain.
_DOMAIN_STATS_SQL = """
WITH per_job AS (
    SELECT r.match_id, r.scrape_job_id, count(*) AS n
    FROM request_attempts r
    WHERE r.scrape_job_id IS NOT NULL
    GROUP BY r.match_id, r.scrape_job_id
),
fanout AS (
    SELECT c.domain AS domain,
           GREATEST(COALESCE(percentile_disc(0.95) WITHIN GROUP (ORDER BY per_job.n), 1), 1)::float
               AS attempts_p95
    FROM per_job
    JOIN competitor_product_matches m ON m.id = per_job.match_id
    JOIN competitors c ON c.id = m.competitor_id
    GROUP BY c.domain
),
latency AS (
    SELECT c.domain AS domain,
           GREATEST(
               COALESCE(percentile_disc(0.95) WITHIN GROUP (ORDER BY r.response_time_ms), 1000),
               1000
           )::float / 1000.0 AS p95_latency_seconds
    FROM request_attempts r
    JOIN competitor_product_matches m ON m.id = r.match_id
    JOIN competitors c ON c.id = m.competitor_id
    WHERE r.response_time_ms IS NOT NULL
    GROUP BY c.domain
)
SELECT fanout.domain,
       fanout.attempts_p95,
       COALESCE(latency.p95_latency_seconds, 5.0) AS p95_latency_seconds
FROM fanout
LEFT JOIN latency ON latency.domain = fanout.domain
"""


def load_classifications(session, csv_path: Path | None) -> dict[str, str]:
    """Current A6 classification per match, from the sidecar or its CSV export.

    The live ``match_audit_classifications`` table is preferred and is what a
    deploy-time rebuild will use. The 2026-08-25 dump predates that migration,
    so during preparation the classifications come from A6's own evidence CSV.
    The two are the same data by construction (the CSV is what the classifier
    wrote), and the source is recorded in the sample header so no reader has
    to guess which one produced a given artifact.
    """
    from sqlalchemy import text

    if csv_path is None:
        rows = session.execute(
            text(
                "SELECT match_id::text AS match_id, state FROM match_audit_classifications "
                "WHERE superseded_at IS NULL"
            )
        ).all()
        return {row.match_id: str(row.state) for row in rows}

    with csv_path.open(newline="", encoding="utf-8") as handle:
        return {
            row["match_id"]: row["state"]
            for row in csv.DictReader(handle)
            if row.get("match_id")
        }


def load_candidate_pool(
    session,
    *,
    classifications: Mapping[str, str],
    labeled_stech_ids: Sequence[str],
    workspace_id: str | None = None,
) -> list[TargetCandidate]:
    """Build the authorized candidate pool from the database + A6 verdicts.

    ``workspace_id`` restricts the pool to a single workspace (see
    :func:`pool_sql`); ``None`` keeps the fleet-wide behaviour.
    """
    from sqlalchemy import text

    labeled = set(labeled_stech_ids)
    pool: list[TargetCandidate] = []
    params = {} if workspace_id is None else {"workspace_id": workspace_id}
    for row in session.execute(text(pool_sql(workspace_id)), params).all():
        state = classifications.get(row.match_id, "UNKNOWN")
        is_labeled = row.match_id in labeled
        if state != "ACTIVE" and not is_labeled:
            continue
        pool.append(
            TargetCandidate(
                match_id=row.match_id,
                competitor_id=row.competitor_id,
                domain=row.domain,
                competitor_url=row.competitor_url,
                classification=state,
                variant_identifier=row.variant_identifier,
                previously_successful=bool(row.previously_successful),
                previously_failed=bool(row.previously_failed),
                attempts_p95=int(row.attempts_p95),
                used_direct=bool(row.used_direct),
                used_proxy=bool(row.used_proxy),
                used_browser=bool(row.used_browser),
                labeled_stech=is_labeled,
            )
        )
    return pool


def load_domain_stats(session) -> dict[str, dict[str, float]]:
    from sqlalchemy import text

    return {
        row.domain: {
            "attempts_p95": float(row.attempts_p95),
            "p95_latency_seconds": float(row.p95_latency_seconds),
        }
        for row in session.execute(text(_DOMAIN_STATS_SQL)).all()
    }


def labeled_stech_match_ids(fixtures_dir: Path) -> list[str]:
    """The labeled S-Tech subset — one directory per match_id (EPA B4 corpus)."""
    if not fixtures_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in fixtures_dir.iterdir()
        if entry.is_dir() and entry.name.count("-") == 4
    )


# --------------------------------------------------------------------------
# Evidence writing
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _git(repo: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", *args], cwd=str(repo), capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def verify_repo_pins() -> tuple[list[dict[str, Any]], list[str]]:
    """Re-read all four repositories and compare against :data:`REPO_PINS`.

    Returns ``(records, problems)``. A pin that does not match, a tree that is
    dirty, or a tag that resolves elsewhere is a *problem*, because the whole
    point of a build manifest is that someone else can rebuild the same thing.

    The engine entry carries :data:`RUNTIME_HEAD` instead of a sha: its
    expected commit *is* whatever HEAD reads at build time, so there is no
    comparison to make (see the module docstring for why a self-pin can never
    hold). Every other check on that entry is unchanged and still fails shut —
    an unreadable repository and a dirty working tree are both refusals.
    """
    records: list[dict[str, Any]] = []
    problems: list[str] = []
    for pin in REPO_PINS:
        repo = Path(pin["path"])
        head = _git(repo, "rev-parse", "HEAD")
        short = _git(repo, "rev-parse", "--short", "HEAD")
        status = _git(repo, "status", "--porcelain")
        runtime_head = pin["commit"] == RUNTIME_HEAD
        record = {
            "name": pin["name"],
            "path": pin["path"],
            "expected_commit": short if runtime_head else pin["commit"],
            "pin_source": "runtime-head" if runtime_head else "constant",
            "head_commit": head,
            "head_short": short,
            "tree_clean": status == "" if status is not None else None,
            "tag": pin["tag"],
            "tag_commit": _git(repo, "rev-list", "-n", "1", pin["tag"]) if pin["tag"] else None,
            "role": pin["role"],
        }
        if short is None:
            problems.append(f"{pin['name']}: not a readable git repository at {pin['path']}")
        elif not runtime_head and not head.startswith(pin["commit"]):  # type: ignore[union-attr]
            problems.append(
                f"{pin['name']}: HEAD {short} != pinned {pin['commit']}"
            )
        if record["tree_clean"] is False:
            problems.append(f"{pin['name']}: working tree is dirty")
        if pin["tag"] and record["tag_commit"] and head and record["tag_commit"] != head:
            problems.append(
                f"{pin['name']}: tag {pin['tag']} resolves to "
                f"{record['tag_commit'][:7]}, not HEAD {short}"
            )
        records.append(record)
    return records, problems


# --------------------------------------------------------------------------
# CLI — Step 1
# --------------------------------------------------------------------------


def cmd_manifest(args: argparse.Namespace) -> int:
    """Step 1 (offline half): build the immutable candidate build manifest.

    Reuses A5's machinery verbatim — ``build_release_manifest.build_manifest``
    computes the same self-hash it would at any other build, so the manifest
    produced here IS the candidate manifest, not a rehearsal of one. The four
    repo pins are written to their own file first and referenced from the
    manifest's evidence map by digest, because the A5 manifest schema has
    slots for one SaaS commit and one plugin ZIP, and Gate D ships four repos.
    """
    import build_release_manifest as brm

    out_dir = args.out_dir
    records, problems = verify_repo_pins()
    if problems and not args.allow_pin_drift:
        for problem in problems:
            print(f"PIN DRIFT: {problem}", file=sys.stderr)
        print(
            "refusing to build a candidate manifest from drifted pins "
            "(--allow-pin-drift records the drift instead)",
            file=sys.stderr,
        )
        return 2

    pins_payload = {
        "record_type": "gate_d_repo_pins",
        "generated_at": args.generated_at or _now(),
        "repos": records,
        "plugin_release_zip_sha256": PLUGIN_RELEASE_ZIP_SHA256,
        "plugin_zip_reproduction_command": (
            "python3 scripts/build_release_zip.py --ref v0.9.3 --out-dir dist --verify"
        ),
        "pin_problems": problems,
    }
    pins_path = out_dir / "repo_pins.json"
    _write_json(pins_path, pins_payload)
    pins_digest = sha256_digest(pins_path.read_bytes())

    manifest_args = argparse.Namespace(
        image_digest=list(args.image_digest or []),
        evidence=[
            f"repo_pins=file://{pins_path}#sha256={pins_digest}",
            *(args.evidence or []),
        ],
        saas_commit=args.saas_commit or pinned_commit("saas"),
        saas_protocol_range=args.saas_protocol_range,
        plugin_zip_sha256=PLUGIN_RELEASE_ZIP_SHA256,
        plugin_version_matrix=[PLUGIN_VERSION_MATRIX],
        salla_contract_version=args.salla_contract_version,
        generated_at=args.generated_at,
    )
    manifest = brm.build_manifest(manifest_args)
    manifest_path = out_dir / "release_manifest.json"
    _write_json(manifest_path, manifest)
    manifest["signing"]["gpg"] = brm._gpg_record(manifest_path, args.gpg_key)
    _write_json(manifest_path, manifest)
    identity_path = out_dir / "release_identity.json"
    _write_json(identity_path, brm.identity_subset(manifest, image_key=args.identity_image))

    print(f"manifest_id={manifest['manifest_id']}")
    print(f"source_digest={manifest['source']['digest']}")
    print(f"expected_db_migration={manifest['migrations']['head']}")
    print(f"repo_pins_sha256={pins_digest}")
    print(f"plugin_zip_sha256={PLUGIN_RELEASE_ZIP_SHA256}")
    print(f"wrote {manifest_path}")
    print(f"wrote {identity_path}")
    print(f"wrote {pins_path}")
    for problem in problems:
        print(f"RECORDED PIN DRIFT: {problem}", file=sys.stderr)
    return 0


def cmd_deploy_manifest(args: argparse.Namespace) -> int:
    """Step 1 (live half): deploy exactly the candidate manifest. OWNER-GATED.

    This subcommand deliberately performs no deployment of its own even after
    the gate opens. The deploy is a multi-service Railway promotion with a
    pre-migration backup, role provisioning, an alembic upgrade and a
    ``/version`` equality gate — every step of which is written out in
    ``evidence/A5_DEPLOY_RUNBOOK.md`` §1.4 with the exact commands. Wrapping
    that in a script would create a second, less-reviewed copy of the most
    dangerous procedure in the run. So the gate opens onto instructions, and
    the attestation the runbook produces is what proves it happened.
    """
    require_owner_go("deploy-manifest", owner_go=args.owner_go, gates_met=args.gate_met)
    manifest_path = args.out_dir / "release_manifest.json"
    if not manifest_path.exists():
        print(f"missing {manifest_path} — run `manifest` first", file=sys.stderr)
        return 2
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    print("OWNER GATE OPEN — deploy is executed by the runbook, not by this script.")
    print(f"  manifest_id           : {manifest['manifest_id']}")
    print(f"  expected_db_migration : {manifest['migrations']['head']}")
    print("  runbook               : evidence/A5_DEPLOY_RUNBOOK.md §1.4")
    print("  attestation (after)   : scripts/build_deployment_attestation.py")
    print(
        "    --build-manifest "
        f"{manifest_path} --environment production --backup-id <pre-migration backup> "
        "--approver <name> --live-migration-head <from /version> "
        "--smoke version_endpoint=pass --smoke ready_endpoint=pass"
    )
    print(
        "  /version must equal manifest_id above; if it does not, roll back rather "
        "than proceeding to Step 2."
    )
    return 0


# --------------------------------------------------------------------------
# CLI — Step 2
# --------------------------------------------------------------------------


def cmd_build_sample(args: argparse.Namespace) -> int:
    session = _open_session(_resolve_db_url(args.db_url))
    try:
        classifications = load_classifications(session, args.classifications_csv)
        labeled = labeled_stech_match_ids(args.stech_fixtures)
        pool = load_candidate_pool(
            session,
            classifications=classifications,
            labeled_stech_ids=labeled,
            workspace_id=args.workspace,
        )
        domain_stats = load_domain_stats(session)
        server_version = _server_version(session)
    finally:
        session.close()

    result = build_stratified_sample(pool, size=args.size, seed=args.seed)
    problems = validate_sample(result, size=args.size)

    header = {
        "record_type": "gate_d_target_set",
        "tool_version": GATE_D_TOOL_VERSION,
        "generated_at": _now(),
        "provenance_banner": args.provenance_banner,
        "source_label": args.source_label,
        "database_server_version": server_version,
        "classification_source": (
            f"A6 evidence CSV {args.classifications_csv}"
            if args.classifications_csv
            else "live match_audit_classifications (superseded_at IS NULL)"
        ),
        "classification_rows": len(classifications),
        "labeled_stech_subset": len(labeled),
        "workspace_filter": args.workspace,
        "pool_size": result.pool_size,
        "sample_size": len(result.targets),
        "selection_seed": result.seed,
        "target_set_hash": result.target_set_hash,
        "coverage": result.coverage,
        "strata_floors": {s.name: s.floor for s in default_strata()},
        "strata_rationale": {s.name: s.rationale for s in default_strata()},
        "capped_strata": list(result.capped_strata),
        "validation_problems": problems,
        "domain_mix": _domain_mix(result.targets),
    }
    _write_json(args.out_dir / "target_set.json", {**header, "targets": [t.__dict__ for t in result.targets]})
    _write_json(args.out_dir / "domain_stats.json", domain_stats)
    _write_csv(
        args.out_dir / "target_set.csv",
        result.targets,
    )
    (args.out_dir / "TARGET_SET_HASH.txt").write_text(
        f"{result.target_set_hash}  target_set (sha256 over sorted "
        f"[match_id, domain, url])\n{args.provenance_banner}\n",
        encoding="utf-8",
    )

    print(f"provenance={args.provenance_banner}")
    print(f"pool_size={result.pool_size}")
    print(f"sample_size={len(result.targets)}")
    print(f"target_set_hash={result.target_set_hash}")
    print(f"coverage={json.dumps(result.coverage, sort_keys=True)}")
    print(f"domain_mix={json.dumps(_domain_mix(result.targets), sort_keys=True)}")
    if result.capped_strata:
        print(f"capped_strata={list(result.capped_strata)}", file=sys.stderr)
    for problem in problems:
        print(f"SAMPLE PROBLEM: {problem}", file=sys.stderr)
    return 1 if problems else 0


def _server_version(session) -> str:
    from sqlalchemy import text

    return str(session.execute(text("SHOW server_version")).scalar())


def _domain_mix(targets: Sequence[TargetCandidate]) -> dict[str, int]:
    mix: dict[str, int] = {}
    for target in targets:
        mix[target.domain] = mix.get(target.domain, 0) + 1
    return dict(sorted(mix.items(), key=lambda kv: (-kv[1], kv[0])))


def _write_csv(path: Path, targets: Sequence[TargetCandidate]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(targets[0].__dict__))
        writer.writeheader()
        for target in targets:
            writer.writerow(target.__dict__)


# --------------------------------------------------------------------------
# CLI — Step 3
# --------------------------------------------------------------------------


def cmd_precompute_sla(args: argparse.Namespace) -> int:
    target_set = json.loads((args.out_dir / "target_set.json").read_text(encoding="utf-8"))
    stats = json.loads((args.out_dir / "domain_stats.json").read_text(encoding="utf-8"))
    mix = target_set["domain_mix"]

    workloads = [
        DomainWorkload(
            domain=domain,
            targets=count,
            attempts_p95_per_target=float(stats.get(domain, {}).get("attempts_p95", 3.0)),
            p95_latency_seconds=float(stats.get(domain, {}).get("p95_latency_seconds", 5.0)),
        )
        for domain, count in mix.items()
    ]
    sla = compute_completion_sla(
        workloads,
        per_domain_concurrency=args.per_domain_concurrency,
        fleet_concurrency=args.fleet_concurrency,
    )
    sla["target_set_hash"] = target_set["target_set_hash"]
    sla["provenance_banner"] = target_set["provenance_banner"]
    sla["generated_at"] = _now()
    _write_json(args.out_dir / "completion_sla.json", sla)

    print(f"computed_sla_seconds={sla['computed_sla_seconds']}")
    print(f"governing_sla_seconds={sla['governing_sla_seconds']}")
    print(f"governing_source={sla['governing_source']}")
    print(f"ten_minute_target_feasible={sla['ten_minute_target_feasible']}")
    print(f"critical_path_seconds={sla['totals']['critical_path_seconds']}")
    for entry in sla["per_domain"][:5]:
        print(
            f"  {entry['domain']:<18} targets={entry['targets']:<4} "
            f"attempts={entry['planned_attempts']:<7} "
            f"eff_rps={entry['effective_rps']:<8} "
            f"bind={entry['binding_constraint']:<12} secs={entry['domain_seconds']}"
        )
    if sla["uncertified_rate_limit_domains"]:
        print(
            "uncertified_rate_limit_domains="
            + ",".join(sla["uncertified_rate_limit_domains"])
        )
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    """Step 3 (live half): capture production baselines. OWNER-GATED.

    Each capture is a *live production read* against a system that is either
    metered (the provider), billed (Railway) or authoritative for the run's
    correctness (breaker verdicts, frozen profile versions, ``/version``). None
    of them may be taken before the owner has said go, because a baseline
    captured at the wrong moment silently mis-attributes every later delta.
    """
    require_owner_go("preflight", owner_go=args.owner_go, gates_met=args.gate_met)
    captures = {
        "provider_usage_baseline": (
            "DataImpulse usage for the dedicated D1 subaccount/correlation tag, "
            "read at T-0. Gate C's lesson: a shared account cannot be reconciled "
            "by time window while unrelated traffic flows."
        ),
        "railway_usage_baseline": "Railway project usage snapshot at T-0 (billing lags; this is the anchor).",
        "breaker_verdicts": "Every proxy/domain breaker's verdict at T-0 — an already-open breaker invalidates the run.",
        "frozen_profile_versions": "Domain-profile versions frozen for the run; any change mid-run is a no-go.",
        "version_equality": "GET /version must equal the manifest_id from Step 1.",
    }
    print("OWNER GATE OPEN — pre-flight capture authorized. Capture each, into "
          f"{args.out_dir / 'preflight'}:")
    for name, why in captures.items():
        print(f"  [{name}] {why}")
    print(
        "This script does not perform the captures itself: each needs a "
        "credentialed session (provider dashboard, Railway CLI, production DSN) "
        "that D1's preparation is explicitly forbidden from holding. Record each "
        "capture as its own JSON file plus the command that produced it."
    )
    return 0


# --------------------------------------------------------------------------
# CLI — Steps 4-7
# --------------------------------------------------------------------------


def cmd_enqueue(args: argparse.Namespace) -> int:
    """Step 4: enqueue ONE job with ONE idempotency identity. OWNER-GATED."""
    require_owner_go("enqueue", owner_go=args.owner_go, gates_met=args.gate_met)
    sla_path = args.out_dir / "completion_sla.json"
    hash_path = args.out_dir / "TARGET_SET_HASH.txt"
    for path in (sla_path, hash_path):
        if not path.exists():
            print(f"missing {path} — Steps 2 and 3 must complete first", file=sys.stderr)
            return 2
    sla = json.loads(sla_path.read_text(encoding="utf-8"))
    if RESTORE_PROVENANCE_BANNER in sla.get("provenance_banner", ""):
        print(
            "REFUSED: the SLA on disk was precomputed from the 2026-08-25 restore. "
            "Rebuild the sample and the SLA against live production before enqueue "
            "— the restore sample is a proof the builder works, not the run's sample.",
            file=sys.stderr,
        )
        return 3
    print("OWNER GATE OPEN — enqueue authorized.")
    print(f"  target_set_hash       : {sla['target_set_hash']}")
    print(f"  governing_sla_seconds : {sla['governing_sla_seconds']} ({sla['governing_source']})")
    print("  Enqueue through the NORMAL path (one job, one idempotency identity):")
    print("    the standard refresh/full-run enqueue API — never a bespoke inserter,")
    print("    because the canary must exercise the code path production uses.")
    print("  Then publish 5-minute progress snapshots into the evidence dir until terminal.")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Step 5: evaluate every §12.6 condition.

    Offline with ``--facts`` (a JSON fact bundle — how the tests and any dry
    run drive it). ``--live`` reads the finished job from production and is
    owner-gated, because that read happens against the production DSN.
    """
    if args.live:
        require_owner_go("evaluate-live", owner_go=args.owner_go, gates_met=args.gate_met)
        print(
            "OWNER GATE OPEN — live evaluation authorized. Collect the fact bundle "
            "from the finished job (job/target counters, ledger, grants, provider "
            "usage, alert log) into a facts JSON, then re-run with --facts.",
        )
        return 0
    if not args.facts:
        print("--facts <path> is required without --live", file=sys.stderr)
        return 2

    facts = CanaryFacts.from_mapping(json.loads(args.facts.read_text(encoding="utf-8")))
    checks = evaluate_canary(facts)
    result = verdict(checks)
    payload = {
        "record_type": "gate_d_evaluation",
        "tool_version": GATE_D_TOOL_VERSION,
        "generated_at": _now(),
        "verdict": result,
        "checks": [check.__dict__ for check in checks],
    }
    if args.out_dir:
        _write_json(args.out_dir / "evaluation.json", payload)

    width = max(len(check.name) for check in checks)
    for check in checks:
        print(f"{check.status:<8} {check.name:<{width}}  {check.detail}")
    print(f"VERDICT={result}")
    if result == "NO-GO":
        print(
            "NO-GO: fix the root cause and repeat the SAME gate with the SAME sample "
            "design (seed unchanged). Never enlarge the sample to average away a "
            "failure — see `no-go` for the loop.",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_no_go(args: argparse.Namespace) -> int:
    """Step 6: the no-go loop, stated as a procedure rather than automated.

    Automating "fix the root cause" would be a lie. What this command does is
    refuse the two shortcuts a no-go invites — a bigger sample and a new seed —
    by printing the invariants a repeat run must preserve and, when an
    evaluation exists, naming exactly which checks failed.
    """
    evaluation_path = args.out_dir / "evaluation.json"
    print("RUN GATE D — NO-GO LOOP (Step 6)")
    print("  1. Root-cause every FAIL below. A failed condition is a defect, not noise.")
    print("  2. Fix it in code/config and land it on a clean commit.")
    print("  3. Rebuild the manifest (Step 1) — the fix changes the candidate.")
    print("  4. Rebuild the sample with the SAME seed and the SAME size:")
    print(f"       --seed {DEFAULT_SELECTION_SEED} --size {DEFAULT_SAMPLE_SIZE}")
    print("     The target-set hash may move (production data moves); the sample")
    print("     DESIGN must not. Enlarging the sample to dilute a failure is")
    print("     forbidden by §12.6 and by this script's refusal to accept a size")
    print("     it did not compute.")
    print("  5. Repeat Steps 3-5 in full. Partial re-runs prove nothing.")
    if evaluation_path.exists():
        payload = json.loads(evaluation_path.read_text(encoding="utf-8"))
        failed = [c for c in payload["checks"] if c["status"] == FAIL]
        print(f"\nLast evaluation verdict: {payload['verdict']}")
        for check in failed:
            print(f"  FAIL {check['name']}: observed={check['observed']} threshold={check['threshold']}")
        if not failed:
            print("  (no failing checks recorded)")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    """Step 7: sign the evidence bundle. OWNER-GATED.

    Signing follows A1's pattern exactly: a NUL-sorted, byte-reproducible
    ``SHA256SUMS`` over every other file in the bundle, plus an approver line
    naming the human who made the go decision. There is no GPG key in custody
    (recorded by A4/A5), so "signed" here means checksummed-and-attributed, and
    the file says so rather than implying a cryptographic signature nobody can
    verify.
    """
    require_owner_go("sign", owner_go=args.owner_go, gates_met=args.gate_met, required_gates=())
    bundle = args.out_dir
    if not bundle.is_dir():
        print(f"no such bundle directory: {bundle}", file=sys.stderr)
        return 2
    evaluation_path = bundle / "evaluation.json"
    if evaluation_path.exists():
        payload = json.loads(evaluation_path.read_text(encoding="utf-8"))
        if payload["verdict"] == "NO-GO" and not args.sign_no_go:
            print(
                "REFUSED: the recorded verdict is NO-GO. A signed bundle asserts a "
                "passing gate; pass --sign-no-go only to preserve a failed run's "
                "evidence, which is signed as a NO-GO record.",
                file=sys.stderr,
            )
            return 3

    files = sorted(
        path
        for path in bundle.rglob("*")
        if path.is_file() and path.name not in {"SHA256SUMS", "APPROVAL.txt"}
    )
    # Bare hex, two spaces, relative path — `sha256sum -c` format exactly, so
    # the owner verifies with the standard tool and not with this script. Note
    # `app_shared.release.sha256_digest` prefixes "sha256:" and is therefore
    # deliberately NOT used here.
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  "
        f"{path.relative_to(bundle).as_posix()}"
        for path in files
    ]
    (bundle / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")

    manifest_path = bundle / "release_manifest.json"
    manifest_id = (
        json.loads(manifest_path.read_text(encoding="utf-8"))["manifest_id"]
        if manifest_path.exists()
        else "UNKNOWN"
    )
    approval = (
        "RUN GATE D — EVIDENCE BUNDLE APPROVAL\n"
        f"bundle          : {bundle}\n"
        f"manifest_id     : {manifest_id}\n"
        f"decision        : {args.decision}\n"
        f"approver        : {args.approver}\n"
        f"approved_at     : {_now()}\n"
        f"files_checksummed: {len(files)}\n"
        "mechanism       : sha256-checksum-manifest + named approver line\n"
        "                  (no GPG key is in custody — this is attribution, not\n"
        "                   a cryptographic signature; see evidence/restore-drill)\n"
        "verify          : cd <bundle> && sha256sum -c SHA256SUMS\n"
    )
    (bundle / "APPROVAL.txt").write_text(approval, encoding="utf-8")
    print(f"wrote {bundle / 'SHA256SUMS'} ({len(files)} files)")
    print(f"wrote {bundle / 'APPROVAL.txt'}")
    print(f"manifest_id={manifest_id}")
    return 0


def cmd_gates(args: argparse.Namespace) -> int:
    """List the deploy gates and the live steps, with their tokens."""
    print("DEPLOY GATES (every live step requires all of them via --gate-met):\n")
    for gate in DEPLOY_GATES:
        print(f"  {gate.name}")
        print(f"      blocker : {gate.blocker_ref}")
        print(f"      assert  : {gate.assertion}\n")
    print("LIVE STEPS and their acknowledgment tokens:\n")
    for step, token in LIVE_STEPS.items():
        print(f"  {step:<18} --owner-go {token}")
    return 0


# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------


DEFAULT_EVIDENCE_DIR = Path("/srv/crawmatic/evidence/run-gate-d-2026-08-26")


def _workspace_uuid(raw: str) -> str:
    """argparse type for ``--workspace``: a well-formed UUID, kept canonical.

    Rejects anything that is not a UUID rather than letting a typo silently
    produce an empty pool (which ``build_stratified_sample`` would then refuse
    as undersized, several steps and one confusing error later).
    """
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError):
        raise argparse.ArgumentTypeError(f"not a valid UUID: {raw!r}") from None


def _add_owner_gate_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--owner-go",
        default=None,
        metavar="TOKEN",
        help="the step's exact acknowledgment token (see `gates`); absent => hard refusal",
    )
    parser.add_argument(
        "--gate-met",
        action="append",
        default=[],
        choices=GATE_NAMES,
        help="repeatable; assert one deploy gate is closed in production",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_gate_d_canary.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_EVIDENCE_DIR,
        help="evidence bundle directory (default: %(default)s)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_manifest = sub.add_parser("manifest", help="Step 1 offline: build the candidate build manifest")
    p_manifest.add_argument("--image-digest", action="append", metavar="SERVICE=sha256:...")
    p_manifest.add_argument("--evidence", action="append", metavar="NAME=URL")
    p_manifest.add_argument("--saas-commit", default=None)
    p_manifest.add_argument("--saas-protocol-range", default=None)
    p_manifest.add_argument("--salla-contract-version", default=None)
    p_manifest.add_argument("--identity-image", default="api")
    p_manifest.add_argument("--generated-at", default=None, help="pin the timestamp for byte-comparable rebuilds")
    p_manifest.add_argument("--gpg-key", default=None)
    p_manifest.add_argument(
        "--allow-pin-drift",
        action="store_true",
        help="record repo-pin drift in the manifest instead of refusing",
    )
    p_manifest.set_defaults(func=cmd_manifest)

    p_deploy = sub.add_parser("deploy-manifest", help="Step 1 live: OWNER-GATED deploy of that manifest")
    _add_owner_gate_args(p_deploy)
    p_deploy.set_defaults(func=cmd_deploy_manifest)

    p_sample = sub.add_parser("build-sample", help="Step 2: build the stratified 199-target sample")
    p_sample.add_argument("--db-url", default=None, help="never echoed; env fallbacks documented in --help")
    p_sample.add_argument("--classifications-csv", type=Path, default=None)
    p_sample.add_argument(
        "--stech-fixtures",
        type=Path,
        default=_REPO_ROOT / "tests" / "fixtures" / "stech_30_targets",
    )
    p_sample.add_argument(
        "--workspace",
        type=_workspace_uuid,
        default=None,
        metavar="UUID",
        help=(
            "restrict the candidate pool to one workspace; omit for the "
            "fleet-wide pool. Recorded in target_set.json as workspace_filter — "
            "whatever is excluded is NOT covered by the certification."
        ),
    )
    p_sample.add_argument("--size", type=int, default=DEFAULT_SAMPLE_SIZE)
    p_sample.add_argument("--seed", default=DEFAULT_SELECTION_SEED)
    p_sample.add_argument("--source-label", default="unspecified")
    p_sample.add_argument("--provenance-banner", default=RESTORE_PROVENANCE_BANNER)
    p_sample.set_defaults(func=cmd_build_sample)

    p_sla = sub.add_parser("precompute-sla", help="Step 3 offline: precompute the completion SLA")
    p_sla.add_argument("--per-domain-concurrency", type=int, default=PER_DOMAIN_CONCURRENCY)
    p_sla.add_argument("--fleet-concurrency", type=int, default=DEFAULT_FLEET_CONCURRENCY)
    p_sla.set_defaults(func=cmd_precompute_sla)

    p_pre = sub.add_parser("preflight", help="Step 3 live: OWNER-GATED baseline capture")
    _add_owner_gate_args(p_pre)
    p_pre.set_defaults(func=cmd_preflight)

    p_enq = sub.add_parser("enqueue", help="Step 4: OWNER-GATED enqueue of the one canary job")
    _add_owner_gate_args(p_enq)
    p_enq.set_defaults(func=cmd_enqueue)

    p_eval = sub.add_parser("evaluate", help="Step 5: evaluate every §12.6 condition")
    p_eval.add_argument("--facts", type=Path, default=None, help="JSON fact bundle (offline)")
    p_eval.add_argument("--live", action="store_true", help="OWNER-GATED live evaluation")
    _add_owner_gate_args(p_eval)
    p_eval.set_defaults(func=cmd_evaluate)

    p_nogo = sub.add_parser("no-go", help="Step 6: the no-go loop's invariants")
    p_nogo.set_defaults(func=cmd_no_go)

    p_sign = sub.add_parser("sign", help="Step 7: OWNER-GATED bundle signing (SHA256SUMS + approver)")
    p_sign.add_argument("--approver", required=True)
    p_sign.add_argument("--decision", default="GO", choices=["GO", "GO-PENDING-CONFIRMATION", "NO-GO"])
    p_sign.add_argument("--sign-no-go", action="store_true")
    _add_owner_gate_args(p_sign)
    p_sign.set_defaults(func=cmd_sign)

    p_gates = sub.add_parser("gates", help="list the deploy gates and live-step tokens")
    p_gates.set_defaults(func=cmd_gates)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except OwnerGateRefusal as refusal:
        print(str(refusal), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
