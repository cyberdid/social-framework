"""Archive official news of the High Anti-Corruption Court (hcac.court.gov.ua) for a date window.

Only the public press-centre pages (no CAPTCHA). The court register (EDRSR) search is
CAPTCHA-protected and is not used. Pages are cp1251; raw bytes are stored gzip + sha256.

Usage: python -m social.collectors.court_news_hacc --since 2026-09-01 --out <dir>
Out:   <dir>/{list_*.html.gz, news_<id>.html.gz, index.jsonl}
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE = "https://hcac.court.gov.ua"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-archive"}
DATE = re.compile(r'news-open__date">\s*(\d{1,2})\s+(\w+)\s+(\d{4})')
MONTHS = {m: i + 1 for i, m in enumerate(["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
                                           "серпня", "вересня", "жовтня", "листопада", "грудня"])}


def fetch(url, delay):
    r = requests.get(url, headers=UA, timeout=40)
    time.sleep(delay)
    return r


def text_of(raw: bytes) -> str:
    s = raw.decode("cp1251", errors="replace")
    s = re.sub(r"<script.*?</script>|<style.*?</style>", " ", s, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--max-pages", type=int, default=40)
    ap.add_argument("--delay", type=float, default=1.5)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    idx = (a.out / "index.jsonl").open("a")
    seen = set()
    for page in range(1, a.max_pages + 1):
        url = f"{BASE}/hcac/pres-centr/news/" + (f"?page={page}" if page > 1 else "")
        r = fetch(url, a.delay)
        (a.out / f"list_{page:03d}.html.gz").write_bytes(gzip.compress(r.content))
        s = r.content.decode("cp1251", errors="replace")
        items = []
        for h, _, raw_title in re.findall(r'href="(/hcac/pres-centr/news/(\d+)/?)"[^>]*>(.*?)</a>', s, re.S):
            t = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", raw_title))).strip()
            if h not in seen and t:
                seen.add(h)
                items.append((h, t))
        oldest = None
        for h, title in items:
            nid = re.search(r"(\d+)", h).group(1)
            rr = fetch(BASE + h, a.delay)
            raw = rr.content
            (a.out / f"news_{nid}.html.gz").write_bytes(gzip.compress(raw))
            m = DATE.search(raw.decode("cp1251", errors="replace"))
            date = (f"{m.group(3)}-{MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
                    if m and m.group(2).lower() in MONTHS else None)
            oldest = min(oldest, date) if (oldest and date) else (date or oldest)
            idx.write(json.dumps({"url": BASE + h, "id": nid, "title": title, "date": date, "status": rr.status_code,
                                  "sha256": hashlib.sha256(raw).hexdigest(), "file": f"news_{nid}.html.gz",
                                  "fetched_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
            idx.flush()
        print(f"page {page}: {len(items)} items, oldest {oldest}", flush=True)
        if not items or (oldest and oldest < a.since):
            break


if __name__ == "__main__":
    main()
