"""Telegram corpus of a case: every collected snapshot (full timelines and keyword search) merged, de-duplicated,
cleaned of channel boilerplate, and labelled from the case lexicon (relevance reason, group, period, language,
frames, topics, claims). Raw data is only read.

Output: data/processed/corpus/posts.jsonl, summary.json
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .lexicon import Lexicon, load

UK_CH, RU_CH = set("іїєґ"), set("ыэъё")


def norm_line(line: str) -> str:
    return re.sub(r"\W+", " ", line.lower()).strip()


def strip_boilerplate(by_channel: dict[str, list[dict]], min_posts: int = 5, share: float = 0.25) -> None:
    """Lines repeated in >= `share` of a channel's posts (signatures, subscribe links) are removed into text_clean."""
    for ch, ps in by_channel.items():
        boiler: set[str] = set()
        if len(ps) >= min_posts:
            cnt = Counter()
            for p in ps:
                cnt.update({norm_line(x) for x in (p["text"] or "").splitlines() if norm_line(x)})
            boiler = {x for x, c in cnt.items() if c / len(ps) >= share and len(x) < 200}
        for p in ps:
            p["text_clean"] = "\n".join(x for x in (p["text"] or "").splitlines() if norm_line(x) not in boiler).strip()


def lang_of(text: str) -> str:
    t = (text or "").lower()
    uk, ru = sum(c in UK_CH for c in t), sum(c in RU_CH for c in t)
    return "?" if uk == ru == 0 else ("uk" if uk > ru else "ru")


def raw_post_files(case: Path) -> list[Path]:
    return sorted(list((case / "data/raw/telegram_web").glob("*/posts.jsonl")) +
                  list((case / "data/raw/telegram_search").glob("*/posts.jsonl")))


def build(case: Path, lex: Lexicon | None = None) -> dict:
    lex = lex or load(case)
    tz = ZoneInfo(lex.tz)
    posts: dict[tuple, dict] = {}
    files = raw_post_files(case)
    for f in files:
        kind = f.parent.parent.name
        for line in f.open(encoding="utf-8"):
            p = json.loads(line)
            ch = (p.get("channel") or "").lower()
            if not ch or not p.get("msg_id") or not p.get("date"):
                continue
            key = (ch, int(p["msg_id"]))
            old = posts.get(key)
            p = {**p, "channel": ch, "source": kind}
            if old is None or (p.get("views") or 0) >= (old.get("views") or 0):  # keep the latest/most-viewed capture
                posts[key] = p
    by_ch = defaultdict(list)
    for p in posts.values():
        by_ch[p["channel"]].append(p)
    strip_boilerplate(by_ch)
    out = []
    for p in posts.values():
        t = datetime.fromisoformat(p["date"].replace("Z", "+00:00")).astimezone(tz)
        txt = p["text_clean"]
        reason = lex.relevance(txt)
        p.update({"t_local": t.isoformat(), "day": t.date().isoformat(), "relevance": reason, "relevant": reason == "relevant",
                  "group": lex.groups.get(p["channel"], "U"), "period": lex.period(t.date().isoformat()),
                  "lang": lang_of(txt), "frames": lex.match(lex.frames, txt) if reason == "relevant" else [],
                  "topics": lex.match(lex.topics, txt), "claims": lex.match(lex.claims, txt)})
        out.append(p)
    out.sort(key=lambda p: (p["t_local"], p["url"]))
    d = case / "data/processed/corpus"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "posts.jsonl").open("w", encoding="utf-8") as fo:
        for p in out:
            fo.write(json.dumps(p, ensure_ascii=False) + "\n")
    rel = [p for p in out if p["relevant"]]
    summary = {"raw_files": [str(f.relative_to(case)) for f in files], "posts": len(out), "channels": len(by_ch),
               "relevance": dict(Counter(p["relevance"] for p in out)), "relevant": len(rel),
               "relevant_channels": len({p["channel"] for p in rel}),
               "relevant_by_group": dict(Counter(p["group"] for p in rel)),
               "relevant_by_period": dict(Counter(p["period"] for p in rel)),
               "relevant_by_lang": dict(Counter(p["lang"] for p in rel)),
               "relevant_views": sum(p.get("views") or 0 for p in rel),
               "unclassified_relevant_channels": sorted({p["channel"] for p in rel if p["group"] == "U"})[:200]}
    (d / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def load_posts(case: Path, relevant_only: bool = True) -> list[dict]:
    p = case / "data/processed/corpus/posts.jsonl"
    if not p.exists():
        raise FileNotFoundError("корпусу ще немає: спершу analyze_corpus")
    return [x for x in map(json.loads, p.open(encoding="utf-8")) if x["relevant"] or not relevant_only]
