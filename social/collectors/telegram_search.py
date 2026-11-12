"""Keyword search over public Telegram channels through the web preview.

Uses t.me/s/<channel>?q=<term> and pages back with &before=<id>. No login, no API.
Raw HTML pages are stored gzip-compressed with sha256 in the manifest so parsing can be
redone and audited. Parsing reuses src/tgweb_collect.parse_page.

After the seed pass, one snowball hop: channels that appear as forward origins or
t.me links in *relevant* posts (the --relevance regex) are searched too,
up to --snowball-max new channels.

Output: <out>/{pages/,posts.jsonl,channels.jsonl,runs.jsonl,manifest.json}
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import re

from social.collectors import telegram_web as tw

LOCK = threading.Lock()


def read_channels(paths):
    out = []
    for p in paths:
        p = Path(p)
        if p.suffix == ".csv":
            rows = (r for r in csv.reader(l for l in p.open(encoding="utf8") if not l.startswith("#")))
            for r in rows:
                if r and r[0] and r[0] != "channel":
                    out.append(r[0].strip().lower())
        else:
            for line in p.open(encoding="utf8"):
                line = line.split("#", 1)[0].strip()
                if line:
                    out.append((tw.channel_from_url(line) or line).lower())
    seen, uniq = set(), []
    for c in out:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    return uniq


def search_channel(ch, term, start, end, out_pages, manifest, delay, max_pages):
    q = quote(term)
    url = f"https://t.me/s/{ch}?q={q}"
    posts, n, status, info = [], 0, "ok", None
    last_before = None
    while True:
        r = tw.get(url, delay)
        if r is None or r.status_code != 200:
            status = "page_error" if n else "unavailable"
            break
        page = r.text
        if n == 0:
            if "tgme_channel_info" not in page:
                status = "unavailable"
                break
            info = tw.parse_channel_info(page, ch, tw.now().isoformat())
        n += 1
        raw = page.encode()
        name = f"{ch}__{hashlib.sha1(term.encode()).hexdigest()[:8]}_{n:04d}.html.gz"
        (out_pages / name).write_bytes(gzip.compress(raw))
        with LOCK:
            manifest.append({"file": f"pages/{name}", "url": url, "sha256": hashlib.sha256(raw).hexdigest(),
                             "fetched_at": tw.now().isoformat()})
        got = tw.parse_page(page, ch, tw.now().isoformat())
        got = [p for p in got if p["channel"] == ch]
        if not got:
            break
        for p in got:
            p["search_term"] = term
        posts.extend(got)
        oldest = min(got, key=lambda p: p["msg_id"])
        if oldest["date"] and datetime.fromisoformat(oldest["date"]) < start:
            break
        if n >= max_pages:
            status = f"max_pages({max_pages})"
            break
        if last_before is not None and oldest["msg_id"] >= last_before:
            break
        last_before = oldest["msg_id"]
        url = f"https://t.me/s/{ch}?q={q}&before={oldest['msg_id']}"
    in_win = [p for p in posts if p["date"] and start <= datetime.fromisoformat(p["date"]) <= end]
    return info, in_win, {"channel": ch, "term": term, "pages": n, "status": status,
                          "hits_all": len(posts), "hits_window": len(in_win)}


def run_pass(channels, terms, args, out_pages, manifest, posts, infos, runs):
    jobs = [(c, t) for c in channels for t in terms]

    def job(ct):
        c, t = ct
        info, got, run = search_channel(c, t, args.start, args.end, out_pages, manifest, args.delay, args.max_pages)
        with LOCK:
            if info and c not in infos:
                infos[c] = info
            for p in got:
                key = (p["channel"], p["msg_id"])
                if key in posts:
                    posts[key].setdefault("search_terms", [posts[key]["search_term"]])
                    if t not in posts[key]["search_terms"]:
                        posts[key]["search_terms"].append(t)
                else:
                    posts[key] = p
            runs.append(run)
        print(f"  {c:32s} {t:10s} pages={run['pages']:3d} win={run['hits_window']:4d} {run['status']}", flush=True)

    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(job, jobs))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--channels", nargs="+", required=True, help="CSV (first column) or txt lists")
    ap.add_argument("--terms", nargs="+", required=True, help="search terms (each word form separately)")
    ap.add_argument("--relevance", default=None, help="regex: posts matching it drive the snowball (default: any term)")
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--max-pages", type=int, default=60)
    ap.add_argument("--snowball-max", type=int, default=300)
    args = ap.parse_args(argv)
    rel = re.compile(args.relevance or "|".join(re.escape(t) for t in args.terms), re.I)

    def is_relevant(text):
        return bool(rel.search(text or ""))
    args.start = datetime.fromisoformat(args.start)
    args.end = datetime.fromisoformat(args.end) if args.end else datetime.now(timezone.utc)

    out = args.out
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} exists and is not empty; raw snapshots are never overwritten")
    out_pages = out / "pages"
    out_pages.mkdir(parents=True)
    started = tw.now().isoformat()

    seeds = read_channels(args.channels)
    print(f"seed channels: {len(seeds)}", flush=True)
    posts, infos, runs, manifest = {}, {}, [], []
    run_pass(seeds, args.terms, args, out_pages, manifest, posts, infos, runs)

    known = set(seeds)
    cand = {}
    for p in posts.values():
        if not is_relevant(p["text"]):
            continue
        for u in [p.get("fwd_from_url")] + list(p.get("links") or []):
            c = tw.channel_from_url(u or "")
            if c and c not in known:
                cand[c] = cand.get(c, 0) + 1
    hop = [c for c, _ in sorted(cand.items(), key=lambda kv: (-kv[1], kv[0]))][: args.snowball_max]
    print(f"snowball channels: {len(hop)} (candidates {len(cand)})", flush=True)
    run_pass(hop, args.terms, args, out_pages, manifest, posts, infos, runs)

    for p in posts.values():
        p["found_by"] = "seed" if p["channel"] in known else "snowball"
        p["relevant"] = is_relevant(p["text"])
    with (out / "posts.jsonl").open("w", encoding="utf8") as f:
        for key in sorted(posts):
            f.write(json.dumps(posts[key], ensure_ascii=False) + "\n")
    with (out / "channels.jsonl").open("w", encoding="utf8") as f:
        for c in sorted(infos):
            infos[c]["found_by"] = "seed" if c in known else "snowball"
            f.write(json.dumps(infos[c], ensure_ascii=False) + "\n")
    with (out / "runs.jsonl").open("w", encoding="utf8") as f:
        for r in sorted(runs, key=lambda r: (r["channel"], r["term"])):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (out / "manifest.json").write_text(json.dumps({
        "collector": "social.collectors.telegram_search", "relevance_regex": rel.pattern, "started": started, "finished": tw.now().isoformat(),
        "start": args.start.isoformat(), "end": args.end.isoformat(), "terms": args.terms,
        "channel_lists": args.channels, "seed_channels": len(seeds), "snowball_channels": len(hop),
        "snowball_candidates": len(cand), "posts": len(posts),
        "relevant": sum(p["relevant"] for p in posts.values()),
        "pages": sorted(manifest, key=lambda m: m["file"]),
    }, ensure_ascii=False, indent=1), encoding="utf8")
    print(f"done: {len(posts)} posts, {sum(p['relevant'] for p in posts.values())} relevant", flush=True)


if __name__ == "__main__":
    main()
