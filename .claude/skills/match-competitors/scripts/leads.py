#!/usr/bin/env python3
"""Pre-grep every batch-15 row's identifier tokens against the local competitor
sitemap indexes. Produces leads.json: {pid: {site: [urls]}} plus tokens used.

Sites with zero hits across their COMPLETE catalogue are evidenced gaps the
matcher agent never has to fetch."""
import json, re, os, sys, unicodedata

SCRATCH = os.path.dirname(os.path.abspath(__file__))
SM = os.path.join(SCRATCH, 'sitemaps')
BATCH = '/tmp/claude-1000/-srv-crawmatic/97b7b3b3-8e9e-4a69-ba5a-0eeddf1b0a1b/scratchpad/todo.json'

SITES = {                      # site key -> .urls file (complete local catalogue)
    'jarir.com':        'jarir.urls',
    'pcpalace.com.sa':  'pcpalace.urls',
    'afaqalhasoob.com': 'afaq.urls',
    'alshamel.sa':      'alshamel.urls',
    'stech.ink':        'stech.urls',
    'fqtoners.com':     'fqtoners.urls',
    'rowadalahbar.com': 'rowadalahbar.urls',
    'rawand.com.sa':    'rawand.urls',
    'amwajest.com':     'amwajest.urls',
    'ahbarhd.com':      'ahbarhd.urls',
}

# spec/capacity noise that matches cables, cases and unrelated SKUs
NOISE = re.compile(r'^(?:\d{1,4}(?:gb|tb|mb|w|watt|mm|hz|mhz|ghz|k|p|v|in|inch|bit|pin|cl\d*)?|'
                   r'ddr[345]|usb\d?|type-?c|hdmi\d?|pcie|atx|rgb|argb|led|lcd|ssd|hdd|nvme|'
                   r'm\.?2|3\.0|3\.1|2\.0|5\.0|4k|8k|80|1080p|1440p|60|120|144|165|240|360|420)$',
                  re.I)
VENDOR_PREFIX = re.compile(r'^(?:CRS|UGR|ESR|OMS|LEX|KLV|SAM|MRV|PRT|BOU|HP|LNQ)-', re.I)


def tokens(p):
    out = []
    raw = []
    sku = (p.get('sku') or '').strip()
    if sku:
        raw.append(sku)
        stripped = VENDOR_PREFIX.sub('', sku)
        raw.append(stripped)
        raw += re.split(r'[-_/\s]+', stripped)
    raw += p.get('mpn_candidates') or []
    text = ' '.join(filter(None, [p.get('english_title'), p.get('name')]))
    raw += re.findall(r'[A-Za-z0-9][A-Za-z0-9\.\-]{2,14}', text)
    for t in raw:
        t = t.strip('.-_').strip()
        if len(t) < 3 or len(t) > 20:
            continue
        if NOISE.match(t):
            continue
        has_d = any(c.isdigit() for c in t)
        has_a = any(c.isalpha() for c in t)
        if has_d and has_a:            # RM750e, 3500X, B0CQ, NM610PRO
            out.append(t)
        elif has_d and len(t) >= 5:    # 9020279, 20204, 90798
            out.append(t)
    seen, uniq = set(), []
    for t in out:
        k = t.lower()
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq[:12]


import bisect

JUNK = re.compile(r'cdn\.salla\.sa|\.(jpg|jpeg|png|webp|svg|gif)(\?|$)|/c\d{6,}$|/blog/|/category|/collections/')


def build_index(lines):
    """token -> set(url).  Tokens are alnum runs of the lowercased URL, so a match is
    delimiter-bounded (kills the ISBN/'5400520204257' class of false hits)."""
    idx = {}
    keep = []
    for u in lines:
        if not u or JUNK.search(u):
            continue
        keep.append(u)
        for t in re.findall(r'[a-z0-9]+', u.split('://', 1)[-1]):
            if len(t) >= 3:
                idx.setdefault(t, set()).add(u)
    return idx, sorted(idx)


def lookup(idx, keys, tok):
    """exact token match, plus URL-tokens that START with ours (ugreen '35605' vs
    slug '35605b'). Prefix mode only for tokens >= 5 chars, to stay specific."""
    out = set(idx.get(tok, ()))
    if len(tok) >= 5:
        i = bisect.bisect_left(keys, tok)
        while i < len(keys) and keys[i].startswith(tok):
            out |= idx[keys[i]]
            i += 1
    return out


def main():
    urls = {}
    for site, fn in SITES.items():
        path = os.path.join(SM, fn)
        lines = open(path, encoding='utf-8', errors='ignore').read().lower().splitlines() \
            if os.path.exists(path) else []
        urls[site] = build_index(lines)
    products = json.load(open(BATCH, encoding='utf-8'))['products']
    leads = {}
    for p in products:
        tk = tokens(p)
        per = {}
        for site, (idx, keys) in urls.items():
            hits = {}
            for t in tk:
                for u in lookup(idx, keys, t.lower()):
                    hits.setdefault(u, t)
            if hits:
                per[site] = [f"{u}  [tok:{t}]" for u, t in sorted(hits.items())[:10]]
        leads[str(p['product_id'])] = {'tokens': tk, 'hits': per,
                                       'clean_sites': [s for s in SITES if s not in per]}
    json.dump(leads, open(os.path.join(SCRATCH, 'leads.json'), 'w'), ensure_ascii=False, indent=1)
    # summary
    from collections import Counter
    c = Counter()
    for v in leads.values():
        for s in v['hits']:
            c[s] += 1
    print('products:', len(leads))
    for s in SITES:
        print(f'  {s:20s} rows with local hits: {c[s]:3d}  (clean gaps: {len(leads)-c[s]})')
    notok = [k for k, v in leads.items() if not v['tokens']]
    print('rows with no usable token:', len(notok), notok[:20])


if __name__ == '__main__':
    main()
