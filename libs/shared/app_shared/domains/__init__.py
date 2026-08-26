"""The domain certification lifecycle (EPA C2 + W4.1).

The authorization service (C3) must consult enforced domain state before
allowing paid or expensive work against a competitor domain. Two modules,
in the order they were built:

* :mod:`app_shared.domains.state_lookup` (C2, READY-004/READY-006
  critical path) — the READ side and the sole reader of
  ``DomainPlaybook.state``: the cached ``get_domain_state`` lookup plus
  ``authorization_rules_for_state``, the deny-by-state rule table C3
  consumes. The state enum itself lives on
  ``app_shared.models.domain_playbooks``.
* :mod:`app_shared.domains.lifecycle` (W4.1) — the WRITE side C2
  deferred: the legal-edge state machine, the approval gate (derived
  from the rule table above, never restated), and the append-only
  ``domain_lifecycle_audit`` trail. ``scripts/domain_lifecycle.py`` is
  its operator-facing caller.

An admin UI over that audit trail is the one piece still outstanding; a
``POST /v1/domains/{domain}/transitions`` route would wrap the same
``transition()`` call the CLI already makes.
"""

from __future__ import annotations

__all__: list[str] = []
