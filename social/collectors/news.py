"""Google News RSS search (public, no key) for media coverage across languages and outlets.

Each query is run per date window (after:/before: operators) to get past the 100-item cap.
Items (title, source name and domain, publication time, Google link) are written and fsynced
at once; a rerun skips (query, window) pairs already done. Results reflect Google News
ranking and indexing, not a census. Article bodies are archived separately where possible.

Usage: python -m social.collectors.news --queries queries.tsv \
          --start 2026-09-01 --end 2026-11-12 --window-days 7 --out <dir>
Query file (tab-separated): query <TAB> hl <TAB> gl <TAB> ceid <TAB> windowed(yes/no)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-archive"}
ITEM = re.compile(r"<item>(.*?)</item>", re.S)


def field(block, tag):
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", block, re.S)
    return html.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1))).strip() if m else None


def durable(f, obj):
    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    f.flush()
    os.fsync(f.fileno())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=Path, required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--pause", type=float, default=3.0)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    done_p, items_p = a.out / "queries_done.jsonl", a.out / "items.jsonl"
    done = {(r["query"], r["hl"], r["window"]) for r in map(json.loads, done_p.open())} if done_p.exists() else set()
    specs = []
    for line in a.queries.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        q, hl, gl, ceid, windowed = (line.split("\t") + ["", "", "", "yes"])[:5]
        specs.append((q, hl, gl, ceid, windowed.strip() != "no"))
    d0, d1 = date.fromisoformat(a.start), date.fromisoformat(a.end)
    with done_p.open("a", encoding="utf-8") as fd, items_p.open("a", encoding="utf-8") as fi:
        for q, hl, gl, ceid, windowed in specs:
            windows = []
            if windowed:
                d = d0
                while d < d1:
                    e = min(d + timedelta(days=a.window_days), d1)
                    windows.append((d, e))
                    d = e
            else:
                windows = [(d0, d1)]
            for ws, we in windows:
                key = (q, hl, f"{ws}:{we}")
                if key in done:
                    continue
                full = f"{q} after:{ws} before:{we}"
                url = (f"https://news.google.com/rss/search?q={quote_plus(full)}&hl={hl}&gl={gl}&ceid={quote_plus(ceid)}")
                text, status = None, None
                for i in range(4):
                    try:
                        r = requests.get(url, headers=UA, timeout=40)
                        status = r.status_code
                        if r.status_code == 200:
                            text = r.text
                            break
                    except requests.RequestException:
                        status = "error"
                    time.sleep(20 * (i + 1))
                time.sleep(a.pause)
                if text is None:
                    print(f"{q} [{hl}] {ws}..{we}: failed {status}", flush=True)
                    continue
                n = 0
                for block in ITEM.findall(text):
                    src = re.search(r'<source url="([^"]+)">([^<]*)</source>', block)
                    pub = field(block, "pubDate")
                    try:
                        pub_iso = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat() if pub else None
                    except (TypeError, ValueError):
                        pub_iso = None
                    durable(fi, {"query": q, "hl": hl, "window": f"{ws}:{we}", "title": field(block, "title"),
                                 "gnews_link": field(block, "link"), "published": pub_iso,
                                 "source_url": src.group(1) if src else None,
                                 "source_name": html.unescape(src.group(2)) if src else None,
                                 "fetched_at": datetime.now(timezone.utc).isoformat()})
                    n += 1
                durable(fd, {"query": q, "hl": hl, "window": f"{ws}:{we}", "items": n, "capped": n >= 100})
                print(f"{q} [{hl}] {ws}..{we}: {n}{' (capped)' if n >= 100 else ''}", flush=True)


if __name__ == "__main__":
    main()
