# Offer-observation evidence retention (W3.1 minimal policy)

`OfferObservation.raw_evidence_hash` is only meaningful if the bytes it
names still exist and still hash to that name. This document is the
retention policy `evidence_store.py` mechanically enforces; the full
W5.5 hardening pass (expiry sweeps, off-box archival, size budgets,
per-workspace quotas) is explicitly **out of scope** here — this is the
minimal-but-real version the contract needs to ship with.

## What is stored

The raw bytes an extraction strategy read the offer facts from —
typically the HTML/JSON fragment (or full response body) a parser
matched against — content-addressed by `sha256(bytes)`. The same hash
is what `OfferObservation.raw_evidence_hash` carries, so "resolve the
hash" and "replay the evidence" are the same operation
(`evidence_store.replay`).

## Storage layout

Local filesystem, fan-out by the first four hex characters of the hash
(`<store_dir>/<hh>/<hh>/<full_hash>`), mirroring content-addressed
stores like Git's object database. Writes are atomic (temp file +
rename) so a concurrent reader never observes a partial object, and
writes are idempotent — storing the same bytes twice is a no-op, which
matters because a retried extraction attempt may re-derive identical
evidence.

The store root defaults to `./var/evidence`, overridable via the
`OFFER_EVIDENCE_STORE_DIR` environment variable. Production placement
(a durable volume, object storage, etc.) is a deployment decision for a
later phase — this module's contract is the content-addressing and
integrity check, not the backing filesystem.

## Retention rules (minimal, real)

1. **Write-once, read-many.** An evidence object is never mutated in
   place. A corrected/re-extracted observation gets a NEW hash (new
   bytes hash to a different name); the old object is not overwritten.
2. **No silent deletion.** Nothing in this module deletes evidence.
   Until a retention-by-age sweep ships (W5.5), evidence accumulates —
   acceptable for a minimal policy because the alternative (deleting
   evidence with no expiry mechanism at all) breaks the "a hash proves
   something" invariant this contract exists to guarantee.
3. **Integrity is checked on every read, not just on write.**
   `resolve_hash`/`replay` re-hash the bytes they read back and raise
   `EvidenceIntegrityError` if the content no longer matches its own
   filename — a resolvable-but-wrong hash is treated as a store defect,
   never silently returned.
4. **A missing hash is a distinct failure from a corrupted one.**
   `EvidenceNotFoundError` (never written, or already swept by a future
   retention job) vs. `EvidenceIntegrityError` (written, but the bytes
   on disk no longer match) — a caller (audit tooling, a dispute
   investigation) needs to tell these apart.

## Replay

```python
from pathlib import Path
from app_shared.observations.evidence_store import store_evidence, replay

evidence_hash = store_evidence(raw_html_bytes, store_dir=Path("var/evidence"))
# ... later, potentially in a different process ...
result = replay(evidence_hash, store_dir=Path("var/evidence"))
assert result.data == raw_html_bytes
assert result.verified
```

CLI form (for manual/audit use):

```sh
uv run python -m app_shared.observations.evidence_store replay <hash> \
    --store-dir var/evidence --out /tmp/replayed.html
```

## What W5.5 adds later

- A retention-by-age sweep (drop evidence past a configurable window),
  paired with the retention-by-drop tolerance the observation tables
  already assume for soft references (`contracts/models-observations.md`
  §22).
- Off-box/durable storage instead of local filesystem.
- A capability flag gating which callers (e.g. the SaaS promotion UI)
  may trigger a replay, since replay reads raw scraped content that may
  include more than the fields `OfferObservation` exposes.
