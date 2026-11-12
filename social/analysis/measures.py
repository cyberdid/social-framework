"""Descriptive measures over the case corpus: volume, frames, claim spread, audience mood (reactions), versions.

All definitions come from the case lexicon. Shares carry Wilson 95 % intervals; view-weighted shares are given
alongside because views are counted at snapshot time (older posts have more).
Outputs go to data/processed/analysis/<measure>/.
"""
from __future__ import annotations

import csv
import gzip
import json
import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from .corpus import load_posts
from .lexicon import Lexicon, load


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if not n:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - h) / d), min(1.0, (c + h) / d))


def _out(case: Path, name: str) -> Path:
    d = case / "data/processed/analysis" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    cols = list(dict.fromkeys(k for r in rows for k in r))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def volume(case: Path) -> dict:
    posts = load_posts(case)
    rows = Counter((p["day"], p["group"]) for p in posts)
    out = _out(case, "volume")
    _csv(out / "daily_by_group.csv", [{"day": d, "group": g, "posts": n} for (d, g), n in sorted(rows.items())])
    top = Counter(p["channel"] for p in posts)
    views = Counter()
    for p in posts:
        views[p["channel"]] += p.get("views") or 0
    res = {"days": len({d for d, _ in rows}), "peak_days": [d for d, _ in Counter(p["day"] for p in posts).most_common(5)],
           "top_channels_by_posts": top.most_common(20), "top_channels_by_views": views.most_common(20)}
    (out / "volume.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def frames(case: Path, lex: Lexicon | None = None) -> dict:
    lex = lex or load(case)
    posts = load_posts(case)
    cells = defaultdict(list)
    for p in posts:
        for key in [(p["group"], "усі"), (p["group"], p["period"] or "—"), ("усі", "усі")]:
            cells[key].append(p)
    rows = []
    for (g, per), ps in sorted(cells.items()):
        n, v = len(ps), sum(p.get("views") or 0 for p in ps)
        row = {"group": g, "period": per, "posts": n, "views": v,
               "no_frame_share": round(sum(not p["frames"] for p in ps) / n, 4) if n else None}
        for k, d in lex.frames.items():
            hit = [p for p in ps if k in p["frames"]]
            lo, hi = wilson(len(hit), n)
            row.update({f"{k}_n": len(hit), f"{k}_share": round(len(hit) / n, 4) if n else None,
                        f"{k}_ci": f"{lo:.3f}–{hi:.3f}", f"{k}_view_share": round(sum(p.get("views") or 0 for p in hit) / v, 4) if v else None})
        rows.append(row)
    out = _out(case, "frames")
    _csv(out / "frames_by_group_period.csv", rows)
    res = {"frames": {k: d["label"] for k, d in lex.frames.items()}, "cells": len(rows),
           "overall": next((r for r in rows if r["group"] == "усі"), {})}
    (out / "frames.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def claims(case: Path, lex: Lexicon | None = None) -> dict:
    """Spread of each claim: first post, posts/channels by group, views, and how much came before the official document."""
    lex = lex or load(case)
    posts = load_posts(case, relevant_only=False)
    posts = [p for p in posts if p["relevant"] or p["claims"]]
    res, rows = {}, []
    for k, d in lex.claims.items():
        m = sorted([p for p in posts if k in p["claims"]], key=lambda p: p["t_local"])
        if not m:
            res[k] = {"label": d["label"], "posts": 0}
            continue
        by_g = Counter(p["group"] for p in m)
        r = {"label": d["label"], "posts": len(m), "channels": len({p["channel"] for p in m}), "posts_by_group": dict(by_g),
             "views": sum(p.get("views") or 0 for p in m),
             "first": {x: m[0][x] for x in ("t_local", "channel", "group", "url")},
             "first_by_group": {g: next({x: p[x] for x in ("t_local", "channel", "url")} for p in m if p["group"] == g) for g in by_g}}
        if d.get("official_at"):
            before = [p for p in m if p["t_local"][:16] < str(d["official_at"])[:16]]
            r.update({"official_at": d["official_at"], "posts_before_official": len(before),
                      "views_before_official": sum(p.get("views") or 0 for p in before)})
        res[k] = r
        rows += [{"claim": k, "t_local": p["t_local"], "channel": p["channel"], "group": p["group"], "views": p.get("views"),
                  "url": p["url"], "relevant": p["relevant"], "text": re.sub(r"\s+", " ", p["text_clean"])[:400]} for p in m]
    out = _out(case, "claims")
    _csv(out / "claim_posts.csv", rows)
    (out / "claims.json").write_text(json.dumps({"regex": {k: d["regex"] for k, d in lex.claims.items()}, "claims": res},
                                                ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def mood(case: Path, lex: Lexicon | None = None) -> dict:
    """Emoji reactions -> emotions, by group, topic and period. A reaction is to the post, not to a person or agency."""
    lex = lex or load(case)
    posts = [p for p in load_posts(case) if p.get("reactions")]
    by = {"group": defaultdict(Counter), "topic": defaultdict(Counter), "period": defaultdict(Counter)}
    for p in posts:
        c = Counter()
        for r in p["reactions"]:
            e, n = (r[0], r[1]) if isinstance(r, (list, tuple)) else (r.get("r"), r.get("n"))
            c[lex.emoji.get(e, "інше")] += n or 0
        by["group"][p["group"]] += c
        by["period"][p["period"] or "—"] += c
        for t in p["topics"]:
            by["topic"][lex.topics[t]["label"]] += c

    def shares(c):
        t = sum(c.values())
        return {k: round(v / t, 4) for k, v in sorted(c.items())} | {"_total": t} if t else {}

    res = {dim: {k: shares(c) for k, c in d.items()} for dim, d in by.items()}
    res["posts_with_reactions"] = len(posts)
    out = _out(case, "mood")
    (out / "mood.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def comment_sample(case: Path, n: int = 400, seed: int = 20261111, regex: str | None = None) -> dict:
    """Stratified random sample of comments (YouTube and any data/raw/*/comments.jsonl) for manual coding."""
    lex = load(case)
    rx = re.compile(regex, re.I) if regex else (lex.mention or None)
    pool = []
    for f in sorted((case / "data/raw").glob("**/comments.jsonl")):
        src = f.relative_to(case / "data/raw").parts[0]
        for line in f.open(encoding="utf-8"):
            c = json.loads(line)
            t = c.get("text") or ""
            if len(t) >= 15 and (rx is None or rx.search(t) or (lex.context and lex.context.search(t))):
                pool.append({"source": src, "id": c.get("comment_id") or c.get("id"), "text": t[:1000],
                             "parent": c.get("video_id") or c.get("post_url") or c.get("link_id")})
    rnd = random.Random(seed)
    by_src = defaultdict(list)
    for c in pool:
        by_src[c["source"]].append(c)
    k_total = min(n, len(pool))
    sample = []
    for s, cs in by_src.items():
        k = round(k_total * len(cs) / len(pool)) if pool else 0
        sample += rnd.sample(cs, min(k, len(cs)))
    out = _out(case, "comments")
    _csv(out / "coding_sample.csv", [{**c, "code": "", "attitude": "", "note": ""} for c in sample])
    texts = Counter(re.sub(r"\W+", " ", c["text"].lower()).strip() for c in pool)
    copies = [{"text": t, "count": k} for t, k in texts.most_common(50) if k >= 3 and len(t) > 10]
    res = {"pool": len(pool), "by_source": {s: len(v) for s, v in by_src.items()}, "sample": len(sample),
           "repeated_texts_ge3": copies, "how_to_code": "fill columns code/attitude/note in coding_sample.csv; report shares with Wilson CIs"}
    (out / "comments.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def _text_sources(case: Path):
    """(source, id, url, time, text) over posts, comments, archived pages and article texts."""
    for p in load_posts(case, relevant_only=False):
        yield "telegram", p["url"], p["url"], p["t_local"], p["text_clean"]
    for f in sorted((case / "data/raw").glob("**/comments.jsonl")):
        for line in f.open(encoding="utf-8"):
            c = json.loads(line)
            yield "comments", c.get("comment_id") or c.get("id"), c.get("video_id") or "", str(c.get("timestamp") or ""), c.get("text") or ""
    for f in sorted((case / "data/processed/web_text").glob("*.txt")):
        yield "web", f.stem, "", "", f.read_text(encoding="utf-8", errors="replace")
    for f in sorted((case / "data/raw").glob("**/*.html.gz")):
        if "pages" in f.parts:
            continue
        try:
            raw = gzip.decompress(f.read_bytes()).decode("utf-8", "replace")
        except OSError:
            continue
        txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)))
        yield "archive", f.name, "", "", txt


def versions(case: Path, lex: Lexicon | None = None) -> dict:
    """Catalogue of explanatory versions across every text source: where each version appears, how often, examples."""
    lex = lex or load(case)
    hits = defaultdict(list)
    for src, sid, url, t, text in _text_sources(case):
        for k, d in lex.versions.items():
            m = d["rx"].search(text or "")
            if m:
                a, b = max(0, m.start() - 160), min(len(text), m.end() + 160)
                hits[k].append({"source": src, "id": sid, "url": url, "time": t, "snippet": re.sub(r"\s+", " ", text[a:b])})
    res = {k: {"label": d["label"], "mentions": len(hits[k]), "by_source": dict(Counter(h["source"] for h in hits[k])),
               "examples": hits[k][:8]} for k, d in lex.versions.items()}
    out = _out(case, "versions")
    _csv(out / "version_mentions.csv", [{"version": k, **h} for k, hs in hits.items() for h in hs])
    (out / "versions.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res
