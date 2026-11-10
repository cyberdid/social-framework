"""Snapshot public Telegram channels through the public web preview (t.me/s/<channel>).

No login, no API keys. Only public channels. For every channel we page back through
its timeline (?before=<id>) until the post date is earlier than --start, keep every
post in the window (relevance filtering happens later, in analysis), and store the raw
HTML pages (gzip) next to the parsed JSONL so parsing can be redone and audited.

Snowball: channels that appear as forward origins or t.me links in *relevant* posts
(matching --relevance) are crawled too, up to --depth hops and --max-channels total.

Output: data/raw/telegram_web/<UTC snapshot>/{pages/,posts.jsonl,channels.jsonl,manifest.json}
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

ROOT = Path(__file__).resolve().parents[1]
UA = "Mozilla/5.0 (X11; Linux x86_64) research-snapshot (public channels only)"
DEFAULT_REL = r"борщаг[іi]в|борщагов|borshch?ah?iv|borshchagov|петропавл[іi]вськ\w*\s+борщ|петропавловск\w*\s+борщ"

S = requests.Session()
S.headers["User-Agent"] = UA


def now():
    return datetime.now(timezone.utc)


def get(url, delay, tries=5):
    for i in range(tries):
        try:
            r = S.get(url, timeout=30)
        except requests.RequestException:
            time.sleep(5 * (i + 1))
            continue
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 30))
            print(f"    429, sleeping {wait}s", flush=True)
            time.sleep(wait)
            continue
        time.sleep(delay)
        return r
    return None


def strip_tags(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


def parse_count(s):
    if s is None:
        return None
    s = s.strip().replace(",", ".")
    mult = 1
    if s.endswith("K"):
        mult, s = 1_000, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000, s[:-1]
    try:
        return int(round(float(s) * mult))
    except ValueError:
        return None


MSG_SPLIT = re.compile(r'(?=<div class="tgme_widget_message_wrap)')


def parse_page(page: str, channel: str, fetched_at: str):
    posts = []
    for block in MSG_SPLIT.split(page)[1:]:
        m = re.search(r'data-post="([^"/]+)/(\d+)"', block)
        if not m:
            continue
        username, mid = m.group(1), int(m.group(2))
        t = re.search(r'<time datetime="([^"]+)"', block)
        txt = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        views = re.search(r'tgme_widget_message_views">([^<]+)<', block)
        fwd = re.search(r'tgme_widget_message_forwarded_from_name"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        fwd_noname = re.search(r'tgme_widget_message_forwarded_from_name"[^>]*>(?:<span[^>]*>)?([^<]+)', block)
        reply = re.search(r'tgme_widget_message_reply"[^>]*href="([^"]+)"', block)
        author = re.search(r'tgme_widget_message_from_author"[^>]*>([^<]+)<', block)
        links = re.findall(r'href="(https?://[^"]+)"', txt.group(1)) if txt else []
        media = sorted(set(re.findall(r"tgme_widget_message_(photo|video|document|voice|roundvideo|poll|sticker)_?wrap", block)))
        n_media = len(re.findall(r"tgme_widget_message_(?:photo|video)_wrap", block))
        reactions = [(html.unescape(e), parse_count(c)) for e, c in
                     re.findall(r'<span class="tgme_reaction[^"]*">(?:<i[^>]*><b>([^<]*)</b></i>|<i[^>]*></i>)?([^<]*)</span>', block)]
        posts.append({
            "channel": username.lower(), "msg_id": mid,
            "url": f"https://t.me/{username}/{mid}",
            "date": t.group(1) if t else None,
            "edited": "tgme_widget_message_meta" in block and ">edited<" in block.replace(" ", ""),
            "text": strip_tags(txt.group(1)) if txt else "",
            "views": parse_count(views.group(1)) if views else None,
            "views_raw": views.group(1) if views else None,
            "fwd_from_url": fwd.group(1) if fwd else None,
            "fwd_from_name": strip_tags(fwd.group(2)) if fwd else (strip_tags(fwd_noname.group(1)) if fwd_noname else None),
            "reply_to_url": reply.group(1) if reply else None,
            "author": html.unescape(author.group(1)).strip() if author else None,
            "links": links,
            "media": media, "n_media": n_media,
            "reactions": [r for r in reactions if r[1] is not None],
            "fetched_at": fetched_at,
        })
    return posts


def parse_channel_info(page: str, channel: str, fetched_at: str):
    title = re.search(r'<meta property="og:title" content="([^"]*)"', page)
    desc = re.search(r'<div class="tgme_channel_info_description">(.*?)</div>', page, re.S)
    counters = dict((k, v) for v, k in re.findall(
        r'<span class="counter_value">([^<]+)</span>\s*<span class="counter_type">([^<]+)</span>', page))
    verified = "verified-icon" in page[:20000]
    return {"channel": channel, "title": html.unescape(title.group(1)) if title else None,
            "description": strip_tags(desc.group(1)) if desc else None,
            "subscribers": parse_count(counters.get("subscribers")) if counters.get("subscribers") else None,
            "counters": counters, "verified": verified, "fetched_at": fetched_at}


def crawl_channel(ch, start, end, out_pages, delay, max_pages):
    fetched = []
    url = f"https://t.me/s/{ch}"
    first = get(url, delay)
    if first is None or first.status_code != 200 or "tgme_channel_info" not in first.text:
        return None, [], "unavailable"
    fa = now().isoformat()
    info = parse_channel_info(first.text, ch, fa)
    page, before, n = first.text, None, 0
    while True:
        n += 1
        (out_pages / f"{ch}_{n:04d}.html.gz").write_bytes(gzip.compress(page.encode()))
        posts = parse_page(page, ch, fa)
        if not posts:
            break
        fetched.extend(posts)
        oldest = min(posts, key=lambda p: p["msg_id"])
        if oldest["date"] and datetime.fromisoformat(oldest["date"]) < start:
            break
        if n >= max_pages:
            return info, fetched, f"max_pages({max_pages})"
        before = oldest["msg_id"]
        r = get(f"https://t.me/s/{ch}?before={before}", delay)
        if r is None or r.status_code != 200:
            return info, fetched, "page_error"
        page, fa = r.text, now().isoformat()
    in_win = [p for p in fetched if p["date"] and start <= datetime.fromisoformat(p["date"]) <= end]
    return info, in_win, "ok"


def channel_from_url(u: str):
    m = re.match(r"https?://t\.me/(?:s/)?([A-Za-z0-9_]{4,})(?:/\d+)?", u or "")
    if not m:
        return None
    name = m.group(1).lower()
    return None if name in {"joinchat", "addstickers", "share", "proxy", "s", "c", "iv"} else name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", default="config/borshchahivka_seeds.txt")
    ap.add_argument("--start", default="2026-11-02T00:00:00+02:00")
    ap.add_argument("--end", default=None)
    ap.add_argument("--relevance", default=DEFAULT_REL)
    ap.add_argument("--depth", type=int, default=2)
    ap.add_argument("--max-channels", type=int, default=400)
    ap.add_argument("--max-pages", type=int, default=150)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    start = datetime.fromisoformat(a.start)
    end = datetime.fromisoformat(a.end) if a.end else now()
    rel = re.compile(a.relevance, re.I)
    seeds = [l.strip().lower() for l in (ROOT / a.seeds).read_text(encoding="utf-8").splitlines()
             if l.strip() and not l.startswith("#")]
    snap = now().strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "data/raw/telegram_web" / snap
    (out / "pages").mkdir(parents=True)
    fposts = (out / "posts.jsonl").open("w", encoding="utf-8")
    fch = (out / "channels.jsonl").open("w", encoding="utf-8")

    from concurrent.futures import ThreadPoolExecutor
    level = [(s, "seed") for s in seeds]
    seen, status, counts = set(), {}, {"posts": 0, "relevant": 0}
    pending = 0
    for d in range(a.depth + 1):
        level = [(c, why) for c, why in dict(level).items() if c not in seen][: max(0, a.max_channels - len(seen))]
        if not level:
            break
        seen.update(c for c, _ in level)
        nxt = []
        with ThreadPoolExecutor(a.workers) as ex:
            futs = {ex.submit(crawl_channel, c, start, end, out / "pages", a.delay, a.max_pages): (c, why) for c, why in level}
            for f in futs:
                ch, why = futs[f]
                info, posts, st = f.result()
                status[ch] = {"status": st, "depth": d, "via": why, "posts_in_window": len(posts)}
                if info:
                    info.update({"depth": d, "via": why})
                    fch.write(json.dumps(info, ensure_ascii=False) + "\n")
                nrel = 0
                for p in posts:
                    p["relevant"] = bool(rel.search(p["text"] or ""))
                    fposts.write(json.dumps(p, ensure_ascii=False) + "\n")
                    counts["posts"] += 1
                    if p["relevant"]:
                        nrel += 1
                        cands = [p["fwd_from_url"]] + [u for u in p["links"] if re.match(r"https?://t\.me/[A-Za-z0-9_]+/\d+", u)]
                        for u in cands:
                            c = channel_from_url(u)
                            if c and c not in seen and c != ch:
                                nxt.append((c, f"{ch}/{p['msg_id']}"))
                counts["relevant"] += nrel
                status[ch]["relevant"] = nrel
                print(f"[{len(status):3d}] d{d} {ch:28s} {st:14s} window={len(posts):5d} relevant={nrel:4d} next={len(nxt)}", flush=True)
                fposts.flush(); fch.flush()
        level = nxt
        pending = len(set(c for c, _ in nxt) - seen)
    queue = [None] * pending
    fposts.close(); fch.close()

    sha = {}
    for f in ["posts.jsonl", "channels.jsonl"]:
        sha[f] = hashlib.sha256((out / f).read_bytes()).hexdigest()
    manifest = {"snapshot_utc": snap, "finished_utc": now().isoformat(), "window": [start.isoformat(), end.isoformat()],
                "method": "t.me/s public web preview, full timeline in window, regex relevance, forward/link snowball",
                "relevance_regex": a.relevance, "depth": a.depth, "max_channels": a.max_channels,
                "seeds": seeds, "counts": counts, "channels": status, "unvisited_queue": len(queue), "sha256": sha}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(counts), "->", out)


if __name__ == "__main__":
    main()
