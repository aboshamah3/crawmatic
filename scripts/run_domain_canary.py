#!/usr/bin/env python3
"""run_domain_canary.py — EPA C4: labeled domain canary (Noon, S-Tech, Amazon).

WHAT THIS IS
============

A domain canary answers one question: *does this domain's current strategy
still extract the prices we already know are correct?* It does that by
re-fetching a small, fixed set of targets whose expected offers are
recorded as **labels** (``tests/fixtures/labeled_offers/<domain>.jsonl``)
and comparing what came back against them.

The acceptance bar is **>= 95 % label agreement on price + currency +
availability**, per field, over the scored labels — see
``docs/ops/DOMAIN_STRATEGIES_2026-09.md``.

TWO HALVES, AND THEY NEVER BLUR
===============================

**Scoring** (``--results``) is offline and free. It reads labels and a
captured results file and reports agreement. It touches no network,
spends nothing, and is what CI and unit tests run. Its scoring core
(:func:`score_results`) is pure.

**Fetching** (no ``--results``) **spends real money** — proxy egress
against a live merchant — and therefore refuses to do anything unless it
is given, explicitly:

* ``--max-usd`` — a hard ceiling on this canary's spend. **Required, with
  no default.** A canary whose budget is implicit is a canary that can
  cost anything; the flag exists so the number is a decision someone
  typed, and the same rule already governs every other money-capable
  script in this repository (``scripts/run_gate_d_canary.py``'s
  ``--owner-go``, ``railway_cost_watchdog.py``'s caps).
* ``--owner-go`` with this domain's exact token — the owner acknowledgment
  that live requests may leave this host.

Neither has a default, neither can be inferred, and a missing one is a
hard refusal (exit 2 for the missing flag, exit 3 for a refused gate),
never a prompt and never a default-yes. ``--max-usd`` is required on the
offline path too -- see :func:`build_parser`.

**As of 2026-09-08 the live half has never been run.** EPA's pre-flight
deferred all spend, so the fetching path ships tested-by-refusal only: the
argument parsing, the gate and the budget arithmetic are exercised, the
fetch is not -- it is not implemented at all rather than implemented
untested. The gate row in ``docs/ops/DOMAIN_STRATEGIES_2026-09.md``
is marked deferred and its result cells are empty on purpose — an empty
cell is an honest "not measured"; a filled one would be a fabrication.

USAGE
=====

Offline (safe, free, unattended)::

    uv run python scripts/run_domain_canary.py \\
        --domain noon.com --max-usd 0 \\
        --labels tests/fixtures/labeled_offers/noon.jsonl \\
        --results <results.jsonl>

Owner-gated (spends; NOT run by EPA)::

    uv run python scripts/run_domain_canary.py \\
        --domain noon.com --max-usd 1.00 \\
        --labels tests/fixtures/labeled_offers/noon.jsonl \\
        --owner-go OWNER-GO:canary:noon.com

LABEL AND RESULT SHAPE
======================

Both files are JSON Lines, one offer per line, joined on ``match_id``:

    {"domain": ..., "match_id": ..., "url": ..., "variant": ...,
     "seller": ..., "price": "349.2500", "currency": "SAR",
     "availability": "IN_STOCK", "expected_error_code": null,
     "label_source": [...]}

``price``/``currency`` are ``null`` for a label that expects no price
(``expected_error_code`` says why). ``seller`` is ``null`` wherever the
evidence bundle captured none — it is recorded, never scored, exactly
like ``variant``: the bar names price, currency and availability.

SECRET DISCIPLINE
=================

No DSN, proxy credential or provider password is read, printed or
written by this script. It takes file paths and a dollar ceiling.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Sequence

#: Fields the acceptance bar is computed over. Deliberately NOT ``seller``
#: or ``variant``: the 2026-08 evidence bundle captured no seller for the
#: two marketplaces, so scoring it would measure the fixture's gaps rather
#: than the domain's strategy. They are carried in the labels for triage.
SCORED_FIELDS: tuple[str, ...] = ("price", "currency", "availability")

#: The bar, per scored field. From the plan (C4 step 2) and restated in
#: ``docs/ops/DOMAIN_STRATEGIES_2026-09.md``.
ACCEPTANCE_AGREEMENT = 0.95

#: Exit codes. 0 pass, 1 below the bar, 2 argparse (a missing required
#: flag), 3 a refused owner gate. Distinct so a caller can tell "the
#: canary ran and regressed" from "the canary refused to run".
EXIT_PASS = 0
EXIT_BELOW_BAR = 1
EXIT_GATE_REFUSED = 3


class OwnerGateRefusal(RuntimeError):
    """Raised when a spending step is invoked without its acknowledgment."""


def owner_go_token(domain: str) -> str:
    """The exact ``--owner-go`` value that authorizes ``domain``'s canary.

    Domain-specific on purpose: an acknowledgment typed for noon.com must
    not silently authorize a run against amazon.sa.
    """
    return f"OWNER-GO:canary:{domain}"


@dataclass(frozen=True)
class FieldScore:
    """Agreement for ONE scored field."""

    field_name: str
    compared: int = 0
    agreed: int = 0

    @property
    def agreement(self) -> float:
        """Share agreed, or ``0.0`` when nothing was comparable.

        Zero, not one: "nothing to compare" must never read as a pass.
        """
        if self.compared == 0:
            return 0.0
        return self.agreed / self.compared

    @property
    def meets_bar(self) -> bool:
        return self.compared > 0 and self.agreement >= ACCEPTANCE_AGREEMENT


@dataclass(frozen=True)
class CanaryScore:
    """The full comparison of one results file against one label file."""

    domain: str
    labels: int
    results: int
    matched: int
    missing_results: tuple[str, ...] = ()
    unexpected_results: tuple[str, ...] = ()
    fields: dict[str, FieldScore] = field(default_factory=dict)
    disagreements: tuple[dict[str, Any], ...] = ()

    @property
    def passed(self) -> bool:
        """Every scored field at or above the bar, and no label unanswered.

        A missing result is a FAILURE, not an omission: a canary that
        fetched 20 of 30 labeled targets has not shown the strategy still
        works, it has shown that a third of it stopped answering.
        """
        if not self.fields or self.missing_results:
            return False
        return all(score.meets_bar for score in self.fields.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "labels": self.labels,
            "results": self.results,
            "matched": self.matched,
            "missing_results": list(self.missing_results),
            "unexpected_results": list(self.unexpected_results),
            "acceptance_agreement": ACCEPTANCE_AGREEMENT,
            "fields": {
                name: {
                    "compared": score.compared,
                    "agreed": score.agreed,
                    "agreement": round(score.agreement, 4),
                    "meets_bar": score.meets_bar,
                }
                for name, score in self.fields.items()
            },
            "disagreements": list(self.disagreements),
            "passed": self.passed,
        }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSON Lines file, skipping blank lines.

    Raises ``ValueError`` naming the 1-based line number on a bad row —
    a silent skip would quietly shrink the label set the bar is computed
    over, which is exactly the failure mode a canary must not have.
    """
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                row = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{number}: not valid JSON ({exc})") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{number}: expected a JSON object")
            rows.append(row)
    return rows


def _normalize(field_name: str, value: Any) -> Any:
    """Comparable form of one field value.

    ``price`` is compared as a :class:`~decimal.Decimal` so ``"349.2500"``
    and ``349.25`` agree — the label came out of a numeric DB column and
    a live result may arrive as either. Everything else compares as a
    case-folded, stripped string, so ``"sar"`` agrees with ``"SAR"``.
    ``None`` stays ``None``: "no price" is a real, comparable answer
    (the expected outcome for a genuinely unlisted offer), never a
    wildcard.
    """
    if value is None or value == "":
        return None
    if field_name == "price":
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return str(value).strip()
    return str(value).strip().casefold()


def score_results(
    labels: Iterable[dict[str, Any]],
    results: Iterable[dict[str, Any]],
    *,
    domain: str,
    scored_fields: Sequence[str] = SCORED_FIELDS,
) -> CanaryScore:
    """Compare ``results`` against ``labels``; pure, no I/O.

    Joined on ``match_id``. A label with no result is reported in
    ``missing_results`` and makes the run fail (see
    :attr:`CanaryScore.passed`); a result with no label is reported in
    ``unexpected_results`` and is NOT scored — the bar is defined over
    the labeled set, and quietly folding in unlabeled rows would let a
    canary improve its own score by fetching more.
    """
    labels_by_id = {str(row.get("match_id")): row for row in labels}
    results_by_id = {str(row.get("match_id")): row for row in results}

    counts = {name: [0, 0] for name in scored_fields}  # [compared, agreed]
    disagreements: list[dict[str, Any]] = []
    matched = 0
    for match_id, label in labels_by_id.items():
        result = results_by_id.get(match_id)
        if result is None:
            continue
        matched += 1
        for name in scored_fields:
            expected = _normalize(name, label.get(name))
            actual = _normalize(name, result.get(name))
            counts[name][0] += 1
            if expected == actual:
                counts[name][1] += 1
            else:
                disagreements.append(
                    {
                        "match_id": match_id,
                        "field": name,
                        "expected": label.get(name),
                        "actual": result.get(name),
                        "url": label.get("url"),
                    }
                )

    return CanaryScore(
        domain=domain,
        labels=len(labels_by_id),
        results=len(results_by_id),
        matched=matched,
        missing_results=tuple(
            sorted(set(labels_by_id) - set(results_by_id))
        ),
        unexpected_results=tuple(
            sorted(set(results_by_id) - set(labels_by_id))
        ),
        fields={
            name: FieldScore(field_name=name, compared=compared, agreed=agreed)
            for name, (compared, agreed) in counts.items()
        },
        disagreements=tuple(disagreements),
    )


def assert_owner_go(domain: str, owner_go: str | None) -> None:
    """Refuse a spending run without this domain's exact acknowledgment."""
    expected = owner_go_token(domain)
    if owner_go != expected:
        raise OwnerGateRefusal(
            f"live canary against {domain} refused: pass --owner-go {expected} "
            "to acknowledge that real requests will leave this host and real "
            "money will be spent. This step is an OWNER gate; no automated "
            "runner may supply it."
        )


def assert_budget(max_usd: Decimal) -> None:
    """Refuse a ceiling that authorizes nothing (or everything)."""
    if max_usd <= 0:
        raise OwnerGateRefusal(
            f"--max-usd must be greater than 0 (got {max_usd}); a canary with "
            "a zero or negative ceiling cannot fetch anything, and there is "
            "no 'unlimited' value by design."
        )


def build_parser() -> argparse.ArgumentParser:
    """The CLI.

    ONE flat command, not subcommands, and ``--max-usd`` is required on
    every invocation — including the offline one that cannot spend a
    cent. That is deliberate: this script's whole reason to exist is that
    it can spend money, and a ceiling that is only required "sometimes"
    is a ceiling somebody will eventually be missing on the invocation
    that mattered. Typing it costs one flag; forgetting it costs a
    merchant bill.

    ``--results`` selects the offline scoring path (compare an already
    captured results file against the labels). Without it the script is
    asking to fetch, which additionally needs ``--owner-go``.
    """
    parser = argparse.ArgumentParser(
        prog="run_domain_canary.py",
        description=(
            "Labeled domain canary. With --results it scores an existing "
            "results file offline and free; without it, it asks to fetch and "
            "refuses unless --owner-go carries this domain's exact token. "
            "--max-usd is required either way."
        ),
    )
    parser.add_argument(
        "--domain", required=True, help="bare competitor domain, e.g. noon.com"
    )
    parser.add_argument(
        "--labels",
        required=True,
        type=Path,
        help="labeled expected offers, JSON Lines "
        "(tests/fixtures/labeled_offers/<domain>.jsonl)",
    )
    # No default, and required on EVERY path -- see this function's docstring.
    parser.add_argument(
        "--max-usd",
        required=True,
        type=Decimal,
        help="hard spend ceiling in USD for this canary (REQUIRED, no default)",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="score this captured results JSONL offline instead of fetching",
    )
    parser.add_argument(
        "--owner-go",
        default=None,
        help="owner acknowledgment for a LIVE run: OWNER-GO:canary:<domain>",
    )
    parser.add_argument("--json-out", type=Path, default=None)
    return parser


def _print_score(score: CanaryScore) -> None:
    print(json.dumps(score.as_dict(), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.results is not None:
        # Offline, free, unattended. Nothing here can reach a merchant.
        labels = read_jsonl(args.labels)
        results = read_jsonl(args.results)
        score = score_results(labels, results, domain=args.domain)
        _print_score(score)
        if args.json_out is not None:
            args.json_out.write_text(
                json.dumps(score.as_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        return EXIT_PASS if score.passed else EXIT_BELOW_BAR

    # --- LIVE ------------------------------------------------------------
    try:
        assert_budget(args.max_usd)
        assert_owner_go(args.domain, args.owner_go)
    except OwnerGateRefusal as refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr)
        return EXIT_GATE_REFUSED

    labels = read_jsonl(args.labels)
    print(
        f"live canary authorized for {args.domain}: {len(labels)} labeled targets, "
        f"ceiling ${args.max_usd} USD",
        file=sys.stderr,
    )
    # The fetch loop is deliberately NOT implemented in this EPA run: the
    # step is a SPEND step and the run's pre-flight deferred all spend, so
    # shipping an untested live fetcher would be shipping the one part of
    # this script nobody has ever watched work. The owner runs the canary
    # through the ordinary dispatch path documented in
    # docs/ops/DOMAIN_STRATEGIES_2026-09.md, exports the results as JSONL,
    # and scores them with --results above -- which IS tested.
    print(
        "NOT IMPLEMENTED: the live fetch loop is deferred (EPA C4 step 2 is an "
        "owner-run SPEND step). Run the canary via the documented dispatch "
        "commands in docs/ops/DOMAIN_STRATEGIES_2026-09.md, export the results "
        "as JSONL, then score them with:\n"
        f"  run_domain_canary.py --domain {args.domain} --max-usd {args.max_usd} "
        f"--labels {args.labels} --results <results.jsonl>",
        file=sys.stderr,
    )
    return EXIT_GATE_REFUSED


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
