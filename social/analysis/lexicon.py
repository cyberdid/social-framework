"""Case lexicon: the measurement definitions of a case (relevance, frames, claims, topics, versions, actors, periods).

The analyst agent writes analysis/lexicon.yaml for each case — ideally before looking at the matched posts and
committed with `precommit` — and every analysis module reads it. Regexes are case-insensitive; a match is a
mention, not an endorsement.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

TEMPLATE = """\
# Лексикон кейсу: визначення вимірювань. Заповніть ДО того, як дивитися на збіги (порада: зафіксуйте precommit).
# Усі регулярні вирази без урахування регістру; збіг = згадка, а не згода (допис може спростовувати твердження).
timezone: Europe/Kyiv

relevance:                       # допис релевантний, якщо є mention, (якщо задано) context, і немає exclude
  mention: "ключов\\\\w*|keyword"    # обов'язкова згадка теми (усі словоформи, укр./рос./англ.)
  context: ""                    # необов'язково: хоча б одна ознака поруч (інституції, дії)
  exclude: ""                    # необов'язково: фрази-омоніми, які не стосуються кейсу

groups_file: analysis/channel_groups.csv   # channel,group[,basis] — класифікація каналів (порожні = U)
group_names:
  D: офіційні й редакційні
  B: українські неофіційні
  C: російські / проросійські
  A: місцеві
  E: іноземні
  U: не визначено

periods:                         # хвилі або періоди: [from, to) у локальних датах
  - {name: "хвиля 1", from: "2026-01-01", to: "2026-02-01"}

frames:                          # рамки подачі (кожен допис може мати кілька)
  R1: {label: "приклад рамки", regex: "приклад"}

claims:                          # спірні твердження для фактчеків; official_at — час першого офіційного документа
  K1: {label: "приклад твердження", regex: "приклад", official_at: null}

topics:                          # теми для настроїв і реакцій
  T1: {label: "приклад теми", regex: "приклад"}

versions:                        # пояснювальні версії «що насправді стоїть за подією»
  V1: {label: "приклад версії", regex: "приклад"}

actors:                          # публічні особи й інституції в службовій ролі (для зв'язків)
  Інституція: "інституц\\\\w*"

reciprocity:                     # необов'язково: дія → відповідь між органами за офіційними каналами
  bodies: {}                     # канал → орган
  targets: {}                    # орган → регулярний вираз «працівник цього органу»
  action: "обшук\\\\w*|підозр\\\\w*|затрима\\\\w*"
  window_days: 14

media_origins: {}                # домен → походження (UA, RU_BY, FOREIGN, ...); решта = ?
emotions: {}                     # необов'язково: емодзі → емоція; порожньо = словник за замовчуванням
"""

DEFAULT_EMOTIONS = {"гнів": "🤬 😡 🖕 🤮", "глузування": "😁 🤣 🤡", "схвалення": "❤ ❤️ 👍 🔥 💯 👏 🙏 👌 🥰 😍",
                    "подив": "🤯 😱 🤨 🙈 👀 🤔 😮", "сум": "😢 😭 💔", "несхвалення": "👎 💩"}


class Lexicon:
    def __init__(self, data: dict, case: Path):
        self.raw = data or {}
        self.case = case
        self.tz = self.raw.get("timezone") or "Europe/Kyiv"
        rel = self.raw.get("relevance") or {}
        self.mention = _rx(rel.get("mention"))
        self.context = _rx(rel.get("context"))
        self.exclude = _rx(rel.get("exclude"))
        self.periods = self.raw.get("periods") or []
        self.group_names = self.raw.get("group_names") or {}
        self.frames = _defs(self.raw.get("frames"))
        self.claims = _defs(self.raw.get("claims"))
        self.topics = _defs(self.raw.get("topics"))
        self.versions = _defs(self.raw.get("versions"))
        self.actors = {k: _rx(v) for k, v in (self.raw.get("actors") or {}).items() if v}
        self.media_origins = {k.lower().removeprefix("www."): v for k, v in (self.raw.get("media_origins") or {}).items()}
        emo = self.raw.get("emotions") or {}
        if emo and all(isinstance(v, str) and len(v) <= 4 for v in emo.values()) and not any(" " in v for v in emo.values()):
            self.emoji = dict(emo)  # emoji -> emotion
        else:
            src = emo or DEFAULT_EMOTIONS
            self.emoji = {e: k for k, v in src.items() for e in v.split()}
        self.groups = self._groups()

    def _groups(self) -> dict[str, str]:
        p = self.case / (self.raw.get("groups_file") or "analysis/channel_groups.csv")
        out = {}
        if p.exists():
            import csv
            for r in csv.DictReader(l for l in p.open(encoding="utf-8") if not l.startswith("#")):
                ch = (r.get("channel") or "").strip().lower().lstrip("@")
                if ch:
                    out[ch] = (r.get("group") or "U").strip() or "U"
        return out

    def relevance(self, text: str) -> str:
        t = text or ""
        if not self.mention or not self.mention.search(t):
            return "no_mention"
        if self.exclude:  # e.g. an idiom: remove it, then the mention must still be there
            t = self.exclude.sub(" ", t)
            if not self.mention.search(t):
                return "excluded"
        if self.context and not self.context.search(t):
            return "no_context"
        return "relevant"

    def period(self, day: str) -> str:
        for p in self.periods:
            if str(p.get("from", "")) <= day < str(p.get("to", "9999")):
                return p.get("name", "")
        return ""

    @staticmethod
    def match(defs: dict, text: str) -> list[str]:
        return [k for k, d in defs.items() if d["rx"].search(text or "")]


def _rx(s):
    return re.compile(s, re.I | re.S) if s else None


def _defs(d) -> dict:
    out = {}
    for k, v in (d or {}).items():
        if isinstance(v, str):
            v = {"label": k, "regex": v}
        if v.get("regex"):
            out[k] = {**v, "label": v.get("label") or k, "rx": _rx(v["regex"])}
    return out


def path(case: Path) -> Path:
    return case / "analysis/lexicon.yaml"


def load(case: Path) -> Lexicon:
    p = path(case)
    if not p.exists():
        raise FileNotFoundError("немає analysis/lexicon.yaml — створіть його інструментом lexicon_template і заповніть")
    return Lexicon(yaml.safe_load(p.read_text(encoding="utf-8")), case)


def write_template(case: Path) -> Path:
    p = path(case)
    if p.exists():
        raise FileExistsError("analysis/lexicon.yaml вже існує")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(TEMPLATE, encoding="utf-8")
    g = case / "analysis/channel_groups.csv"
    if not g.exists():
        g.write_text("# channel,group,basis — група за критеріями з lexicon.yaml (group_names); невідомі = U\nchannel,group,basis\n",
                     encoding="utf-8")
    return p
