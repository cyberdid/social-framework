"""YouTube discovery + metadata + comments for the incident with yt-dlp (open source, no API key, no login).

Step 1: per query, relevance (ytsearchN) + upload-date-sorted results URL -> candidate video IDs.
Step 2: full metadata per video (no media download); keep uploads >= --since.
Step 3: comments for the top --comment-videos videos by views (cap --max-comments each).
Privacy: comment authors -> HMAC pseudonyms (salt ~/.config/social/pseudo_salt); author names/handles not stored.
Search results are YouTube's ranking, not a census.

Usage: python src/youtube_collect.py [--per-query 60] [--since 20261102]
Out:   data/raw/youtube/<UTC>/{search.jsonl,videos.jsonl,comments.jsonl,manifest.json}
"""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp

ROOT = Path(__file__).resolve().parents[1]
CFG = Path.home() / ".config/social"
QUERIES = ["Петропавлівська Борщагівка", "Петропавловская Борщаговка", "Борщагівка ТЦК", "Борщаговка ТЦК",
           "Борщаговка концлагерь", "Борщагівка Буданов", "Борщаговка Буданов", "Борщагівка нацгвардія",
           "Борщаговка нацгвардия подвал", "Borshchahivka", "Борщагівка медик швидка", "Борщаговка тюрьма"]
VIDEO_KEYS = ["id", "title", "description", "upload_date", "timestamp", "release_timestamp", "duration", "view_count",
              "like_count", "comment_count", "channel", "channel_id", "channel_follower_count", "uploader_id",
              "is_live", "was_live", "live_status", "webpage_url", "tags", "categories", "language"]


def salt():
    p = CFG / "pseudo_salt"
    if not p.exists():
        os.umask(0o077)
        p.write_text(secrets.token_hex(32))
    return bytes.fromhex(p.read_text().strip())


def pseudo(x, key):
    return "acct_" + hmac.new(key, f"yt:{x}".encode(), hashlib.sha256).hexdigest()[:12]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-query", type=int, default=60)
    ap.add_argument("--since", default="20261102")
    ap.add_argument("--comment-videos", type=int, default=30)
    ap.add_argument("--max-comments", type=int, default=1500)
    ap.add_argument("--pause", type=float, default=8.0)
    ap.add_argument("--resume", default=None, help="existing data/raw/youtube/<UTC> dir: reuse search, skip fetched videos")
    a = ap.parse_args()
    key = salt()
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(a.resume).resolve() if a.resume else ROOT / "data/raw/youtube" / now
    out.mkdir(parents=True, exist_ok=bool(a.resume))
    base = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "ignoreerrors": True}
    ids = {}
    queries_run = QUERIES
    if a.resume:
        for l in (out / "search.jsonl").open():
            r = json.loads(l)
            ids.setdefault(r["id"], r["query"])
        queries_run = []
    fs = (out / "search.jsonl").open("a")
    from urllib.parse import quote_plus
    with yt_dlp.YoutubeDL(base | {"extract_flat": "in_playlist", "playlistend": a.per_query}) as y:
        for q in queries_run:
            # two orderings: relevance (ytsearchN) and upload date (results URL, sp=CAI%3D); ytsearchdate was removed upstream
            for mode, url in [("relevance", f"ytsearch{a.per_query}:{q}"),
                              ("date", f"https://www.youtube.com/results?search_query={quote_plus(q)}&sp=CAI%253D")]:
                r = y.extract_info(url, download=False) or {}
                for rank, e in enumerate(r.get("entries") or []):
                    if e and e.get("id"):
                        ids.setdefault(e["id"], q)
                        fs.write(json.dumps({"query": q, "mode": mode, "rank": rank, "id": e["id"], "title": e.get("title"),
                                             "fetched_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
                time.sleep(a.pause)
            print(f"search {q!r}: total ids {len(ids)}", flush=True)
    fs.close()
    vids, done = [], set()
    if (out / "videos.jsonl").exists():
        for l in (out / "videos.jsonl").open():
            v = json.loads(l)
            vids.append(v)
            done.add(v["id"])
    with yt_dlp.YoutubeDL(base) as y, (out / "videos.jsonl").open("a") as fv:
        for vid, q in ids.items():
            if vid in done:
                continue
            info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
            time.sleep(a.pause)
            if not info:
                continue
            rec = {k: info.get(k) for k in VIDEO_KEYS} | {"found_by": q, "fetched_at": datetime.now(timezone.utc).isoformat()}
            rec["in_window"] = (rec.get("upload_date") or "0") >= a.since
            fv.write(json.dumps(rec, ensure_ascii=False) + "\n")
            vids.append(rec)
    print(f"videos: {len(vids)}, in window: {sum(v['in_window'] for v in vids)}", flush=True)
    top = sorted([v for v in vids if v["in_window"]], key=lambda v: -(v.get("view_count") or 0))[: a.comment_videos]
    nc = 0
    opts = base | {"getcomments": True,
                   "extractor_args": {"youtube": {"max_comments": [str(a.max_comments), "all", "100", "20"],
                                                  "comment_sort": ["new"]}}}
    with yt_dlp.YoutubeDL(opts) as y, (out / "comments.jsonl").open("w") as fc:
        for v in top:
            info = y.extract_info(v["webpage_url"], download=False) or {}
            for c in info.get("comments") or []:
                fc.write(json.dumps({"video_id": v["id"], "comment_id": c.get("id"), "parent": c.get("parent"),
                                     "text": c.get("text"), "timestamp": c.get("timestamp"),
                                     "like_count": c.get("like_count"), "is_pinned": c.get("is_pinned"),
                                     "author_is_uploader": c.get("author_is_uploader"),
                                     "author_is_verified": c.get("author_is_verified"),
                                     "author": pseudo(c.get("author_id") or c.get("author"), key),
                                     "fetched_at": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False) + "\n")
                nc += 1
            print(f"comments {v['id']}: total {nc}", flush=True)
            time.sleep(a.pause)
    man = {"collected_utc": now, "queries": QUERIES, "per_query": a.per_query, "since": a.since, "ids": len(ids),
           "videos": len(vids), "videos_in_window": sum(v["in_window"] for v in vids), "comment_videos": len(top),
           "comments": nc, "yt_dlp": yt_dlp.version.__version__, "auth": "none",
           "sha256": {f: hashlib.sha256((out / f).read_bytes()).hexdigest() for f in ["search.jsonl", "videos.jsonl", "comments.jsonl"]}}
    (out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2))
    print(json.dumps({k: man[k] for k in ["ids", "videos", "videos_in_window", "comments"]}), "->", out)


if __name__ == "__main__":
    main()
