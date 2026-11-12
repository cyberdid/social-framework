"""Media coverage list from the open GDELT DOC 2.0 API (no key), one query per day window.

GDELT asks for at most one request per 5 s; we wait 6 s. Each day x query result is written
and fsynced immediately; a rerun skips windows already done. GDELT lists what it indexed —
a broad sample of online news, not a census.

Usage: python -m social.collectors.gdelt --queries "ключове слово" "\"exact phrase\"" \
          --start 2026-09-01 --end 2026-11-11 --out <dir>
Out:   <dir>/articles.jsonl, <dir>/windows_done.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

API = "https://api.gdeltproject.org/api/v2/doc/doc"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-archive"}


def durable(f, obj):
    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    f.flush()
    os.fsync(f.fileno())


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", nargs="+", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--pause", type=float, default=6.0)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    done_p, art_p = a.out / "windows_done.jsonl", a.out / "articles.jsonl"
    done = {(r["query"], r["day"]) for r in map(json.loads, done_p.open())} if done_p.exists() else set()
    d0, d1 = date.fromisoformat(a.start), date.fromisoformat(a.end)
    with done_p.open("a", encoding="utf-8") as fd, art_p.open("a", encoding="utf-8") as fa:
        for q in a.queries:
            d = d0
            while d <= d1:
                key = (q, d.isoformat())
                if key in done:
                    d += timedelta(days=1)
                    continue
                params = {"query": q, "mode": "artlist", "maxrecords": 250, "format": "json", "sort": "datedesc",
                          "startdatetime": d.strftime("%Y%m%d000000"),
                          "enddatetime": (d + timedelta(days=1)).strftime("%Y%m%d000000")}
                arts, status = None, None
                for attempt in range(5):
                    try:
                        r = requests.get(API, params=params, headers=UA, timeout=90)
                        status = r.status_code
                        if r.status_code == 200:
                            arts = (r.json() if r.text.strip() else {}).get("articles", [])
                            break
                    except (requests.RequestException, ValueError):
                        status = "error"
                    time.sleep(a.pause * (attempt + 2))
                time.sleep(a.pause)
                if arts is None:
                    print(f"{q} {d}: failed ({status}); will retry on next run", flush=True)
                    d += timedelta(days=1)
                    continue
                for x in arts:
                    durable(fa, {"query": q, "day": d.isoformat(), **x,
                                 "fetched_at": datetime.now(timezone.utc).isoformat()})
                durable(fd, {"query": q, "day": d.isoformat(), "n": len(arts), "capped": len(arts) >= 250})
                print(f"{q} {d}: {len(arts)}", flush=True)
                d += timedelta(days=1)


if __name__ == "__main__":
    main()
