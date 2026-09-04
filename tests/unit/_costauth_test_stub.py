"""Stub the EPA C3 cost-authorization gate for DB-free dispatch unit tests.

``dispatch_job`` / ``recover_stalled_batches`` / the thin fallback task all
authorize before they POST (EPA C3, READY-006). Authorization is a real
database transaction — it locks budget rows, reads the durable breaker and
entitlement evidence, and consults C2's domain-state table — so the
fake-session, no-Postgres unit tests that assert *dispatch* semantics
(EPA B1's identities, B2's stamping, the replay matrix) cannot run it.

Those tests are about a different question, and this helper keeps them
about it: it replaces the gate with an always-grant stub so the assertions
below it continue to describe identity and stamping behaviour rather than
budget behaviour. The gate's OWN behaviour — every denial reason, the
concurrent hard ceiling, the lease/ledger interaction, settlement
idempotence — is tested against a real Postgres in
``tests/integration/test_cost_authorization.py``, which is where a claim
about a database belongs.

Stubbing here is therefore a division of labour, not a coverage hole. The
one thing it must not do is hide a *missing* gate, so
:func:`assert_gate_is_wired` exists: it fails loudly if the module it is
handed has no gate to stub, which is what would happen if someone deleted
the authorization call to make a test pass.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _fake_grant() -> SimpleNamespace:
    """A grant shaped like ``AuthorizationGrant`` — enough for a call site.

    Every field the real dataclass has, including the three decision facts
    a call site forwards to the spider (EPA Phase C F3). A stub missing a
    field a call site reads would fail as an ``AttributeError`` in a test
    that is not about authorization at all, which teaches nothing.
    """
    return SimpleNamespace(
        authorization_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        budget_decision_version="stub",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=900),
        reserved_cost_micro_units=0,
        reserved_bytes=0,
        reserved_requests=1,
        reserved_browser_seconds=0,
        currency="USD",
        replayed=False,
        entitlement_version="stub-entitlement",
        breaker_decision="CLOSED",
    )


class AlwaysGrantCostAuthorizationService:
    """Grants everything; records nothing durable.

    ``release``/``settle``/``settle_partial``/``heartbeat`` are accepted
    and recorded so a call site's error path still runs to completion
    under test — and so a test CAN assert that the hold was returned.
    """

    #: Set on a subclass (or an instance) to deny instead of granting.
    #: Carries the :class:`DenialReason` value the fake denial reports.
    deny_reason: str | None = None

    def __init__(self, *args, **kwargs) -> None:
        self.released: list[uuid.UUID] = []
        self.settled: list[uuid.UUID] = []
        self.granted: list[uuid.UUID] = []

    def authorize(self, req):  # noqa: ANN001 - a stub mirrors the real signature
        if self.deny_reason is not None:
            from app_shared.costauth import CostAuthorizationDenied

            raise CostAuthorizationDenied(
                self.deny_reason, "denied by _costauth_test_stub"
            )
        grant = _fake_grant()
        self.granted.append(grant.authorization_id)
        return grant

    def heartbeat(self, authorization_id) -> None:  # noqa: ANN001
        return None

    def settle(self, authorization_id, actual=None) -> None:  # noqa: ANN001
        self.settled.append(authorization_id)

    def settle_partial(self, authorization_id, delta=None) -> None:  # noqa: ANN001
        self.settled.append(authorization_id)

    def release(self, authorization_id, **_kwargs) -> None:  # noqa: ANN001
        self.released.append(authorization_id)


class DenyingCostAuthorizationService(AlwaysGrantCostAuthorizationService):
    """Denies every request — the other half of every call site's contract.

    A denial is not an error condition to be smoke-tested; it is the
    behaviour the whole C3 gate exists to produce, and each call site has
    its own correct response to it (skip the batch and leave the targets
    untouched, leave the batch stalled, raise, fail the run, 402). EPA
    Phase C F6: not one of those responses had a test, because every
    dispatch test stubbed the gate to always grant.
    """

    deny_reason = "MONEY_BUDGET_EXCEEDED"


def assert_gate_is_wired(module) -> None:  # noqa: ANN001
    """Fail unless ``module`` actually has a C3 gate to stub.

    Without this, deleting the authorization call from a dispatch site
    would make these tests pass *more* easily — the stub would simply have
    nothing to replace. The check turns that into a loud failure.
    """
    missing = [
        name
        for name in ("CostAuthorizationService",)
        if not hasattr(module, name)
    ]
    if missing:
        raise AssertionError(
            f"{module.__name__} has no C3 cost-authorization gate to stub "
            f"(missing {missing}). A paid dispatch site must authorize before "
            "it POSTs — see app_shared.costauth.service."
        )


def stub_cost_authorization(module, service_class=None) -> None:  # noqa: ANN001
    """Point ``module``'s C3 gate at a stub, in place.

    ``service_class`` defaults to :class:`AlwaysGrantCostAuthorizationService`;
    pass :class:`DenyingCostAuthorizationService` (or any class with the
    same surface) to exercise the denial side. ``authorize_or_none`` is
    re-pointed at a faithful copy of the real helper — a denial becomes
    ``None``, which is what the background call sites branch on — rather
    than at a passthrough that could never return ``None``.
    """
    assert_gate_is_wired(module)
    module.CostAuthorizationService = (
        service_class or AlwaysGrantCostAuthorizationService
    )
    if hasattr(module, "authorize_or_none"):
        module.authorize_or_none = _authorize_or_none_stub


def _authorize_or_none_stub(service, req, *, site):  # noqa: ANN001
    from app_shared.costauth import CostAuthorizationDenied

    try:
        return service.authorize(req)
    except CostAuthorizationDenied:
        return None
