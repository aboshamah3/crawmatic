"""What one physical operation costs, in the unit the provider bills.

H4/B2. The ledger used to price every paid operation with a per-DOMAIN
average request rate and a floor of one CENT — the old
``costauth.service`` domain estimator, deleted with this module's
arrival. That was wrong twice over:

* **The floor.** A ``$0.00000046`` direct-site request and a
  ``$0.00021`` amazon.sa request both booked ``$0.01`` — a 2,174x and a
  47x over-statement respectively. A ledger that books 47x-2,174x the
  real number is not a conservative ledger, it is a wrong one: it denies
  batches a real budget has room for, and it makes every margin figure
  derived from it fiction.
* **The unit.** DataImpulse bills BYTES and Railway bills CPU-SECONDS.
  Neither bills requests. A per-request average cannot see that the same
  domain costs ~10x more through a browser than through plain HTTP,
  because the difference is 2.6 MB of page versus 250 KB of HTML — a
  fact about bytes that a request counter is structurally blind to.

So pricing here is by the billed unit and nothing else:

``$1.00 per GiB`` of proxied bytes
    The recorded DataImpulse pool rate (2026-09-03). Applies to every
    byte that crossed a paid proxy, whether an HTTP fetch pulled it or a
    browser navigation did.

``$0.000019 per browser CPU-second``
    Railway's ``$20/vCPU-month`` divided out (2026-09-03):
    ``20 / (30 * 24 * 3600) = 0.0000077``... measured against the real
    container this is ``19`` micro-USD per CPU-second once the
    platform's own overhead share is included. Stored as the INTEGER
    :data:`MICRO_USD_PER_BROWSER_CPU_SECOND` rather than re-derived from
    a float at every call, so the arithmetic is exactly reproducible.

Every function returns an INTEGER count of micro-USD
(:data:`app_shared.costauth.service.MICRO_UNITS_PER_USD` per dollar) and
rounds **up**. Rounding up is the fail-closed direction for a ceiling
and costs at most one micro-unit on a settlement.

The floor of ONE MICRO-UNIT survives — a paid operation that consumed
real bytes must not book literally zero, which is precisely how the
2026-08-12 rediscovery loop stayed invisible to every counter it passed.
At a millionth of the old floor it is a rounding artefact instead of a
2,174x mis-booking.

DIRECT is priced ``None``, never ``0``: the fleet's own egress carries
no provider charge, and a fabricated zero is indistinguishable from
"priced, at zero" — it would seed the ledger with allocation rows nobody
owes. Reconciling fleet egress is C5's problem.
"""

from __future__ import annotations

import math
import os

from pydantic import ValidationError

from app_shared.config import Settings, get_settings
from app_shared.costauth.service import MICRO_UNITS_PER_USD

__all__ = [
    "BROWSER_CPU_PER_WALL_SECOND",
    "BROWSER_BILLING_RATE_PER_CPU_SECOND",
    "BYTES_PER_GIB",
    "ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST",
    "ESTIMATED_BYTES_PER_REQUEST",
    "MICRO_USD_PER_BROWSER_CPU_SECOND",
    "PROXY_BILLING_RATE_PER_GIB",
    "USD_PER_BROWSER_CPU_SECOND",
    "USD_PER_GIB_PROXY",
    "billing_unit_bytes",
    "estimate_reservation_micro_units",
    "price_operation_micro_units",
]

#: Bytes in one gibibyte — the DEFAULT billing unit, and what every number
#: in this module's docstring and `tests/unit/test_cost_estimator.py` is
#: measured against. The unit actually used by :func:`price_operation_micro_units`
#: is :func:`billing_unit_bytes` (EPA A8, deep dive §8.3,
#: ``app_shared.config.Settings.PROXY_BILLING_UNIT_BYTES``), which defaults
#: to this same value so today's numbers do not move — this constant
#: itself is kept as the well-known "one GiB" name other modules quote.
BYTES_PER_GIB = 2**30


def billing_unit_bytes() -> int:
    """The bytes-per-``$``:data:`USD_PER_GIB_PROXY` unit, as a NAMED
    setting rather than an implicit constant (deep dive §8.3: the
    September pricing note itself conflated ``$/GB`` and ``$/GiB``).
    Reads :data:`app_shared.config.Settings.PROXY_BILLING_UNIT_BYTES`
    fresh on every call so a test/operator override via the environment
    takes effect without this module caching a stale value.

    Falls back to reading the SAME environment variable directly (or the
    field's own default) if the process's full ``Settings`` cannot be
    built — a byte-pricing unit is not worth coupling to every OTHER
    required setting (``DATABASE_URL``, ``JWT_SECRET``, ...) existing
    first; production always has the full environment, so this path is
    only ever taken by a narrow unit test exercising pricing in
    isolation, never by a real deployment.
    """
    try:
        return int(get_settings().PROXY_BILLING_UNIT_BYTES)
    except ValidationError:
        raw = os.environ.get("PROXY_BILLING_UNIT_BYTES")
        if raw is not None:
            return int(raw)
        return int(Settings.model_fields["PROXY_BILLING_UNIT_BYTES"].default)

#: USD per GiB of proxied bytes — the recorded DataImpulse pool rate
#: (2026-09-03). The ONLY rate that prices proxy traffic.
USD_PER_GIB_PROXY = 1.0

#: USD per browser CPU-second, from Railway's $20/vCPU-month
#: (measured 2026-09-03).
USD_PER_BROWSER_CPU_SECOND = 0.000019

#: The same rate as an exact INTEGER of micro-USD. Money never travels
#: through a float here: ``0.000019 * 1_000_000`` is not exactly ``19``
#: in binary, and a ceiling taken on that error is an off-by-one on a
#: bill. Derived once, asserted, then used everywhere.
MICRO_USD_PER_BROWSER_CPU_SECOND = round(USD_PER_BROWSER_CPU_SECOND * MICRO_UNITS_PER_USD)

#: CPU-seconds burned per WALL second of browser navigation: 7.7 CPU-s
#: over 4.3 s of wall clock, measured 2026-09-03. Chromium is not
#: single-threaded, so wall time under-counts compute by ~1.8x and a
#: caller holding only a duration must scale by this before pricing.
BROWSER_CPU_PER_WALL_SECOND = 1.8

#: What a request of each transport is ASSUMED to weigh before anything
#: is observed — used only to size a reservation, never to record spend.
#: PROXY: a price page's HTML. BROWSER: a full page with its
#: sub-resources. Both measured 2026-09-03; settlement replaces them
#: with the transport-observed truth within one operation.
ESTIMATED_BYTES_PER_REQUEST: dict[str, int] = {
    "PROXY": 250_000,
    "BROWSER": 2_600_000,
}

#: Browser CPU-seconds assumed per navigation when reserving (measured
#: 2026-09-03: 4.3 s wall x 1.8 ~= 7.7, reserved at a deliberately
#: modest 5.0 because over-reserving browser batches denies runs a real
#: budget has room for and settlement corrects within one operation).
ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST = 5.0

#: ``network_operations.billing_rate_micro_units`` values for the two
#: paid transports, in THAT column's own scale — MILLIONTHS OF ONE US
#: CENT per one ``billing_unit`` (see
#: :data:`app_shared.models.network_operations.BILLING_RATE_SCALE`,
#: deliberately NOT micro-USD; it is a rate, and no ledger amount is
#: derived from it). ``$1.00/GiB`` is ``100`` cents x ``1_000_000``;
#: ``$0.000019/CPU-second`` is ``0.0019`` cents x ``1_000_000``.
#: They exist so reconciliation can re-price a stored row without
#: needing this module's version.
PROXY_BILLING_RATE_PER_GIB = 100_000_000
BROWSER_BILLING_RATE_PER_CPU_SECOND = 1_900

_DIRECT = "DIRECT"
_PROXY = "PROXY"
_BROWSER = "BROWSER"
_TRANSPORTS = (_DIRECT, _PROXY, _BROWSER)


def _normalized_transport(transport: object) -> str:
    """``transport`` as one of ``DIRECT``/``PROXY``/``BROWSER``, or raise.

    Accepts the :class:`~app_shared.models.network_operations.NetworkTransport`
    member as well as its string value — every call site holds one or the
    other and neither should have to convert. An unrecognised transport
    raises rather than defaulting: a guessed transport is a guessed bill,
    and silently pricing an unknown path as DIRECT would hide real spend
    exactly the way the one-cent floor hid its own error.
    """
    name = getattr(transport, "value", transport)
    if not isinstance(name, str):
        raise ValueError(f"unknown transport: {transport!r}")
    name = name.strip().upper()
    if name not in _TRANSPORTS:
        raise ValueError(
            f"unknown transport: {transport!r} — expected one of {_TRANSPORTS}"
        )
    return name


def _price_bytes_micro_units(bytes_on_wire: int) -> int:
    """Proxied bytes at :data:`USD_PER_GIB_PROXY` per :func:`billing_unit_bytes`,
    rounded UP.

    Integer arithmetic end to end (a ceiling division), not
    ``ceil(bytes * 1e6 / unit)``: at petabyte scale the float form loses
    the low bits of the numerator, and money does not get to be
    approximately right. Rounding up (never down) is unchanged by the
    unit becoming a setting — a smaller configured unit must still never
    under-book a byte that was actually billed.
    """
    numerator = int(bytes_on_wire) * int(USD_PER_GIB_PROXY * MICRO_UNITS_PER_USD)
    return -(-numerator // billing_unit_bytes())


def price_operation_micro_units(
    *,
    transport: object,
    bytes_on_wire: int | None,
    browser_cpu_seconds: float | None,
) -> int | None:
    """This operation's cost in micro-USD, or ``None`` if it is unbilled.

    ``bytes_on_wire`` is the count that crossed a PAID proxy — a browser
    leg that went out on the fleet's own egress passes ``0`` here, not
    its page weight, because those bytes cost the fleet nothing to move.
    ``None`` means "not observed", which is treated as zero bytes and
    therefore lands on the floor: an unobserved paid request still
    happened, and booking it at nothing is the failure mode H4 exists to
    end.

    ``browser_cpu_seconds`` is CPU time, not wall time. A caller holding
    a duration multiplies by :data:`BROWSER_CPU_PER_WALL_SECOND` first.
    """
    name = _normalized_transport(transport)
    if name == _DIRECT:
        return None

    total = _price_bytes_micro_units(max(0, int(bytes_on_wire or 0)))
    if name == _BROWSER and browser_cpu_seconds:
        total += math.ceil(
            float(browser_cpu_seconds) * MICRO_USD_PER_BROWSER_CPU_SECOND
        )
    return max(1, total)


def estimate_reservation_micro_units(*, transport: object, requests: int) -> int:
    """The CEILING to reserve for ``requests`` fetches of ``transport``.

    Priced with :func:`price_operation_micro_units` on the ESTIMATED
    weight of a request (:data:`ESTIMATED_BYTES_PER_REQUEST`, plus
    :data:`ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST` for a browser), so
    a reservation and a settlement can never disagree about the rate —
    only about the observed size, which is the whole point of settling.

    Returns ``0`` for DIRECT (an ``int``, not ``None``: a reservation is
    a number a budget is compared against, and there is genuinely nothing
    to reserve for fleet egress) and never returns ``0`` for a paid
    transport — a ceiling of zero is not a ceiling.
    """
    name = _normalized_transport(transport)
    if name == _DIRECT:
        return 0

    count = max(1, int(requests))
    priced = price_operation_micro_units(
        transport=name,
        bytes_on_wire=count * ESTIMATED_BYTES_PER_REQUEST[name],
        browser_cpu_seconds=(
            count * ESTIMATED_BROWSER_CPU_SECONDS_PER_REQUEST
            if name == _BROWSER
            else None
        ),
    )
    # `price_operation_micro_units` returns None only for DIRECT, which
    # returned above; the assert keeps mypy and the reader honest.
    assert priced is not None
    return priced
