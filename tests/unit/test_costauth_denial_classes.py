"""Every cost-authorization denial reason is explicitly permanent or transient (2026-09-29, E4).

The dispatcher terminalizes a batch at once on a PERMANENT denial and
retries a TRANSIENT one for a bounded window. A reason added to
`DenialReason` without a class must fail here, not default silently into
either behaviour -- defaulting to "transient" is how the daily canary spent
12 hours retrying a denial that could never clear.
"""

from __future__ import annotations

from app_shared.costauth.service import DENIAL_PERMANENCE, DenialPermanence, DenialReason
from app_shared.enums import ScrapeErrorCode


def test_every_reason_is_classified() -> None:
    assert set(DENIAL_PERMANENCE) == set(DenialReason)
    assert all(isinstance(v, DenialPermanence) for v in DENIAL_PERMANENCE.values())


def test_domain_state_denials_are_permanent() -> None:
    for reason in (
        DenialReason.DOMAIN_NOT_CERTIFIED,
        DenialReason.DOMAIN_QUARANTINED,
        DenialReason.DOMAIN_ESCALATION_DENIED,
    ):
        assert DENIAL_PERMANENCE[reason] is DenialPermanence.PERMANENT


def test_fleet_and_budget_denials_are_transient() -> None:
    for reason in (
        DenialReason.BREAKER_OPEN,
        DenialReason.BREAKER_EVIDENCE_STALE,
        DenialReason.CONCURRENCY_CAP_EXCEEDED,
        DenialReason.MONEY_BUDGET_EXCEEDED,
        DenialReason.ENTITLEMENT_INACTIVE,
    ):
        assert DENIAL_PERMANENCE[reason] is DenialPermanence.TRANSIENT


def test_every_reason_has_a_target_error_code_of_the_same_name() -> None:
    for reason in DenialReason:
        assert ScrapeErrorCode(reason.value).value == reason.value
        assert len(reason.value) <= 32  # scrape_job_targets.error_code is VARCHAR(32)
