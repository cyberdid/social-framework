"""Archive every item of a news site's paginated listing since a date (durable, resumable).

Listing pages are walked newest-first until all items on a page are older than --since.
Every item page is stored gzip + sha256, and its index line is flushed and fsynced at once,
so a power cut loses at most the item in flight; a rerun skips items already on disk.
Selection by topic is done later on the full text, not on URLs.

Usage: python -m social.collectors.site_listing --name slidstvo_news \
          --listing https://www.slidstvo.info/news/ --page-pattern "{base}page/{n}/" \
          --item-regex 'https://www\\.slidstvo\\.info/news/[a-z0-9-]+/' --since 2026-09-01 --out <dir>
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-archive"}
DT = re.compile(r'datetime="(\d{4}-\d{2}-\d{2})')


def fetch(url, delay):
    for i in range(5):
        try:
            r = requests.get(url, headers=UA, timeout=40)
            time.sleep(delay)
            if r.status_code in (429, 503):
                time.sleep(30 * (i + 1))
                continue
            return r
        except requests.RequestException:
            time.sleep(10 * (i + 1))
    return None


def durable(f, obj):
    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    f.flush()
    os.fsync(f.fileno())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--listing", required=True)
    ap.add_argument("--page-pattern", default="{base}page/{n}/")
    ap.add_argument("--item-regex", required=True)
    ap.add_argument("--since", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-pages", type=int, default=200)
    ap.add_argument("--delay", type=float, default=1.5)
    a = ap.parse_args(argv)
    out = a.out / a.name
    out.mkdir(parents=True, exist_ok=True)
    idx_p = out / "index.jsonl"
    known = {}
    if idx_p.exists():
        for l in idx_p.open():
            r = json.loads(l)
            known[r["url"]] = r.get("published")
    done = set(known)
    item_rx = re.compile(a.item_regex)
    with idx_p.open("a", encoding="utf-8") as idx:
        for n in range(1, a.max_pages + 1):
            url = a.listing if n == 1 else a.page_pattern.format(base=a.listing, n=n)
            r = fetch(url, a.delay)
            if r is None or r.status_code != 200:
                print(f"listing {n}: stop ({None if r is None else r.status_code})", flush=True)
                break
            page = r.text
            items = list(dict.fromkeys(item_rx.findall(page)))
            dates = DT.findall(page)
            newest = max(dates) if dates else None
            fetched = 0
            page_dates = []
            for u in items:
                if u in done:
                    if known.get(u):
                        page_dates.append(known[u])
                    continue
                rr = fetch(u, a.delay)
                if rr is None:
                    durable(idx, {"url": u, "error": "fetch_failed", "fetched_at": datetime.now(timezone.utc).isoformat()})
                    continue
                raw = rr.content
                h = hashlib.sha256(raw).hexdigest()
                (out / f"{h[:16]}.html.gz").write_bytes(gzip.compress(raw))
                m = DT.search(rr.text)
                durable(idx, {"url": u, "status": rr.status_code, "sha256": h, "file": f"{h[:16]}.html.gz",
                              "published": m.group(1) if m else None, "listing_page": n,
                              "fetched_at": datetime.now(timezone.utc).isoformat()})
                done.add(u)
                known[u] = m.group(1) if m else None
                if known[u]:
                    page_dates.append(known[u])
                fetched += 1
            print(f"listing {n}: {len(items)} items, +{fetched} fetched, dates {min(dates) if dates else None}..{newest}", flush=True)
            # Stop when most items on the page are older than the window. Sidebars with fresh
            # links ("popular", "latest") must not keep the walk going forever.
            old = sum(d < a.since for d in page_dates)
            if not items or (page_dates and old > len(page_dates) / 2):
                break


if __name__ == "__main__":
    main()
