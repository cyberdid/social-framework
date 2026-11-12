"""Resolve Google News RSS links to publisher URLs and archive the article pages (durable, resumable).

Decoding uses the open-source googlenewsdecoder package (the same public request a browser makes;
no CAPTCHA or login is bypassed). Publisher pages that answer 403/CAPTCHA are recorded, not bypassed.
Order: oldest first; filter with --since and --domains.

Usage: python -m social.collectors.news_resolve --gnews data/raw/gnews/items.jsonl --out data/raw/news_articles
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
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


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--gnews", type=Path, nargs="+", required=True, help="items.jsonl from social.collectors.news")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--since", default="", help="only items published on/after this ISO date")
    ap.add_argument("--domains", nargs="*", default=None, help="only these publisher domains (default: all)")
    ap.add_argument("--pause", type=float, default=1.5)
    ap.add_argument("--decode-only", action="store_true",
                    help="resolve URLs only (for publishers blocked in your network; texts via public archives)")
    a = ap.parse_args(argv)
    from googlenewsdecoder import gnewsdecoder  # optional extra: pip install 'social-framework[news]'

    a.out.mkdir(parents=True, exist_ok=True)
    links = {}
    for g in a.gnews:
        for l in g.open(encoding="utf-8"):
            r = json.loads(l)
            dom = (r.get("source_url") or "").split("://")[-1].split("/")[0].lower().removeprefix("www.")
            if a.domains and dom not in a.domains:
                continue
            if r.get("gnews_link") and (r.get("published") or "") >= a.since:
                links.setdefault(r["gnews_link"], {"title": r["title"], "domain": dom, "origin": "", "published": r["published"] or ""})
    idx_p = a.out / "index.jsonl"
    done = {json.loads(l)["gnews_link"] for l in idx_p.open()} if idx_p.exists() else set()
    order = sorted(links.items(), key=lambda kv: kv[1]["published"])
    print(f"to process: {sum(1 for k, _ in order if k not in done)} of {len(order)}", flush=True)
    with idx_p.open("a", encoding="utf-8") as idx:
        for link, meta in order:
            if link in done:
                continue
            rec = {"gnews_link": link, **meta, "fetched_at": datetime.now(timezone.utc).isoformat()}
            try:
                d = gnewsdecoder(link, interval=1)
                url = d.get("decoded_url")
                rec["url"] = url
                if url and a.decode_only:
                    rec["status"] = "decoded_only"
                elif url:
                    r = requests.get(url, headers=UA, timeout=40)
                    b = r.content
                    h = hashlib.sha256(b).hexdigest()
                    (a.out / f"{h[:16]}.html.gz").write_bytes(gzip.compress(b))
                    rec.update({"status": r.status_code, "final_url": r.url, "sha256": h, "file": f"{h[:16]}.html.gz",
                                "bytes": len(b)})
                else:
                    rec["error"] = f"decode_failed: {d.get('message')}"
            except Exception as e:  # noqa: BLE001 - record and continue
                rec["error"] = f"{type(e).__name__}: {e}"[:300]
            durable(idx, rec)
            print(rec["domain"], rec.get("status", rec.get("error", ""))[:40] if isinstance(rec.get("status", ""), str) else rec.get("status"),
                  rec.get("url", "")[:90], flush=True)
            time.sleep(a.pause)


if __name__ == "__main__":
    main()
