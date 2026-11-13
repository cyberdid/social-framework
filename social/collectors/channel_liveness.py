"""Check that seed channels are live: last public post date and subscribers via t.me/s (no login).

Usage: python -m social.collectors.channel_liveness <seeds.txt> <out.jsonl>   (durable, resumable)
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-snapshot (public channels only)"}
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="last public post and subscribers of Telegram channels (t.me/s, no login)")
    ap.add_argument("seeds", help="file with one channel per line")
    ap.add_argument("out", help="jsonl output (appended, resumable)")
    a = ap.parse_args(argv)
    seeds = [l.strip() for l in open(a.seeds, encoding="utf-8") if l.strip() and not l.startswith("#")]
    out = Path(a.out)
    done = {json.loads(l)["channel"] for l in out.open()} if out.exists() else set()
    with out.open("a", encoding="utf-8") as f:
        for ch in seeds:
            if ch in done:
                continue
            try:
                r = requests.get(f"https://t.me/s/{ch}", headers=UA, timeout=30)
                dates = re.findall(r'<time datetime="([^"]+)"', r.text)
                subs = re.search(r'counter_value">([^<]+)</span>\s*<span class="counter_type">subscribers', r.text)
                title = re.search(r'<meta property="og:title" content="([^"]*)"', r.text)
                rec = {"channel": ch, "status": r.status_code, "preview": "tgme_channel_info" in r.text,
                       "last_post": max(dates) if dates else None, "subscribers": subs.group(1) if subs else None,
                       "title": title.group(1) if title else None}
            except requests.RequestException as e:
                rec = {"channel": ch, "error": type(e).__name__}
            rec["checked_at"] = datetime.now(timezone.utc).isoformat()
            f.write(json.dumps(rec, ensure_ascii=False) + "\n"); f.flush(); os.fsync(f.fileno())
            time.sleep(1.2)


if __name__ == "__main__":
    main()
