"""Tests for the general analysis layer (social/analysis, social/figures, forecast_watch) on synthetic data."""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from social.analysis import coordination, corpus, measures, sources
from social.analysis import lexicon as L
from social.case import create_case
from social.tools import core

TEXT = ("Операція Рубікон: детективи бюро провели обшуки у двох відомствах, повідомили про підозру трьом посадовцям "
        "і заявили, що слідство триває. Деталі обіцяють згодом.")


def _post(ch, mid, t, text, views=100, reactions=None):
    return {"channel": ch, "msg_id": mid, "date": t, "text": text, "views": views, "url": f"https://t.me/{ch}/{mid}",
            "reactions": reactions or []}


@pytest.fixture()
def case(tmp_path):
    c = create_case(tmp_path / "c", "Синтетичний кейс")
    L.write_template(c)
    lex = yaml.safe_load((c / "analysis/lexicon.yaml").read_text(encoding="utf-8"))
    lex["relevance"] = {"mention": r"рубікон\w*", "exclude": r"перейти рубікон", "context": r"бюро|детектив\w*|обшук\w*"}
    lex["periods"] = [{"name": "W1", "from": "2026-01-01", "to": "2026-01-10"}, {"name": "W2", "from": "2026-01-10", "to": "2026-02-01"}]
    lex["frames"] = {"R1": {"label": "помста", "regex": r"помст\w*"}}
    lex["claims"] = {"K1": {"label": "обшуки", "regex": r"обшук\w*", "official_at": "2026-01-05T13:00"}}
    lex["topics"] = {"T1": {"label": "обшуки", "regex": r"обшук\w*"}}
    lex["versions"] = {"V1": {"label": "помста", "regex": r"помст\w*"}}
    lex["actors"] = {"Бюро": r"бюро", "Відомство": r"відомств\w*"}
    (c / "analysis/lexicon.yaml").write_text(yaml.safe_dump(lex, allow_unicode=True), encoding="utf-8")
    (c / "analysis/channel_groups.csv").write_text("channel,group\nofficial,D\nru1,C\nru2,C\n", encoding="utf-8")
    d = c / "data/raw/telegram_search/s1"
    d.mkdir(parents=True)
    posts = [
        _post("official", 1, "2026-01-05T10:00:00+00:00", TEXT, 1000, [["👍", 40], ["🤬", 10]]),
        _post("ru1", 5, "2026-01-05T10:04:00+00:00", TEXT + " Підписуйтесь!", 500, [["🤣", 30]]),
        _post("ru2", 9, "2026-01-05T10:06:00+00:00", TEXT, 300),
        _post("ru2", 10, "2026-01-12T09:00:00+00:00", "Рубікон — це помста бюро відомству, кажуть джерела.", 50),
        _post("misc", 3, "2026-01-06T09:00:00+00:00", "Час перейти Рубікон у футболі.", 10),
        _post("misc", 4, "2026-01-06T10:00:00+00:00", "Рубікон без контексту", 10),
        _post("misc", 5, "2026-01-06T11:00:00+00:00", "Про інше взагалі", 10),
    ]
    (d / "posts.jsonl").write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in posts), encoding="utf-8")
    return c


def test_lexicon_template_and_relevance(case):
    lex = L.load(case)
    assert lex.relevance("Рубікон: обшуки бюро") == "relevant"
    assert lex.relevance("перейти Рубікон") == "excluded"
    assert lex.relevance("Рубікон без нічого") == "no_context"
    assert lex.relevance("щось інше") == "no_mention"
    with pytest.raises(FileExistsError):
        L.write_template(case)


def test_corpus_labels(case):
    s = corpus.build(case)
    assert s["posts"] == 7 and s["relevant"] == 4
    assert s["relevance"] == {"relevant": 4, "excluded": 1, "no_context": 1, "no_mention": 1}
    posts = {p["url"]: p for p in corpus.load_posts(case)}
    assert posts["https://t.me/official/1"]["group"] == "D" and posts["https://t.me/official/1"]["period"] == "W1"
    assert posts["https://t.me/ru2/10"]["frames"] == ["R1"] and posts["https://t.me/ru2/10"]["period"] == "W2"


def test_measures(case):
    corpus.build(case)
    fr = measures.frames(case)
    assert fr["overall"]["posts"] == 4 and fr["overall"]["R1_n"] == 1
    cl = measures.claims(case)["K1"]
    assert cl["posts"] == 3 and cl["first"]["channel"] == "official" and cl["posts_before_official"] == 3
    md = measures.mood(case)
    assert md["group"]["D"]["схвалення"] == 0.8 and md["group"]["C"]["глузування"] == 1.0
    v = measures.versions(case)
    assert v["V1"]["mentions"] >= 1
    assert measures.volume(case)["days"] == 2
    rel = sources.relations(case)
    assert rel["pairs"] >= 1 and rel["top"][0]["a"] in ("Бюро", "Відомство")


def test_coordination_finds_synchronous_copies(case):
    corpus.build(case)
    res = coordination.analyze(case, permutations=49)
    assert res["edges"] == 3 and res["sync_edges"] == 3 and res["channels"] == 3
    assert res["largest_components"][0]["size"] == 3


def test_figures(case):
    pytest.importorskip("matplotlib")
    pytest.importorskip("networkx")
    from social import figures
    corpus.build(case)
    measures.volume(case)
    measures.frames(case)
    measures.mood(case)
    measures.claims(case)
    coordination.analyze(case, permutations=9)
    core.fact_check(case, "x", "фейк", "A", "y", [])
    core.judgment(case, "z", 0.7, "C", "r")
    out = figures.make(case)
    for name in ["volume", "frames", "mood", "verdicts", "judgments", "copy_network", "claims_timeline"]:
        assert out[name].endswith(".png"), (name, out[name])
        assert (case / out[name]).exists()
    assert out["scenarios"].startswith("пропущено")


def test_forecast_watch_measures(case):
    from social.tools import forecast_watch as FW
    fixed = date(2026, 1, 1)
    model = {"fixed_at": fixed.isoformat(), "scenarios": [{"id": "A", "name": "a", "p": 0.5}, {"id": "B", "name": "b", "p": 0.5}],
             "indicators": [
                 {"id": "I1", "name": "згасання", "window_end": "2026-03-01", "p_given": {"A": 0.8, "B": 0.3},
                  "measure": {"type": "attention_below", "baseline": ["2025-12-25", "2025-12-31"], "ratio": 0.1, "weeks": 2}},
                 {"id": "I2", "name": "рамка", "window_end": "2026-03-01", "p_given": {"A": 0.2, "B": 0.7},
                  "measure": {"type": "share_increase", "regex": "помст", "baseline": ["2025-12-25", "2025-12-31"],
                              "factor": 2, "min_count": 3}}],
             "forecasts": []}
    core.forecast_define(case, model)
    d = case / "data/raw/gnews"
    d.mkdir(parents=True)
    items, done = [], []
    for i in range(70):  # baseline week: 10 headlines a day, 1 in 10 about revenge
        day = date(2025, 12, 25) + timedelta(days=i // 10)
        items.append({"title": "помста" if i % 10 == 0 else "новина", "published": f"{day}T10:00:00+00:00", "gnews_link": f"b{i}"})
    for k in range(5):  # week of 5 Jan: 5 headlines, 4 about revenge; then quiet weeks
        items.append({"title": "помста знову" if k < 4 else "інше", "published": f"2026-01-0{5 + k}T10:00:00+00:00", "gnews_link": f"w{k}"})
    d.joinpath("items.jsonl").write_text("".join(json.dumps(x) + "\n" for x in items))
    d.joinpath("queries_done.jsonl").write_text(json.dumps({"window": "2026-01-25:2026-01-26"}) + "\n")
    res = FW.watch(case)
    assert res["indicators"]["I2"]["status"] == "met" and res["indicators"]["I1"]["status"] == "met"
    obs = {e["id"]: e for e in core.list_records(case, "forecast_journal")}
    assert obs["I2"]["status"] == "occurred" and obs["I2"]["level"] == "A"
    again = FW.watch(case)  # idempotent: no duplicate observations
    assert again["indicators"]["I1"]["status"] == "met" and len(core.list_records(case, "forecast_journal")) == 2
