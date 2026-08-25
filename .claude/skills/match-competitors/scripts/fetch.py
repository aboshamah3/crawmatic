#!/usr/bin/env python3
"""Fetch a competitor product/search page through the DataImpulse SA residential proxy.

Usage:  python3 fetch.py <url> [--raw] [--max-chars 15000] [--no-proxy]

Reads PROXY_* from crawmatic/.env, appends the __cr.sa geo suffix to the login.
Default output: page <title>, any detected SAR prices, then de-tagged visible text
(truncated) — enough for a matcher agent to verify brand/MPN/specs and read the price.
"""
import argparse
import html
import os
import re
import subprocess
import sys

ENV_FILE = os.environ.get("CRAWMATIC_ENV", "/srv/crawmatic/crawmatic/.env")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PRICE_RES = [
    re.compile(r"(?:SAR|SR|ر\.س|ريال)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"),
    re.compile(r"([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:SAR|SR|ر\.س|ريال)"),
    re.compile(r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]{1,2})?)"?'),
]


def proxy_url():
    env = {}
    try:
        for line in open(ENV_FILE, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError as e:
        sys.exit(f"cannot read {ENV_FILE}: {e}")
    try:
        login = env["PROXY_LOGIN"] + "__cr.sa"
        return f"http://{login}:{env['PROXY_PASSWORD']}@{env['PROXY_HOST']}:{env['PROXY_PORT']}"
    except KeyError as e:
        sys.exit(f"missing {e} in {ENV_FILE}")


def fetch(url, use_proxy=True):
    cmd = ["curl", "-sL", "--compressed", "--max-time", "60", "-A", UA,
           "-H", "Accept-Language: en;q=0.9,ar;q=0.8"]
    if use_proxy:
        cmd += ["--proxy", proxy_url()]
    cmd += [url]
    r = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        if use_proxy:
            # some hosts (jarir) time out via the residential proxy but serve fine direct
            print(f"NOTE: proxy fetch failed (curl exit {r.returncode}), retrying direct",
                  file=sys.stderr)
            return fetch(url, use_proxy=False)
        sys.exit(f"curl failed (exit {r.returncode}): {r.stderr.strip()[:300]}")
    return r.stdout


def visible_text(page):
    page = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", page)
    page = re.sub(r"(?s)<!--.*?-->", " ", page)
    page = re.sub(r"<[^>]+>", " ", page)
    page = html.unescape(page)
    return re.sub(r"[ \t]{2,}|\s{3,}", "  ", page).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--raw", action="store_true", help="dump raw HTML")
    ap.add_argument("--max-chars", type=int, default=15000)
    ap.add_argument("--no-proxy", action="store_true")
    args = ap.parse_args()

    page = fetch(args.url, use_proxy=not args.no_proxy)
    if args.raw:
        print(page[:args.max_chars])
        return

    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", page)
    og = re.search(r'(?i)property="og:title"\s+content="([^"]*)"', page) or \
         re.search(r'(?i)content="([^"]*)"\s+property="og:title"', page)
    prices = []
    for rx in PRICE_RES:
        prices += rx.findall(page)
    prices = list(dict.fromkeys(prices))[:8]

    print(f"URL: {args.url}")
    print(f"TITLE: {html.unescape(title.group(1)).strip() if title else '(none)'}")
    if og:
        print(f"OG_TITLE: {html.unescape(og.group(1)).strip()}")
    print(f"PRICES_SEEN: {prices}")
    if re.search(r"(?i)captcha|are you a robot|just a moment|access denied", page[:4000]):
        print("WARNING: page looks bot-walled — content below may be a challenge page")
    print("---")
    print(visible_text(page)[:args.max_chars])


if __name__ == "__main__":
    main()
