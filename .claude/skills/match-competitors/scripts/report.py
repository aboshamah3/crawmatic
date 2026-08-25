#!/usr/bin/env python3
"""Run statistics and (later) export of bulk-upsert payloads.

Usage:
  python3 report.py                    # stats
  python3 report.py --export-upsert    # write matching/export/upsert-XXX.json chunks
                                       # (high + medium 'matched' records only)
"""
import argparse
import json
import os
from collections import Counter

DATA_DIR = os.environ.get("MATCH_DATA_DIR", "/srv/crawmatic/matching")
MASTER = os.path.join(DATA_DIR, "matches.master.jsonl")
EXPORT_DIR = os.path.join(DATA_DIR, "export")
CHUNK = 200


def load():
    # MASTER is an append-only log: retry sweeps and validator passes re-append a full
    # corrected record for a product rather than rewriting the earlier line. Only the last
    # line for a product_id is current, so collapse before doing anything else. Without
    # this, a match the validator later REJECTED still exports: the corrective record
    # carries no competitor_url, so export()'s (product, competitor, url) dedup never
    # overwrites the stale matched row and the dead URL reaches prod.
    latest = {}
    if os.path.exists(MASTER):
        with open(MASTER, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    r = json.loads(line)
                    latest[r["product_id"]] = r
    return list(latest.values())


def stats(recs):
    conf = Counter()
    per_comp = Counter()
    status = Counter()
    for r in recs:
        for m in r.get("matches", []):
            conf[m.get("confidence", "?")] += 1
            status[m.get("status", "?")] += 1
            if m.get("status") == "matched":
                per_comp[m.get("competitor", "?")] += 1
    print(f"products with results: {len(recs)}")
    print(f"confidence: {dict(conf)}")
    print(f"status:     {dict(status)}")
    print(f"matched per competitor: {dict(per_comp)}")
    prices = [m["observed_price"] for r in recs for m in r.get("matches", [])
              if isinstance(m.get("observed_price"), (int, float))]
    print(f"observed prices captured: {len(prices)}")


def export(recs):
    os.makedirs(EXPORT_DIR, exist_ok=True)
    rows = []
    for r in recs:
        for m in r.get("matches", []):
            if m.get("status") != "matched" or m.get("confidence") not in ("high", "medium"):
                continue
            opts = dict(m.get("competitor_variant_options") or {})
            opts["confidence"] = m.get("confidence")
            rows.append({
                "variant_external_id": str(r["product_id"]),
                "variant_sku": r.get("sku") or None,
                "competitor_domain": m.get("competitor"),
                "competitor_url": m.get("competitor_url"),
                "competitor_variant_identifier": m.get("competitor_variant_identifier"),
                "external_title": m.get("external_title"),
                "competitor_variant_options": opts,
            })
    # last-wins dedup on (source, competitor, url) mirroring upsert arbiter
    dedup = {}
    for row in rows:
        dedup[(row["variant_external_id"], row["competitor_domain"], row["competitor_url"])] = row
    rows = list(dedup.values())
    n = 0
    for i in range(0, len(rows), CHUNK):
        n += 1
        path = os.path.join(EXPORT_DIR, f"upsert-{n:03d}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"matches": rows[i:i + CHUNK]}, f, ensure_ascii=False, indent=2)
    print(f"exported {len(rows)} matches into {n} chunk(s) under {EXPORT_DIR}")
    print("NOTE: upload is human-gated — map to the 005 bulk-upsert contract and POST manually.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export-upsert", action="store_true")
    args = ap.parse_args()
    recs = load()
    stats(recs)
    if args.export_upsert:
        export(recs)


if __name__ == "__main__":
    main()
