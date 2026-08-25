#!/usr/bin/env python3
"""URL safety gate + corpus-wide duplicate detection over matching results.

Usage:  python3 validate_urls.py [--batch N]        # default: whole master corpus

Checks (lightweight mirror of libs/shared url_safety intent — the real
validate_competitor_url still runs at bulk-upsert time):
  - https only, no userinfo, no port, no IP-literal host
  - host must belong to amazon.sa / noon.com / jarir.com
Normalization for dedup:
  - amazon: https://www.amazon.sa/dp/{ASIN}
  - noon:   scheme+host+path, query stripped
  - jarir:  scheme+host+path, query stripped
Duplicates: same (competitor, normalized_url) claimed by 2+ distinct products
-> appended to needs_human_review.json (deduped).
"""
import argparse
import ipaddress
import json
import os
import re
import sys
from urllib.parse import urlsplit

DATA_DIR = os.environ.get("MATCH_DATA_DIR", "/srv/crawmatic/matching")
MASTER = os.path.join(DATA_DIR, "matches.master.jsonl")
REVIEW = os.path.join(DATA_DIR, "needs_human_review.json")

def load_competitors():
    path = os.path.join(DATA_DIR, "competitors.json")
    if not os.path.exists(path):
        sys.exit(f"missing {path}")
    raw = json.load(open(path, encoding="utf-8"))
    domains = []
    for entry in (raw if isinstance(raw, list) else raw.get("competitors", [])):
        url = entry if isinstance(entry, str) else entry.get("url", "")
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        host = host[4:] if host.startswith("www.") else host
        if host and host not in domains:
            domains.append(host)
    return domains


COMPETITOR_DOMAINS = None  # loaded in main()
ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})")


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def check_and_normalize(competitor, url):
    """Return (normalized_url, None) or (None, reason)."""
    try:
        u = urlsplit(url)
    except ValueError as e:
        return None, f"unparseable: {e}"
    if u.scheme != "https":
        return None, "not https"
    if u.username or u.password:
        return None, "userinfo in URL"
    host = (u.hostname or "").lower()
    if u.port:
        return None, "explicit port"
    try:
        ipaddress.ip_address(host)
        return None, "IP-literal host"
    except ValueError:
        pass
    dom = competitor.lower()
    dom = dom[4:] if dom.startswith("www.") else dom
    if not (host == dom or host.endswith("." + dom)):
        return None, f"host {host} not valid for {competitor}"
    if dom not in (COMPETITOR_DOMAINS or []):
        return None, f"competitor {competitor} not in competitors.json"
    if "amazon." in host:
        m = ASIN_RE.search(u.path)
        if not m:
            return None, "amazon URL without /dp/ASIN"
        return f"https://{host}/dp/{m.group(1)}", None
    return f"https://{host}{u.path}", None


def load_records(batch=None):
    if not os.path.exists(MASTER):
        sys.exit(f"no results yet ({MASTER} missing)")
    recs = []
    with open(MASTER, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if batch is None or r.get("batch_id") == batch:
                recs.append(r)
    return recs


def main():
    global COMPETITOR_DOMAINS
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int)
    args = ap.parse_args()

    COMPETITOR_DOMAINS = load_competitors()
    recs = load_records(args.batch)
    rejects, seen_urls = [], {}
    checked = 0
    for r in recs:
        for m in r.get("matches", []):
            url = m.get("competitor_url")
            if m.get("status") != "matched" or not url:
                continue
            checked += 1
            norm, err = check_and_normalize(m.get("competitor", ""), url)
            if err:
                rejects.append({"product_id": r["product_id"], "competitor": m.get("competitor"),
                                "url": url, "reason": f"url_gate: {err}"})
                continue
            seen_urls.setdefault((m.get("competitor"), norm), set()).add(r["product_id"])

    dupes = [{"competitor": c, "normalized_url": u, "product_ids": sorted(pids),
              "reason": "same competitor URL claimed by multiple products"}
             for (c, u), pids in seen_urls.items() if len(pids) > 1]

    if rejects or dupes:
        review = json.load(open(REVIEW, encoding="utf-8")) if os.path.exists(REVIEW) else []
        existing = {json.dumps(x, sort_keys=True, ensure_ascii=False) for x in review}
        added = 0
        for item in rejects + dupes:
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
            if key not in existing:
                review.append(item)
                existing.add(key)
                added += 1
        atomic_write(REVIEW, review)
        print(f"flagged {added} new review items ({len(rejects)} URL rejects, {len(dupes)} duplicate groups)")
    scope = f"batch {args.batch}" if args.batch else "full corpus"
    print(f"{scope}: {checked} matched URLs checked, {len(rejects)} rejected, {len(dupes)} duplicate URL groups")
    for x in (rejects + dupes)[:20]:
        print(" ", json.dumps(x, ensure_ascii=False))


if __name__ == "__main__":
    main()
