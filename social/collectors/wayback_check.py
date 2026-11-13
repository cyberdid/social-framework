"""Wayback Machine: list existing captures (CDX, read-only) and optionally request Save Page Now.

Per URL: number of captures, first/last timestamps; with --save, an anonymous SPN request
(web.archive.org/save/<url>) whose resulting snapshot location is recorded.
Usage: python -m social.collectors.wayback_check --sources urls.txt --out <json> [--save]
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-archive"}


def cdx(url):
    r = requests.get("https://web.archive.org/cdx/search/cdx",
                     params={"url": url, "output": "json", "fl": "timestamp,statuscode,digest"}, headers=UA, timeout=60)
    rows = r.json() if r.status_code == 200 and r.text.strip() else []
    return rows[1:] if rows else []


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--delay", type=float, default=20.0)
    a = ap.parse_args(argv)
    urls = [l.split("#", 1)[0].strip() for l in a.sources.read_text(encoding="utf-8").splitlines()]
    urls = [u for u in urls if u]
    res = json.loads(a.out.read_text()) if a.out.exists() else []
    done = {r["url"] for r in res if ("save_status" in r or "save_error" in r or not a.save)}
    res = [r for r in res if r["url"] in done]
    for u in urls:
        if u in done:
            continue
        rec = {"url": u, "checked_at": datetime.now(timezone.utc).isoformat()}
        try:
            caps = cdx(u)
            rec.update({"captures_before": len(caps), "first": caps[0][0] if caps else None,
                        "last": caps[-1][0] if caps else None, "distinct_digests": len({c[2] for c in caps})})
        except Exception as e:  # noqa: BLE001
            rec["cdx_error"] = f"{type(e).__name__}: {e}"
        if a.save:
            try:
                r = requests.get("https://web.archive.org/save/" + u, headers=UA, timeout=180, allow_redirects=False)
                rec["save_status"] = r.status_code
                rec["save_location"] = r.headers.get("Content-Location") or r.headers.get("Location")
            except Exception as e:  # noqa: BLE001
                rec["save_error"] = f"{type(e).__name__}: {e}"
            time.sleep(a.delay)
        res.append(rec)
        print(json.dumps(rec, ensure_ascii=False), flush=True)
        a.out.parent.mkdir(parents=True, exist_ok=True)
        tmp = a.out.with_suffix(".tmp")
        tmp.write_text(json.dumps(res, ensure_ascii=False, indent=1))
        with tmp.open("rb") as fh:
            os.fsync(fh.fileno())
        tmp.replace(a.out)  # atomic: the file is never half-written


if __name__ == "__main__":
    main()
