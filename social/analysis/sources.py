"""Cross-source measures: search completeness (recall), media layer, actor co-mentions, reciprocal official actions."""
from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .corpus import load_posts
from .lexicon import Lexicon, load
from .measures import _csv, _out, _text_sources


def recall(case: Path, lex: Lexicon | None = None) -> dict:
    """How much of what full timelines contain did keyword search find? Truth = relevant posts in full-timeline
    snapshots (telegram_web) for channels also searched, within the search window."""
    lex = lex or load(case)
    full, search, windows = {}, set(), {}
    for f in (case / "data/raw/telegram_web").glob("*/posts.jsonl"):
        for p in map(json.loads, f.open(encoding="utf-8")):
            full[(p["channel"].lower(), int(p["msg_id"]))] = p
    for d in (case / "data/raw/telegram_search").glob("*"):
        m = d / "manifest.json"
        if m.exists():
            mf = json.loads(m.read_text(encoding="utf-8"))
            windows[d.name] = (mf.get("start"), mf.get("end"))
        if (d / "posts.jsonl").exists():
            for p in map(json.loads, (d / "posts.jsonl").open(encoding="utf-8")):
                search.add((p["channel"].lower(), int(p["msg_id"])))
    searched_channels = {c for c, _ in search}
    lo = min((w[0] for w in windows.values() if w[0]), default=None)
    hi = max((w[1] for w in windows.values() if w[1]), default=None)
    truth = [k for k, p in full.items() if k[0] in searched_channels and lex.relevance(p.get("text") or "") == "relevant"
             and (not lo or p["date"] >= lo) and (not hi or p["date"] <= hi)]
    found = [k for k in truth if k in search]
    missing = Counter(k[0] for k in truth if k not in search)
    res = {"truth_posts": len(truth), "found": len(found), "recall": round(len(found) / len(truth), 4) if truth else None,
           "channels_compared": len({k[0] for k in truth}), "missing_by_channel": missing.most_common(30),
           "note": "needs both full timelines (collect_telegram) and keyword search (collect_telegram_search) of the same channels"}
    (_out(case, "recall") / "recall.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def media(case: Path, lex: Lexicon | None = None) -> dict:
    """Online media layer from Google News / GDELT items: outlets by origin, daily volume, claims in headlines."""
    lex = lex or load(case)
    items, seen = [], set()
    for f in sorted((case / "data/raw").glob("*/items.jsonl")):
        for r in map(json.loads, f.open(encoding="utf-8")):
            key = r.get("gnews_link") or r.get("url") or (r.get("title"), r.get("source_name"))
            if key in seen:
                continue
            seen.add(key)
            url = r.get("source_url") or r.get("url") or ""
            dom = url.split("://")[-1].split("/")[0].lower().removeprefix("www.")
            pub = r.get("published") or r.get("seendate") or ""
            origin = next((o for d, o in lex.media_origins.items() if dom == d or dom.endswith("." + d)), "?")
            items.append({"day": pub[:10], "domain": dom, "outlet": r.get("source_name") or dom, "origin": origin,
                          "title": r.get("title") or "", "claims": "|".join(lex.match(lex.claims, r.get("title") or "")),
                          "relevant": lex.relevance(r.get("title") or "") == "relevant"})
    out = _out(case, "media")
    _csv(out / "media_items.csv", items)
    daily = Counter((i["day"], i["origin"]) for i in items)
    _csv(out / "daily_by_origin.csv", [{"day": d, "origin": o, "items": n} for (d, o), n in sorted(daily.items())])
    res = {"items": len(items), "by_origin": dict(Counter(i["origin"] for i in items)),
           "top_outlets": Counter(i["outlet"] for i in items).most_common(25),
           "claims_in_headlines": dict(Counter(c for i in items for c in i["claims"].split("|") if c)),
           "unclassified_domains": Counter(i["domain"] for i in items if i["origin"] == "?").most_common(40)}
    (out / "media.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


SENT = re.compile(r"(?<=[.!?…])\s+|\n+")


def relations(case: Path, lex: Lexicon | None = None, max_examples: int = 5) -> dict:
    """Candidate relations: sentences that name >= 2 lexicon actors. Hints for manual adjudication, not facts."""
    lex = lex or load(case)
    pairs = defaultdict(list)
    for src, sid, url, t, text in _text_sources(case):
        for s in SENT.split(text or ""):
            if len(s) > 600:
                s = s[:600]
            names = sorted({a for a, rx in lex.actors.items() if rx.search(s)})
            for i in range(len(names)):
                for j in range(i + 1, len(names)):
                    pairs[(names[i], names[j])].append({"source": src, "url": url, "time": t, "sentence": s.strip()})
    rows = [{"a": a, "b": b, "sentences": len(v), "sources": "|".join(sorted({x["source"] for x in v})),
             "examples": " ¶ ".join(x["sentence"][:220] for x in v[:max_examples])} for (a, b), v in pairs.items()]
    rows.sort(key=lambda r: -r["sentences"])
    out = _out(case, "relations")
    _csv(out / "actor_pairs.csv", rows)
    res = {"pairs": len(rows), "top": [{k: r[k] for k in ("a", "b", "sentences")} for r in rows[:30]],
           "note": "co-mention is not a relation; check each pair in the sources before drawing an edge"}
    (out / "relations.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def reciprocity(case: Path, lex: Lexicon | None = None, permutations: int = 999, seed: int = 20261111) -> dict:
    """Official actions of one body against staff of another (from the bodies' own channels), and replies B->A
    within window_days after A->B. Permutation null: action dates shuffled among actions. Descriptive; says nothing
    about intent."""
    lex = lex or load(case)
    cfg = lex.raw.get("reciprocity") or {}
    bodies = {k.lower(): v for k, v in (cfg.get("bodies") or {}).items()}
    targets = {b: re.compile(rx, re.I) for b, rx in (cfg.get("targets") or {}).items()}
    action = re.compile(cfg.get("action") or r"обшук\w*|підозр\w*|затрима\w*", re.I)
    win = timedelta(days=int(cfg.get("window_days") or 14))
    if not bodies or not targets:
        raise ValueError("заповніть reciprocity.bodies і reciprocity.targets у lexicon.yaml")
    acts = []
    for p in load_posts(case, relevant_only=False):
        actor = bodies.get(p["channel"])
        if not actor or not action.search(p["text_clean"]):
            continue
        for tgt, rx in targets.items():
            if tgt != actor and rx.search(p["text_clean"]):
                acts.append({"t": p["t_local"], "actor": actor, "target": tgt, "url": p["url"],
                             "text": re.sub(r"\s+", " ", p["text_clean"])[:300]})
    acts.sort(key=lambda a: a["t"])

    def pairs(ts):
        found = []
        for i, a in enumerate(acts):
            for j, b in enumerate(acts):
                if b["actor"] == a["target"] and b["target"] == a["actor"] and timedelta(0) < ts[j] - ts[i] <= win:
                    found.append((i, j))
        return found

    ts = [datetime.fromisoformat(a["t"]) for a in acts]
    obs = pairs(ts)
    rnd = random.Random(seed)
    null = []
    for _ in range(permutations if acts else 0):
        sh = ts[:]
        rnd.shuffle(sh)
        null.append(len(pairs(sh)))
    out = _out(case, "reciprocity")
    _csv(out / "actions.csv", acts)
    _csv(out / "reciprocal_pairs.csv", [{"first": acts[i]["url"], "first_t": acts[i]["t"], "first_actor": acts[i]["actor"],
                                         "reply": acts[j]["url"], "reply_t": acts[j]["t"], "reply_actor": acts[j]["actor"]}
                                        for i, j in obs])
    res = {"actions": len(acts), "by_actor": dict(Counter(a["actor"] for a in acts)), "reciprocal_pairs": len(obs),
           "null_mean": round(sum(null) / len(null), 2) if null else None,
           "p_value_one_sided": round((1 + sum(n >= len(obs) for n in null)) / (1 + len(null)), 4) if null else None,
           "window_days": win.days, "note": "every detected action should be checked by hand (rule-based detection)"}
    (out / "reciprocity.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res
