"""Instagram: hashtag posts about the incident + comments of the most-discussed posts (Instaloader, open source).

Uses the user's own saved Instaloader session (~/.config/instaloader/session-<user>, created by the user in their
terminal; password never seen by this code). Conservative pacing to protect the account.
Privacy: post owners and commenters -> HMAC pseudonyms; only is_verified / is_business flags kept for owners.
Post URLs (/p/<shortcode>) are public and contain no username.

Usage: python src/instagram_collect.py --user <your_username> [--max-per-tag 150] [--comment-posts 15]
Out:   data/raw/instagram/<UTC>/{posts.jsonl,comments.jsonl,manifest.json}
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

import instaloader

ROOT = Path(__file__).resolve().parents[1]
CFG = Path.home() / ".config/social"
TAGS = ["борщагівка", "борщаговка", "петропавлівськаборщагівка", "петропавловскаяборщаговка", "борщагівкатцк"]
SINCE = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)  # 02.11 00:00 Kyiv


def salt():
    p = CFG / "pseudo_salt"
    if not p.exists():
        os.umask(0o077)
        p.write_text(secrets.token_hex(32))
    return bytes.fromhex(p.read_text().strip())


def pseudo(x, key):
    return "acct_" + hmac.new(key, f"ig:{x}".encode(), hashlib.sha256).hexdigest()[:12]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--max-per-tag", type=int, default=150)
    ap.add_argument("--comment-posts", type=int, default=15)
    ap.add_argument("--max-comments", type=int, default=300)
    ap.add_argument("--pause", type=float, default=4.0)
    a = ap.parse_args()
    key = salt()
    L = instaloader.Instaloader(quiet=True, download_pictures=False, download_videos=False, download_video_thumbnails=False,
                                download_geotags=False, download_comments=False, save_metadata=False, max_connection_attempts=1)
    L.load_session_from_file(a.user)
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "data/raw/instagram" / now
    out.mkdir(parents=True)
    posts, log = {}, []
    with (out / "posts.jsonl").open("w") as fp:
        for tag in TAGS:
            n = seen = 0
            status = "ok"
            try:
                for p in instaloader.Hashtag.from_name(L.context, tag).get_posts_resumable():
                    seen += 1
                    if seen > a.max_per_tag * 3:
                        status = "scan_cap"
                        break
                    d = p.date_utc.replace(tzinfo=timezone.utc)
                    if d < SINCE or p.shortcode in posts:
                        continue
                    rec = {"tag": tag, "shortcode": p.shortcode, "url": f"https://www.instagram.com/p/{p.shortcode}/",
                           "date_utc": d.isoformat(), "caption": p.caption or "", "hashtags": p.caption_hashtags,
                           "likes": p.likes, "comments": p.comments, "is_video": p.is_video,
                           "video_views": p.video_view_count if p.is_video else None, "typename": p.typename,
                           "owner": pseudo(p.owner_id, key), "fetched_at": datetime.now(timezone.utc).isoformat()}
                    posts[p.shortcode] = (rec, p)
                    fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    n += 1
                    if n >= a.max_per_tag:
                        status = "post_cap"
                        break
                    time.sleep(a.pause / 4)
            except Exception as e:  # noqa: BLE001 - record and continue; never retry aggressively
                status = f"error:{type(e).__name__}:{str(e)[:160]}"
            log.append({"tag": tag, "posts_in_window": n, "scanned": seen, "status": status})
            print(log[-1], flush=True)
            time.sleep(a.pause * 3)
    top = sorted(posts.values(), key=lambda rp: -(rp[0]["comments"] or 0))[: a.comment_posts]
    nc = 0
    with (out / "comments.jsonl").open("w") as fc:
        for rec, p in top:
            try:
                for i, c in enumerate(p.get_comments()):
                    if i >= a.max_comments:
                        break
                    fc.write(json.dumps({"shortcode": rec["shortcode"], "comment_id": str(c.id), "parent": None,
                                         "date_utc": c.created_at_utc.replace(tzinfo=timezone.utc).isoformat(),
                                         "text": c.text, "likes": c.likes_count, "author": pseudo(c.owner.userid, key)},
                                        ensure_ascii=False) + "\n")
                    nc += 1
                    for r in c.answers:
                        fc.write(json.dumps({"shortcode": rec["shortcode"], "comment_id": str(r.id), "parent": str(c.id),
                                             "date_utc": r.created_at_utc.replace(tzinfo=timezone.utc).isoformat(),
                                             "text": r.text, "likes": r.likes_count, "author": pseudo(r.owner.userid, key)},
                                            ensure_ascii=False) + "\n")
                        nc += 1
            except Exception as e:  # noqa: BLE001
                log.append({"comments_for": rec["shortcode"], "status": f"error:{type(e).__name__}:{str(e)[:160]}"})
            print(f"comments {rec['shortcode']}: total {nc}", flush=True)
            time.sleep(a.pause * 2)
    man = {"collected_utc": now, "tags": TAGS, "since": SINCE.isoformat(), "posts": len(posts), "comments": nc, "log": log,
           "tool": f"instaloader {instaloader.__version__}", "auth": "user's own session (password not handled by code)",
           "sha256": {f: hashlib.sha256((out / f).read_bytes()).hexdigest() for f in ["posts.jsonl", "comments.jsonl"]}}
    (out / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=2))
    print(json.dumps({"posts": len(posts), "comments": nc}), "->", out)


if __name__ == "__main__":
    main()
