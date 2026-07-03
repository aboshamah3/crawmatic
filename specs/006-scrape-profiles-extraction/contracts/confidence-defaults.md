# Contract: confidence defaults (`app_shared/profiles/confidence.py`)

DB-tunable default confidences (§17, FR-011) — the extractor (SPEC-07+) reads confidences through this accessor, never hardcoded literals.

## Constants (§17 verbatim)

```python
DEFAULT_CONFIDENCE_RULES: dict[str, float] = {
    "platform_variant_json": 0.95,
    "jsonld": 0.95,
    "embedded_json": 0.90,
    "css": 0.85,
    "xpath": 0.85,
    "regex": 0.75,
    "playwright": 0.80,
    "single_number": 0.40,   # "reject by default" — the reject decision is SPEC-07's, not here
}
DEFAULT_MIN_ACCEPTED_CONFIDENCE = 0.75
DEFAULT_PROMOTION_THRESHOLD = 0.85
```

## `resolve_confidence_rules(profile_rules: Mapping | None) -> dict`

Returns the effective confidence config: `DEFAULT_CONFIDENCE_RULES` + `{min_accepted, promotion_threshold}` overlaid with any values present in the profile's (already `[0,1]`-validated) `confidence_rules`. A profile that omits a value falls back to the documented default (FR-011, SC / US4 scenario 2). Never mutates the input.

## Rules

- Constants are the **fallback**; the per-profile `confidence_rules` (DB) is the tuning surface and wins when present.
- Keys align with the extraction methods of §16/§17.

## Tests (unit, no DB)

- Each `DEFAULT_*` value equals the §17 figure.
- `resolve_confidence_rules(None)` == defaults.
- A partial override replaces only the given keys; unspecified keys keep defaults.
- Input mapping is not mutated.
