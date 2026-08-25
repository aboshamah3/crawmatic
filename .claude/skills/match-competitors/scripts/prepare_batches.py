#!/usr/bin/env python3
"""One-time prep -> /srv/crawmatic/matching/{batches,state.json}.

Two source modes:
  default        : mushtryati_products.json (WooCommerce export)
  --xlsx <path>  : the merchant's Excel inventory sheet (قائمه الاسعار والكميات) —
                   parses numbered item rows (A=no, B=code, C=name) grouped under
                   section-header rows; quantities/prices in the sheet are ignored.

Deterministic. Safe to re-run only before the run starts (refuses if any batch
is already completed/in progress unless --force).
"""
import json
import os
import re
import sys
import unicodedata
from urllib.parse import unquote

PRODUCTS = os.environ.get("MATCH_PRODUCTS", "/srv/crawmatic/mushtryati_products.json")
DATA_DIR = os.environ.get("MATCH_DATA_DIR", "/srv/crawmatic/matching")

BATCH_SIZE = {"A": 200, "B": 200, "C": 200}  # merchant wants steady 200-product batches


def load_competitors():
    """Competitor domains come from matching/competitors.json (the merchant's list)."""
    path = os.path.join(DATA_DIR, "competitors.json")
    if not os.path.exists(path):
        sys.exit(f"missing {path} — create it first (a JSON array of competitor "
                 f"domains/URLs, from the merchant's 14-link Excel)")
    raw = json.load(open(path, encoding="utf-8"))
    domains = []
    for entry in (raw if isinstance(raw, list) else raw.get("competitors", [])):
        url = entry if isinstance(entry, str) else entry.get("url", "")
        host = re.sub(r"^https?://", "", url).split("/")[0].lower()
        host = host[4:] if host.startswith("www.") else host
        if host and host not in domains:
            domains.append(host)
    if not domains:
        sys.exit(f"{path} contains no usable domains")
    return domains


COMPETITORS = None  # resolved in main() via load_competitors()

TIER_C_KEYWORDS = [
    "لابتوب", "جوال", "جولات", "تابلت", "لوحي", "ايباد", "آيباد", "ايفون", "آيفون",
    "ماك بوك", "كمبيوتر محمول", "بلايستيشن", "اكس بوكس", "كونسول", "ساعات ذكية",
]
# Ink/toner/paper only — accessories stay Tier B (too many real hardware items
# carry the ملحقات/اكسسوارات category as a secondary tag).
TIER_A_KEYWORDS = ["احبار", "أحبار", "حبر", "خرطوش", "ورق تصوير"]


def atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def is_english(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    return sum(c.isascii() for c in letters) / len(letters) > 0.6


def strip_html(text):
    return re.sub(r"<[^>]+>", " ", text or "")


MPN_RE = re.compile(r"\b[A-Za-z0-9]{2,6}-?[A-Za-z0-9]{2,10}\b")
MPN_JUNK = {"963xl", "wifi", "wi-fi", "usb", "hdmi", "ddr4", "ddr5", "5g", "4g", "hd", "uhd", "4k", "2k"}


def mpn_candidates(p):
    """Ordered, deduped hard-identifier candidates from sku, slug and name."""
    out = []
    sku = (p.get("sku") or "").strip()
    if sku:
        base = re.sub(r"-\d$", "", sku)  # internal suffix like 3JA30AE-1
        for tok in (base, sku):
            if re.search(r"\d", tok) and re.search(r"[A-Za-z]", tok) and 4 <= len(tok) <= 16:
                out.append(tok.upper())
    for src in (unquote(p.get("slug") or ""), p.get("name") or ""):
        for tok in MPN_RE.findall(src):
            t = tok.upper()
            if not (re.search(r"\d", t) and re.search(r"[A-Za-z]", t)):
                continue
            if t.lower() in MPN_JUNK or len(t) < 5:
                continue
            out.append(t)
    seen, dedup = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            dedup.append(t)
    return dedup[:5]


def tier_of(p):
    cats = " ".join(c.get("name", "") for c in (p.get("categories") or []))
    hay = cats + " " + (p.get("name") or "")
    if any(k in hay for k in TIER_C_KEYWORDS):
        return "C"
    if any(k in cats for k in TIER_A_KEYWORDS):
        return "A"
    return "B"


def primary_category(p):
    names = [c.get("name", "") for c in (p.get("categories") or [])]
    for n in names:  # skip catch-all buckets
        if n not in ("جميع المنتجات", "عروض وتخفيضات", "الكل في واحد"):
            return n
    return names[0] if names else ""


def brand_of(p):
    for key in ("brands", "etheme_brands"):
        for b in p.get(key) or []:
            name = b.get("name") if isinstance(b, dict) else str(b)
            if name:
                return name
    # crude fallback: first ASCII word of slug
    slug = p.get("slug") or ""
    m = re.match(r"([a-z]+)", slug)
    return (m.group(1).upper() if m else "")


def english_title(p):
    """Product name if already English, else the decoded slug if IT is English,
    else empty (Arabic-only product — searches use the Arabic name)."""
    name = unicodedata.normalize("NFC", p.get("name") or "").strip()
    if is_english(name):
        return name
    slug = unquote(p.get("slug") or "").replace("-", " ").strip()
    if is_english(slug):
        return slug
    return ""


# Excel sections -> tier. Toners/inks are formulaic (A); hardware/accessories (B).
XLSX_TIER_B_KEYWORDS = ["فرامات", "طابعات", "اكسسوار", "جمينج", "جيمنج", "PRINTER", "SHREDDER"]
EAN_RE = re.compile(r"^\d{12,14}$")


def xlsx_rows(path):
    """Parse the inventory sheet into batch-product rows (stdlib only)."""
    import zipfile
    from xml.etree import ElementTree as ET

    M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(path)
    ss = ET.fromstring(z.read("xl/sharedStrings.xml"))
    strings = ["".join(t.text or "" for t in si.iter(M + "t")) for si in ss]
    sh = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))

    rows, section = [], ""
    for row in sh.iter(M + "row"):
        cells = {}
        for c in row.iter(M + "c"):
            col = "".join(ch for ch in c.get("r") if ch.isalpha())
            v = c.find(M + "v")
            if v is None:
                continue
            val = strings[int(v.text)] if c.get("t") == "s" else v.text
            if val and str(val).strip():
                cells[col] = str(val).strip()
        a = cells.get("A", "")
        if a and not a.isdigit() and "B" not in cells:
            section = a  # section header / subtotal label
            continue
        if not a.isdigit():
            continue
        code = cells.get("B", "")
        name = cells.get("C", "") or code
        if not (code or name):
            continue
        # the code cell often packs several interchangeable OEM part numbers: "CE505A/280A/CRG719"
        mpns = []
        if EAN_RE.match(code):
            mpns.append(code)  # EAN/UPC barcode — strongest possible key
        else:
            for tok in re.split(r"[/,+ ]+", code):
                tok = tok.strip().upper()
                if 3 <= len(tok) <= 16 and re.search(r"\d", tok) and tok not in mpns:
                    mpns.append(tok)
        name_is_en = is_english(name)
        tier = "B" if any(k in section.upper() or k in section for k in XLSX_TIER_B_KEYWORDS) else "A"
        rows.append({
            "product_id": 900000 + int(a),  # synthetic id namespace, disjoint from Woo ids
            "sheet_row_no": int(a),
            "sku": code,
            "name": name,
            "slug": "",
            "english_title": name if name_is_en else "",
            "brand": "",
            "category": section,
            "categories": [section],
            "mpn_candidates": mpns[:6],
            "barcode": code if EAN_RE.match(code) else "",
            "permalink": "",
            "tier": tier,
            "short_desc": "",
        })
    return rows


def main():
    global COMPETITORS
    COMPETITORS = load_competitors()
    print(f"competitors ({len(COMPETITORS)}): {', '.join(COMPETITORS)}")
    force = "--force" in sys.argv
    xlsx = None
    if "--xlsx" in sys.argv:
        xlsx = sys.argv[sys.argv.index("--xlsx") + 1]
    state_path = os.path.join(DATA_DIR, "state.json")
    if os.path.exists(state_path) and not force:
        st = json.load(open(state_path, encoding="utf-8"))
        if st.get("batches", {}).get("completed") or st.get("current_batch"):
            sys.exit("state.json shows work in progress — refusing to regenerate. Use --force to override.")

    if xlsx:
        rows = xlsx_rows(xlsx)
        finish(rows, state_path, source=xlsx)
        return

    products = [p for p in json.load(open(PRODUCTS, encoding="utf-8")) if p.get("status") == "publish"]

    rows = []
    for p in products:
        rows.append({
            "product_id": p["id"],
            "sku": p.get("sku") or "",
            "name": p.get("name") or "",
            "slug": unquote(p.get("slug") or ""),
            "english_title": english_title(p),
            "brand": brand_of(p),
            "category": primary_category(p),
            "categories": [c.get("name", "") for c in (p.get("categories") or [])][:6],
            "mpn_candidates": mpn_candidates(p),
            "price": p.get("price") or "",
            "regular_price": p.get("regular_price") or "",
            "permalink": p.get("permalink") or "",
            "tier": tier_of(p),
            "short_desc": strip_html(p.get("short_description"))[:300].strip(),
        })
    finish(rows, state_path, source=PRODUCTS)


def finish(rows, state_path, source):
    rows.sort(key=lambda r: (r["tier"], r["category"], r["brand"], r["name"]))

    os.makedirs(os.path.join(DATA_DIR, "batches"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "results"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "logs"), exist_ok=True)
    os.makedirs(os.path.join(DATA_DIR, "export"), exist_ok=True)

    batch_id = 0
    batch_index = []
    for tier in ("A", "B", "C"):
        tier_rows = [r for r in rows if r["tier"] == tier]
        size = BATCH_SIZE[tier]
        for i in range(0, len(tier_rows), size):
            batch_id += 1
            chunk = tier_rows[i:i + size]
            atomic_write(os.path.join(DATA_DIR, "batches", f"batch-{batch_id:03d}.json"), {
                "batch_id": batch_id,
                "tier": tier,
                "competitors": COMPETITORS,
                "count": len(chunk),
                "products": chunk,
            })
            batch_index.append({"id": batch_id, "tier": tier, "count": len(chunk),
                                "categories": sorted({c["category"] for c in chunk})[:8]})

    state = {
        "run_id": "matching-2026-07",
        "source": source,
        "batches": {
            "total": batch_id,
            "index": batch_index,
            "completed": [],
            "in_progress": None,
            "remaining": [b["id"] for b in batch_index],
        },
        "current_batch": None,
        "stats": {
            "products_done": 0, "high": 0, "medium": 0, "low": 0, "gap": 0,
            "review": 0, "errors": 0,
            "per_competitor": {c: {"matched": 0, "gap": 0} for c in COMPETITORS},
        },
        "retry_queue_size": 0,
    }
    atomic_write(state_path, state)
    for name, default in (("retry-queue.json", []), ("needs_human_review.json", [])):
        atomic_write(os.path.join(DATA_DIR, name), default)

    tiers = {t: sum(1 for r in rows if r["tier"] == t) for t in "ABC"}
    print(f"products: {len(rows)}  tiers: {tiers}  batches: {batch_id} (200/batch)")
    print(f"state: {state_path}")


if __name__ == "__main__":
    main()
