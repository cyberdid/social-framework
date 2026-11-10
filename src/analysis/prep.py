"""Load a Telegram web snapshot, strip channel boilerplate, mark relevance, compute basic channel features.

Writes data/processed/<snapshot>/posts.parquet-like JSONL (posts_clean.jsonl) and channels_features.csv.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
KYIV = timezone(timedelta(hours=2))  # EET, November 2026 (no DST)

# Mentions of the place (uk/ru/en word forms). Footers removed before matching.
PLACE = re.compile(r"борщаг[іi]в|борщагов|borshch?ah?iv|borshchagov|borschag|петропавл[іi]вськ|петропавловск|\bппб\b", re.I)
LEX = re.compile(r"тцк|нгу|нацгвард|нац\.? ?гвард|національн\w* гвард|национальн\w* гвард|заручник|заложник|підвал|подвал|концтаб|концлаг|"
                 r"утриму|утримув|удержив|удерж|сзч|соч\b|азов|катівн|катува|пыт|пытк|тортур|швидк|скор(?:ая|ой|ую)|медик|медсестр|лікар|врач|"
                 r"поліці|полици|буданов|півненк|пивненк|натовп|толп|протест|бунт|мобіліз|мобилиз|ворот|паркан|забор|в/ч|3121|військов|военн|"
                 r"бусиф|викуп|выкуп|хартія|хартия|дбр|гбр|сорок|\b40\b|в'язн|вязн|неволі|неволе|загинув|помер|убит|вбит|тцк|"
                 r"сутичк|стычк|потасов|бійк|драк|барикад|баррикад|облива|полива|стрім|стрим|копів|копов|силовик|"
                 r"глав\w* оп\b|голов\w* оп\b|муданов|несовершеннолет|неповнолітн|підліт|подрост|тюрьм|в.язниц|"
                 r"событи|подій|події|рука кремля|нарезка|антикриз|садизм", re.I)
# Full village name is specific (unlike "Борщагівка", which is also a Kyiv district / tram line).
VILLAGE = re.compile(r"петропавл[іi]вськ\w*\s+борщаг|петропавловск\w*\s+борщаг|petropavl\w*\s+borsh|\bппб\b", re.I)
# Kyiv street/tram-line names that are not the village.
KYIV_PLACES = re.compile(r"борщагівськ\w*\s+(?:лін|вул|швидкісн|трамва)\w*|борщаговск\w*\s+(?:лин|ул)\w*|(?:вул|ул)\.?\s+борщаг\w+|(?:вулиц[яіюе]|улиц[аеуы])\s+борщаг\w+|"
                         r"борщагівськ\w*\s+(?:громад|сільськ)\w*|софіївськ\w*\s+борщаг\w*|софиевск\w*\s+борщаг\w*", re.I)
INCIDENT_FROM = "2026-11-02T11:00:00+02:00"
UK_CH = set("іїєґ")
RU_CH = set("ыэъё")


def latest_snapshot(kind="telegram_web") -> Path:
    snaps = sorted((ROOT / "data/raw" / kind).iterdir())
    return snaps[-1]


def norm_line(l: str) -> str:
    return re.sub(r"\W+", " ", l.lower()).strip()


def strip_boilerplate(posts_by_ch: dict[str, list[dict]], min_posts=5, share=0.25):
    """Lines repeated in >= `share` of a channel's posts (signatures, subscribe links) are removed."""
    boiler = {}
    for ch, ps in posts_by_ch.items():
        if len(ps) < min_posts:
            boiler[ch] = set()
            continue
        cnt = Counter()
        for p in ps:
            cnt.update({norm_line(l) for l in (p["text"] or "").splitlines() if norm_line(l)})
        boiler[ch] = {l for l, c in cnt.items() if c / len(ps) >= share and len(l) < 200}
    for ch, ps in posts_by_ch.items():
        for p in ps:
            lines = [l for l in (p["text"] or "").splitlines() if norm_line(l) not in boiler[ch]]
            p["text_clean"] = "\n".join(lines).strip()
    return boiler


def lang_of(text: str) -> str:
    t = text.lower()
    uk = sum(c in UK_CH for c in t)
    ru = sum(c in RU_CH for c in t)
    if uk == ru == 0:
        return "?"
    return "uk" if uk > ru else "ru"


def relevance_decision(text_clean: str, t_kyiv: str) -> tuple[bool, str]:
    """Return the legacy relevance decision and a stable reason code.

    Keep the branch order aligned with the historical selection rule: first the
    incident cutoff, then the explicit village name, then place plus topic cues.
    """
    place = bool(PLACE.search(KYIV_PLACES.sub(" ", text_clean)))
    village = bool(VILLAGE.search(text_clean))
    topic = bool(LEX.search(text_clean))
    if t_kyiv < INCIDENT_FROM:
        return False, "outside_time_window"
    if village:
        return True, "explicit_village_name"
    if place and topic:
        return True, "place_plus_topic"
    if place:
        return False, "place_without_topic"
    if topic:
        return False, "topic_without_place"
    return False, "neither"


def main(snapshot: str | None = None):
    """`snapshot` may be one name or several joined by '+' (merged; a channel is taken from the first snapshot
    that contains it, so the same channel is never counted twice)."""
    names = snapshot.split("+") if snapshot else [latest_snapshot().name]
    out = ROOT / "data/processed" / "+".join(names)
    out.mkdir(parents=True, exist_ok=True)
    posts, chans = [], {}
    for name in names:
        snap = ROOT / "data/raw/telegram_web" / name
        new = set()
        for l in (snap / "channels.jsonl").open(encoding="utf-8"):
            c = json.loads(l)
            if c["channel"] not in chans:
                chans[c["channel"]] = c | {"snapshot": name}
                new.add(c["channel"])
        for l in (snap / "posts.jsonl").open(encoding="utf-8"):
            p = json.loads(l)
            if p["channel"] in new:
                posts.append(p)
    # dedupe (a channel can only be crawled once, but be safe)
    uniq = {(p["channel"], p["msg_id"]): p for p in posts}
    posts = list(uniq.values())
    by_ch = defaultdict(list)
    for p in posts:
        by_ch[p["channel"]].append(p)
    boiler = strip_boilerplate(by_ch)
    for p in posts:
        dt = datetime.fromisoformat(p["date"])
        p["t_kyiv"] = dt.astimezone(KYIV).isoformat()
        tc = p["text_clean"]
        p["place"] = bool(PLACE.search(KYIV_PLACES.sub(" ", tc)))
        p["relevant"], p["relevance_reason_code"] = relevance_decision(tc, p["t_kyiv"])
        p["lang"] = lang_of(p["text_clean"])
        p["fwd_channel"] = None
        m = re.match(r"https?://t\.me/([A-Za-z0-9_]+)/(\d+)", p.get("fwd_from_url") or "")
        if m:
            p["fwd_channel"], p["fwd_msg_id"] = m.group(1).lower(), int(m.group(2))
    with (out / "posts_clean.jsonl").open("w", encoding="utf-8") as f:
        for p in sorted(posts, key=lambda p: p["date"]):
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
    rows = []
    for ch, ps in by_ch.items():
        langs = Counter(p["lang"] for p in ps)
        rel = [p for p in ps if p["relevant"]]
        c = chans.get(ch, {})
        rows.append({
            "channel": ch, "title": c.get("title"), "subscribers": c.get("subscribers"),
            "depth": c.get("depth"), "via": c.get("via"),
            "posts_window": len(ps), "posts_relevant": len(rel),
            "views_relevant": sum(p["views"] or 0 for p in rel),
            "share_uk": round(langs["uk"] / max(1, langs["uk"] + langs["ru"]), 3),
            "first_relevant_kyiv": min((p["t_kyiv"] for p in rel), default=None),
            "boilerplate_lines": len(boiler.get(ch, ())),
            "description": (c.get("description") or "")[:300].replace("\n", " "),
        })
    df = pd.DataFrame(rows).sort_values("views_relevant", ascending=False)
    df.to_csv(out / "channels_features.csv", index=False)
    print(f"{out.name}: posts={len(posts)} relevant={sum(p['relevant'] for p in posts)} channels={len(df)} -> {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
