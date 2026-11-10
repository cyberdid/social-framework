"""Recall audit of the relevance rule: lemmatize (pymorphy3 uk+ru, approach reused from archive-index lexical.py)
and fuzzy-match tokens to the place name; report posts in the window that mention the place but were not marked
relevant, for manual review."""
import json, re, sys
from functools import lru_cache
from pathlib import Path
import pymorphy3
from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parents[2]
snap = sys.argv[1]
UK, RU = pymorphy3.MorphAnalyzer(lang="uk"), pymorphy3.MorphAnalyzer(lang="ru")
TARGETS = ["борщагівка", "борщаговка", "петропавлівська", "петропавловская"]

@lru_cache(maxsize=200_000)
def norm(tok):
    forms = {tok}
    for an in (UK, RU):
        forms |= {p.normal_form for p in an.parse(tok)[:3]}
    return frozenset(forms)

def hits(text):
    out = set()
    for tok in re.findall(r"[а-яіїєґё'’a-z]{6,}", text.lower()):
        for f in norm(tok):
            for t in TARGETS:
                if fuzz.ratio(f, t) >= 85:
                    out.add(t)
    return out

posts = [json.loads(l) for l in (ROOT / "data/processed" / snap / "posts_clean.jsonl").open()]
win = [p for p in posts if p["t_kyiv"] >= "2026-11-02T11:00"]
miss = []
for p in win:
    h = hits(p["text_clean"])
    if "борщагівка" in h or "борщаговка" in h:
        if not p["relevant"]:
            miss.append((p["t_kyiv"][5:16], p["channel"], p["views"], sorted(h), p["text_clean"][:160].replace("\n", " ")))
rel = sum(p["relevant"] for p in win)
print(f"window posts={len(win)} relevant={rel} lemma/fuzzy place hits not relevant={len(miss)}")
for m in miss: print(*m, sep=" | ")
