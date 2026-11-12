"""Reddit posts and comments about a case via the open Arctic Shift archive (no login; reddit.com itself blocks
unauthenticated search and is NOT bypassed). Every record is appended + fsync'ed immediately; resumable via
queries_done.jsonl. Authors are replaced by HMAC pseudonyms (key from ~/.config/social/pseudonym.key if present,
else a per-run random key that is not stored).

Usage: python -m social.collectors.reddit --terms "keyword" "exact phrase" --out data/raw/reddit --after 2026-09-01 --before 2026-11-13
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import requests

API = "https://arctic-shift.photon-reddit.com/api"
SUBS = ["ukraine", "UkrainianConflict", "europe", "worldnews", "ukraina", "RussiaUkraineWar2022", "geopolitics",
        "UkraineRussiaReport", "anime_titties", "Ukrainian", "kyiv", "AskUkraine", "UkraineWarVideoReport", "liberta"]


def key():
    p = Path.home() / ".config/social/pseudonym.key"
    return p.read_bytes().strip() if p.exists() else secrets.token_bytes(32)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--terms", nargs="+", required=True, help="search terms (any language)")
    ap.add_argument("--subreddits", nargs="+", default=SUBS)
    ap.add_argument("--after", required=True)
    ap.add_argument("--before", required=True)
    ap.add_argument("--pause", type=float, default=1.5)
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    k = key()
    done = set()
    if (out / "queries_done.jsonl").exists():
        done = {json.loads(l)["q"] for l in (out / "queries_done.jsonl").open() if l.strip()}
    seen = set()
    for f in ["posts.jsonl", "comments.jsonl"]:
        if (out / f).exists():
            seen |= {json.loads(l)["id"] for l in (out / f).open() if l.strip()}
    fp = (out / "posts.jsonl").open("a")
    fc = (out / "comments.jsonl").open("a")
    fd = (out / "queries_done.jsonl").open("a")
    S = requests.Session()
    S.headers["User-Agent"] = "research-snapshot (public archive)"

    def pseud(x):
        return "acct_" + hmac.new(k, (x or "").encode(), hashlib.sha256).hexdigest()[:12] if x else None

    def get(url, params):
        for i in range(5):
            try:
                r = S.get(url, params=params, timeout=60)
                if r.status_code == 200:
                    return r.json().get("data") or []
                if r.status_code in (422, 429, 500, 502, 503):
                    time.sleep(20 * (i + 1))
                    continue
                print("HTTP", r.status_code, r.text[:200], flush=True)
                return None
            except Exception as e:  # noqa: BLE001
                print("err", e, flush=True)
                time.sleep(10 * (i + 1))
        return None

    from datetime import date, timedelta
    d0, d1 = date.fromisoformat(a.after), date.fromisoformat(a.before)
    wins = []
    while d0 < d1:
        e = min(d0 + timedelta(days=31), d1)
        wins.append((d0.isoformat(), e.isoformat()))
        d0 = e
    fails = 0
    for sub in a.subreddits:
      for t in a.terms:
        for w0, w1 in wins:
            for kind in ["posts", "comments"]:
                q = f"{kind}|{sub}|{t}|{w0}"
                if q in done:
                    continue
                field = "query" if kind == "posts" else "body"
                data = get(f"{API}/{kind}/search", {"subreddit": sub, field: t, "after": w0, "before": w1,
                                                     "limit": 100, "sort": "desc"})
                if data is None:
                    print("FAILED", q, flush=True)
                    fails += 1
                    if fails >= 4:
                        print("archive unavailable, exiting for later retry", flush=True)
                        raise SystemExit(3)
                    continue
                fails = 0
                n = 0
                for d in data:
                    if d.get("id") in seen:
                        continue
                    seen.add(d.get("id"))
                    rec = {"id": d.get("id"), "subreddit": d.get("subreddit"), "created_utc": d.get("created_utc"),
                           "author": pseud(d.get("author")), "score": d.get("score"), "query": t}
                    if kind == "posts":
                        rec.update({"title": d.get("title"), "selftext": d.get("selftext"), "url": d.get("url"),
                                    "permalink": d.get("permalink"), "num_comments": d.get("num_comments")})
                        fp.write(json.dumps(rec, ensure_ascii=False) + "\n"); fp.flush(); os.fsync(fp.fileno())
                    else:
                        rec.update({"body": d.get("body"), "link_id": d.get("link_id"), "parent_id": d.get("parent_id")})
                        fc.write(json.dumps(rec, ensure_ascii=False) + "\n"); fc.flush(); os.fsync(fc.fileno())
                    n += 1
                fd.write(json.dumps({"q": q, "n": n, "returned": len(data)}) + "\n"); fd.flush(); os.fsync(fd.fileno())
                print(q, len(data), n, flush=True)
                time.sleep(a.pause)


if __name__ == "__main__":
    main()
