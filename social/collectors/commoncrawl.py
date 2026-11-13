"""Fetch article pages from Common Crawl (open web archive) by exact URL. Durable, resumable.

Used for publisher domains that Ukrainian providers block (Russian media): the page is read from
Common Crawl's own servers (data.commoncrawl.org), not from the publisher. URLs not found in the
given crawls are written to a queue for a Wayback Save Page Now request.

Usage: python -m social.collectors.commoncrawl --urls <index.jsonl with url> --crawls CC-MAIN-2026-43 CC-MAIN-2026-39 \
          --out data/raw/commoncrawl
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-archive"}


def durable(f, obj):
    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    f.flush()
    os.fsync(f.fileno())


def lookup(crawl, url):
    for i in range(4):
        try:
            r = requests.get(f"https://index.commoncrawl.org/{crawl}-index",
                             params={"url": url, "output": "json"}, headers=UA, timeout=90)
            if r.status_code == 404:
                return []
            if r.status_code == 200:
                return [json.loads(l) for l in r.text.splitlines() if l.strip()]
        except requests.RequestException:
            pass
        time.sleep(10 * (i + 1))
    return None


def fetch_record(rec):
    start, length = int(rec["offset"]), int(rec["length"])
    r = requests.get(f"https://data.commoncrawl.org/{rec['filename']}", headers={**UA, "Range": f"bytes={start}-{start + length - 1}"},
                     timeout=120)
    r.raise_for_status()
    warc = gzip.GzipFile(fileobj=io.BytesIO(r.content)).read()
    # WARC headers, blank line, HTTP headers, blank line, body
    parts = warc.split(b"\r\n\r\n", 2)
    return parts[2] if len(parts) == 3 else warc


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--urls", type=Path, required=True)
    ap.add_argument("--crawls", nargs="+", required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--pause", type=float, default=2.0)
    a = ap.parse_args(argv)
    a.out.mkdir(parents=True, exist_ok=True)
    src = [json.loads(l) for l in a.urls.open()]
    src = [r for r in src if r.get("url")]
    idx_p, q_p = a.out / "index.jsonl", a.out / "wayback_queue.txt"
    done = {json.loads(l)["url"] for l in idx_p.open()} if idx_p.exists() else set()
    with idx_p.open("a") as idx, q_p.open("a") as q:
        for s in src:
            u = s["url"]
            if u in done:
                continue
            rec = {"url": u, "title": s.get("title"), "domain": s.get("domain"), "origin": s.get("origin"),
                   "published": s.get("published"), "fetched_at": datetime.now(timezone.utc).isoformat()}
            hit = None
            for c in a.crawls:
                res = lookup(c, u)
                if res is None:
                    rec.setdefault("index_errors", []).append(c)
                    continue
                ok = [x for x in res if x.get("status") == "200"]
                if ok:
                    hit = (c, ok[-1])
                    break
            if hit:
                try:
                    body = fetch_record(hit[1])
                    h = hashlib.sha256(body).hexdigest()
                    (a.out / f"{h[:16]}.html.gz").write_bytes(gzip.compress(body))
                    rec.update({"status": "commoncrawl", "crawl": hit[0], "cc_timestamp": hit[1].get("timestamp"),
                                "sha256": h, "file": f"{h[:16]}.html.gz", "bytes": len(body)})
                except Exception as e:  # noqa: BLE001
                    rec["error"] = f"{type(e).__name__}: {e}"[:200]
            else:
                rec["status"] = "not_in_crawls"
                q.write(u + "\n")
                q.flush()
            durable(idx, rec)
            print(rec.get("status"), rec.get("crawl", ""), u[:90], flush=True)
            time.sleep(a.pause)


if __name__ == "__main__":
    main()
