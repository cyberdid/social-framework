"""Detect editorial changes between the earliest and latest Wayback captures of each source URL.

For URLs with >= 2 captures (from wayback_check.py output), fetch the raw first and last
captures (id_ mode, original bytes), extract <title>/og:title and the main text, and record
title changes, text similarity and number tokens added/removed. Durable and resumable.

Usage: python -m social.collectors.wayback_diff --wayback wayback.json \
          --raw <dir for raw captures> --out wayback_diff.jsonl
"""
from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import html
import json
import os
import re
import time
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-archive"}
NUM = re.compile(r"\d[\d\s.,]*\s*(?:млн|млрд|тис|%|грн|дол|\$)?", re.I)


def text_of(s: str) -> str:
    s = re.sub(r"<script.*?</script>|<style.*?</style>|<nav.*?</nav>|<footer.*?</footer>", " ", s, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def title_of(s: str):
    m = re.search(r'<meta property="og:title" content="([^"]*)"', s) or re.search(r"<title>(.*?)</title>", s, re.S)
    return html.unescape(m.group(1)).strip() if m else None


def capture(url, ts, raw_dir):
    p = raw_dir / f"{ts}_{hashlib.sha1(url.encode()).hexdigest()[:12]}.html.gz"
    if p.exists():
        return gzip.decompress(p.read_bytes()).decode("utf-8", "replace")
    for i in range(4):
        try:
            r = requests.get(f"https://web.archive.org/web/{ts}id_/{url}", headers=UA, timeout=90)
            if r.status_code == 200:
                p.write_bytes(gzip.compress(r.content))
                time.sleep(3)
                return r.content.decode("utf-8", "replace")
        except requests.RequestException:
            pass
        time.sleep(15 * (i + 1))
    return None


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wayback", type=Path, required=True)
    ap.add_argument("--raw", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args(argv)
    a.raw.mkdir(parents=True, exist_ok=True)
    done = {json.loads(l)["url"] for l in a.out.open()} if a.out.exists() else set()
    rows = [r for r in json.loads(a.wayback.read_text()) if (r.get("captures_before") or 0) >= 2]
    with a.out.open("a", encoding="utf-8") as f:
        for r in rows:
            if r["url"] in done:
                continue
            first, last = capture(r["url"], r["first"], a.raw), capture(r["url"], r["last"], a.raw)
            rec = {"url": r["url"], "first": r["first"], "last": r["last"]}
            if first is None or last is None:
                rec["error"] = "capture_fetch_failed"
            else:
                t1, t2 = text_of(first), text_of(last)
                n1, n2 = set(x.strip() for x in NUM.findall(t1)), set(x.strip() for x in NUM.findall(t2))
                rec.update({"title_first": title_of(first), "title_last": title_of(last),
                            "title_changed": title_of(first) != title_of(last),
                            "text_similarity": round(difflib.SequenceMatcher(None, t1[:20000], t2[:20000]).ratio(), 3),
                            "numbers_removed": sorted(n1 - n2)[:40], "numbers_added": sorted(n2 - n1)[:40]})
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
            print(json.dumps({k: rec.get(k) for k in ["url", "title_changed", "text_similarity", "error"]}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
