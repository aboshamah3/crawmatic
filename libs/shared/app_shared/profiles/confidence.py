"""DB-tunable default confidences (`contracts/confidence-defaults.md`, SPEC-06 US4 T041, FR-011).

The §17 per-method default confidences plus the documented minimum-
accepted and promotion-threshold constants. The extractor (SPEC-07+)
reads confidences through :func:`resolve_confidence_rules`, never
hardcoded literals — this module *is* the single source of truth for
the fallback values, with a profile's own (already `[0,1]`-validated)
`confidence_rules` JSONB bundle as the DB-tunable override surface.

Pure Python, no DB/I/O/FastAPI — safe to import from `apps/api` and
unit tests alike.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# §17 verbatim per-method default confidences.
DEFAULT_CONFIDENCE_RULES: dict[str, float] = {
    "platform_variant_json": 0.95,
    "jsonld": 0.95,
    "embedded_json": 0.90,
    "css": 0.85,
    "xpath": 0.85,
    "regex": 0.75,
    "playwright": 0.80,
    "single_number": 0.40,  # "reject by default" — the reject decision is SPEC-07's, not here
}

# The documented minimum-accepted confidence and promotion threshold (§17).
DEFAULT_MIN_ACCEPTED_CONFIDENCE = 0.75
DEFAULT_PROMOTION_THRESHOLD = 0.85


def resolve_confidence_rules(profile_rules: Mapping[str, Any] | None) -> dict[str, Any]:
    """Overlay a profile's validated ``confidence_rules`` over the §17 defaults.

    Returns ``DEFAULT_CONFIDENCE_RULES`` plus ``min_accepted_confidence``
    and ``promotion_threshold``, each overridden by the corresponding key
    in ``profile_rules`` when present (FR-011). A profile that omits a
    key falls back to the documented default. Unknown keys in
    ``profile_rules`` (forward-compat extraction methods not yet in
    ``DEFAULT_CONFIDENCE_RULES``) are passed through unchanged rather
    than dropped, since `validate_confidence_rules` already constrained
    every present value to ``[0, 1]`` at write time and this accessor is
    not the place to re-litigate shape.

    Never mutates ``profile_rules`` (or the module-level defaults).
    """
    resolved: dict[str, Any] = dict(DEFAULT_CONFIDENCE_RULES)
    resolved["min_accepted_confidence"] = DEFAULT_MIN_ACCEPTED_CONFIDENCE
    resolved["promotion_threshold"] = DEFAULT_PROMOTION_THRESHOLD

    if profile_rules:
        resolved.update(profile_rules)

    return resolved
