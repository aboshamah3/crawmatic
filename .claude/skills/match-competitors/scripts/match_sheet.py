#!/usr/bin/env python3
"""Catalog-to-catalog matching: OUR store products (2,141 WooCommerce items)
against the COMPETITOR'S inventory sheet (قائمه الاسعار والكميات, ~499 items).

Deterministic part-number join first; anything it can't resolve is left for a
small LLM cleanup pass. No web access.

Outputs (to MATCH_DATA_DIR, default /srv/crawmatic/matching):
  sheet-matches.csv        every our-product -> sheet-item match (or ambiguous/none)
  sheet-matches.jsonl      same, machine-readable
  sheet-unmatched-ours.csv our products with no sheet match (expected for most non-toner items)
  sheet-uncovered.csv      sheet items that matched none of our products

Usage: python3 match_sheet.py [--xlsx PATH] [--products-from-archive DIR]
"""
import csv
import glob
import importlib.util
import json
import os
import re
import sys
from collections import defaultdict

DATA_DIR = os.environ.get("MATCH_DATA_DIR", "/srv/crawmatic/matching")
SKILL_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
DEFAULT_XLSX = os.path.join(DATA_DIR, "قائمه الاسعار والكميات 2 (2).xlsx")
DEFAULT_ARCHIVE = os.path.join(DATA_DIR, "woo-run-archive", "batches")

# tokens too generic to identify anything on their own
STOPWORDS = {
    "TONER", "INK", "MINT", "BLACK", "CYAN", "YELLOW", "MAGENTA", "COLOR",
    "WITH", "CHIP", "70ML", "100ML", "130ML", "300ML", "BK", "SEE", "ELS",
    "HP", "CANON", "EPSON", "BROTHER", "SAMSUNG", "KYOCERA", "TOSHIBA",
    "XEROX", "RICOH", "SHARP", "USB", "RGB", "LED", "PRO", "PLUS", "MAX",
}


def norm(tok):
    return re.sub(r"[^A-Z0-9]", "", tok.upper())


def load_sheet(xlsx):
    spec = importlib.util.spec_from_file_location(
        "prep", os.path.join(SKILL_SCRIPTS, "prepare_batches.py"))
    prep = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prep)
    return prep.xlsx_rows(xlsx)


def sheet_tokens(item):
    """Identifier tokens for a sheet item: split code + name words with digits."""
    toks = set()
    for src in (item["sku"], item["name"]):
        for t in re.split(r"[/,+\s–-]+", src):
            n = norm(t)
            if len(n) < 3 or not re.search(r"\d", n) or n in STOPWORDS:
                continue
            if n.isdigit() and len(n) == 4:
                continue  # bare numbers like 1000/1600 are DPI/spec noise
            toks.add(n)
    # the whole code as one token (CH-106 BK -> CH106BK) — exact-SKU joins
    full = norm(item["sku"])
    if len(full) >= 5:
        toks.add(full)
        stripped = re.sub(r"^(TA|BR|SG|KY)", "", full)  # vendor prefixes (TA-T-FC50P-C)
        if len(stripped) >= 5:
            toks.add(stripped)
    if item.get("barcode"):
        toks.add(norm(item["barcode"]))
    return toks


def product_tokens(p):
    """Identifier tokens for one of our products."""
    toks = set()
    for c in p.get("mpn_candidates", []):
        n = norm(c)
        if len(n) >= 3:
            toks.add(n)
        for part in re.split(r"-", c):
            n2 = norm(part)
            if len(n2) >= 3 and re.search(r"\d", n2):
                toks.add(n2)
    sku = norm(p.get("sku", ""))
    if len(sku) >= 3 and re.search(r"\d", sku):
        toks.add(sku)
        toks.add("T" + sku)  # Toshiba-style T- prefixed MPNs (FC50P-C vs T-FC50P-C)
    for t in re.split(r"[\s/,+–|-]+", p.get("name", "")):
        n = norm(t)
        if len(n) >= 4 and re.search(r"\d", n) and n not in STOPWORDS:
            if n.isdigit() and len(n) == 4:
                continue
            toks.add(n)
    return toks - {norm(s) for s in STOPWORDS}


COLOR_MAP_OURS = [
    (("اسود", "أسود", "BLACK", "BK"), "BK"),
    (("ازرق", "أزرق", "سماوي", "CYAN"), "C"),
    (("اصفر", "أصفر", "YELLOW"), "Y"),
    (("احمر", "أحمر", "ارجواني", "أرجواني", "MAGENTA"), "M"),
]


def color_of_ours(p):
    hay = (p.get("name", "") + " " + p.get("sku", "")).upper()
    for keys, c in COLOR_MAP_OURS:
        if any(k.upper() in hay for k in keys):
            return c
    return ""


def color_of_sheet(item):
    hay = (item["sku"] + " " + item["name"]).upper()
    # explicit suffix tokens first (…/CRG054 BK, …117A Y with chip)
    m = re.search(r"\b(BK|BLACK|C|CYAN|Y|YELLOW|M|MAGENTA)\b(?!\w)", hay)
    if m:
        t = m.group(1)
        return {"BLACK": "BK", "CYAN": "C", "YELLOW": "Y", "MAGENTA": "M"}.get(t, t)
    return ""


def main():
    xlsx = DEFAULT_XLSX
    archive = DEFAULT_ARCHIVE
    if "--xlsx" in sys.argv:
        xlsx = sys.argv[sys.argv.index("--xlsx") + 1]
    if "--products-from-archive" in sys.argv:
        archive = sys.argv[sys.argv.index("--products-from-archive") + 1]

    sheet = load_sheet(xlsx)
    products = []
    for bf in sorted(glob.glob(os.path.join(archive, "batch-*.json"))):
        products.extend(json.load(open(bf, encoding="utf-8"))["products"])
    print(f"sheet items: {len(sheet)}   our products: {len(products)}")

    # index: token -> sheet item row numbers
    index = defaultdict(set)
    items_by_row = {}
    for it in sheet:
        items_by_row[it["sheet_row_no"]] = it
        for t in sheet_tokens(it):
            index[t].add(it["sheet_row_no"])

    matched, ambiguous, unmatched = [], [], []
    covered_rows = set()
    for p in products:
        toks = set(product_tokens(p))
        # part numbers sometimes miss the trailing revision letter (CF210 -> CF210A);
        # only for real part-number-length tokens, else "130 مل" fabricates "130A"
        for t in list(toks):
            if t[-1].isdigit() and len(t) >= 5:
                toks.add(t + "A")
        hits = defaultdict(set)  # row -> matching tokens
        for t in toks:
            for row in index.get(t, ()):
                hits[row].add(t)
        if not hits:
            unmatched.append(p)
            continue
        # prefer rows hit by the most specific (longest) token, then by most tokens
        def rank(row):
            return (max(len(t) for t in hits[row]), len(hits[row]))
        best = max(rank(r) for r in hits)
        # a ≤3-char best token alone is too weak to assert a match
        if best[0] <= 3:
            unmatched.append(p)
            continue
        best_rows = [r for r in hits if rank(r) == best]
        # tie-break color families (BK/C/Y/M rows of the same toner family)
        if len(best_rows) > 1:
            oc = color_of_ours(p)
            if oc:
                same = [r for r in best_rows if color_of_sheet(items_by_row[r]) == oc]
                if len(same) == 1:
                    best_rows = same
                elif not same and all(color_of_sheet(items_by_row[r]) for r in best_rows):
                    # the family exists in the sheet but OUR color variant doesn't
                    unmatched.append(p)
                    continue
        # tie-break compatible-vs-original: our Mint (منت) products prefer the
        # Mint sections; others prefer non-Mint sections
        if len(best_rows) > 1:
            ours_mint = "منت" in p.get("name", "") or "MINT" in p.get("name", "").upper()
            def is_mint_row(r):
                s = items_by_row[r]["category"].upper()
                return "MINT" in s or "منت" in items_by_row[r]["category"]
            pref = [r for r in best_rows if is_mint_row(r) == ours_mint]
            if len(pref) == 1:
                best_rows = pref
        # identical duplicate rows in the sheet -> take the first, note it
        dup_note = ""
        if len(best_rows) > 1 and len({items_by_row[r]["sku"] for r in best_rows}) == 1:
            dup_note = f"sheet lists this code {len(best_rows)}x (rows {sorted(best_rows)})"
            best_rows = [min(best_rows)]
        # multi-color kit (طقم/set) legitimately maps to the whole color family
        is_kit = "طقم" in p.get("name", "") or re.search(r"\b(SET|KIT)\b", p.get("name", "").upper())
        if len(best_rows) > 1 and is_kit:
            rec = {
                "product_id": p["product_id"], "sku": p.get("sku", ""),
                "name": p.get("name", ""), "permalink": p.get("permalink", ""),
                "status": "matched", "confidence": "high",
                "sheet_row": min(best_rows),
                "sheet_code": " + ".join(items_by_row[r]["sku"][:30] for r in sorted(best_rows)),
                "sheet_name": f"KIT -> {len(best_rows)} color rows {sorted(best_rows)}",
                "sheet_section": items_by_row[min(best_rows)]["category"],
                "matched_tokens": sorted({t for r in best_rows for t in hits[r]},
                                         key=len, reverse=True)[:6],
            }
            covered_rows.update(best_rows)
            matched.append(rec)
            continue
        rec = {
            "product_id": p["product_id"], "sku": p.get("sku", ""),
            "name": p.get("name", ""), "permalink": p.get("permalink", ""),
        }
        if len(best_rows) == 1:
            row = best_rows[0]
            it = items_by_row[row]
            covered_rows.add(row)
            rec.update({
                "sheet_row": row, "sheet_code": it["sku"], "sheet_name": it["name"],
                "sheet_section": it["category"],
                "matched_tokens": sorted(hits[row], key=len, reverse=True)[:4],
                "confidence": "high" if max(len(t) for t in hits[row]) >= 5 else "medium",
                "status": "matched",
            })
            matched.append(rec)
        else:
            rec.update({
                "status": "ambiguous",
                "candidates": [{
                    "sheet_row": r, "sheet_code": items_by_row[r]["sku"],
                    "sheet_name": items_by_row[r]["name"],
                    "matched_tokens": sorted(hits[r], key=len, reverse=True)[:4],
                } for r in sorted(best_rows)[:6]],
            })
            ambiguous.append(rec)

    uncovered = [it for it in sheet if it["sheet_row_no"] not in covered_rows]

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(os.path.join(DATA_DIR, "sheet-matches.jsonl"), "w", encoding="utf-8") as f:
        for rec in matched + ambiguous:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    with open(os.path.join(DATA_DIR, "sheet-matches.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["our_product_id", "our_sku", "our_name", "our_url",
                    "sheet_row", "sheet_code", "sheet_name", "sheet_section",
                    "matched_on", "confidence", "status"])
        for r in matched:
            w.writerow([r["product_id"], r["sku"], r["name"], r["permalink"],
                        r["sheet_row"], r["sheet_code"], r["sheet_name"],
                        r["sheet_section"], "/".join(r["matched_tokens"]),
                        r["confidence"], r["status"]])
        for r in ambiguous:
            cands = " | ".join(f'row{c["sheet_row"]}:{c["sheet_code"]}' for c in r["candidates"])
            w.writerow([r["product_id"], r["sku"], r["name"], r["permalink"],
                        "", cands, "", "", "", "", "ambiguous"])
    with open(os.path.join(DATA_DIR, "sheet-unmatched-ours.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["our_product_id", "our_sku", "our_name", "our_category"])
        for p in unmatched:
            w.writerow([p["product_id"], p.get("sku", ""), p.get("name", ""), p.get("category", "")])
    with open(os.path.join(DATA_DIR, "sheet-uncovered.csv"), "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["sheet_row", "sheet_code", "sheet_name", "sheet_section"])
        for it in uncovered:
            w.writerow([it["sheet_row_no"], it["sku"], it["name"], it["category"]])

    print(f"matched: {len(matched)}  ambiguous: {len(ambiguous)}  "
          f"unmatched (ours): {len(unmatched)}")
    print(f"sheet items covered: {len(covered_rows)}/{len(sheet)}  "
          f"uncovered: {len(uncovered)}")
    print(f"outputs in {DATA_DIR}: sheet-matches.csv/.jsonl, "
          f"sheet-unmatched-ours.csv, sheet-uncovered.csv")


if __name__ == "__main__":
    main()
