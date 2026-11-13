"""Automatic measurement of scenario indicators from news headlines (collect_news items).

An indicator in the scenario model may carry a "measure" block; forecast_watch evaluates it on calendar weeks
(Monday–Sunday) after the model's fixed_at date that the collected news fully covers, and journals the outcome
(level A, source = this output) once it is met, or as not_occurred when its window has closed.

  {"type": "attention_below", "baseline": ["2026-11-04", "2026-11-10"], "ratio": 0.10, "weeks": 4, "regex": "..."?}
      N consecutive weeks each with fewer headlines than ratio x the baseline's weekly rate.
  {"type": "share_increase", "regex": "помст\\w*|revenge", "baseline": ["2026-11-04", "2026-11-11"],
   "factor": 2.0, "min_count": 10, "within": "..."?}
      a week whose share of headlines matching regex is >= factor x the baseline share, with >= min_count matches.
  Optional "within" (regex) restricts headlines to the topic before counting.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..case import read_jsonl
from . import core


def _items(case: Path, tz: ZoneInfo) -> list[dict]:
    seen, out = set(), []
    for f in sorted((case / "data/raw").glob("*/items.jsonl")):
        for r in read_jsonl(f):
            key = r.get("gnews_link") or (r.get("title"), r.get("source_name"))
            if key in seen or not r.get("published"):
                continue
            seen.add(key)
            day = datetime.fromisoformat(r["published"].replace("Z", "+00:00")).astimezone(tz).date()
            out.append({"day": day, "title": r.get("title") or ""})
    return out


def _covered_until(case: Path) -> date | None:
    ends = []
    for f in (case / "data/raw").glob("*/queries_done.jsonl"):
        for r in read_jsonl(f):
            if ":" in r.get("window", ""):
                ends.append(date.fromisoformat(r["window"].split(":")[1]))
    return max(ends) if ends else None


def watch(case: Path, record: bool = True, tz: str = "Europe/Kyiv") -> dict:
    model_p = core._model_path(case)
    model = json.loads(model_p.read_text(encoding="utf-8"))
    fixed = date.fromisoformat(model["fixed_at"])
    items = _items(case, ZoneInfo(tz))
    until = _covered_until(case)
    journal = read_jsonl(core._journal(case))
    done = {e["id"] for e in journal if e["type"] == "observe"}
    first_monday = fixed + timedelta(days=(7 - fixed.weekday()) % 7 or 7)
    weeks = []
    wk = first_monday
    while until and wk + timedelta(days=7) <= until:
        weeks.append(wk)
        wk += timedelta(days=7)
    results = {}
    for ind in model.get("indicators", []):
        m = ind.get("measure")
        if not m:
            continue
        within = re.compile(m["within"], re.I) if m.get("within") else None
        pool = [i for i in items if not within or within.search(i["title"])]
        b0, b1 = (date.fromisoformat(x) for x in m["baseline"])
        base = [i for i in pool if b0 <= i["day"] <= b1]
        wend = date.fromisoformat(ind["window_end"])
        inwin = [w for w in weeks if w + timedelta(days=6) <= wend]
        met, detail = None, []
        if m["type"] == "attention_below":
            rate = len(base) * 7 / ((b1 - b0).days + 1)
            run = 0
            for w in inwin:
                n = sum(w <= i["day"] < w + timedelta(days=7) for i in pool)
                run = run + 1 if n < m.get("ratio", 0.1) * rate else 0
                detail.append({"week": w.isoformat(), "headlines": n, "ratio_to_baseline": round(n / rate, 3) if rate else None})
                if run >= m.get("weeks", 4) and not met:
                    met = w
        elif m["type"] == "share_increase":
            rx = re.compile(m["regex"], re.I)
            bshare = sum(bool(rx.search(i["title"])) for i in base) / max(len(base), 1)
            for w in inwin:
                wk_items = [i for i in pool if w <= i["day"] < w + timedelta(days=7)]
                k = sum(bool(rx.search(i["title"])) for i in wk_items)
                share = k / len(wk_items) if wk_items else 0.0
                detail.append({"week": w.isoformat(), "matches": k, "headlines": len(wk_items), "share": round(share, 4)})
                if not met and k >= m.get("min_count", 10) and share >= m.get("factor", 2.0) * bshare:
                    met = w
        else:
            detail.append({"error": f"unknown measure type {m['type']}"})
        status = "met" if met else ("window_closed" if until and until > wend else "pending")
        results[ind["id"]] = {"status": status, "weeks": detail, "baseline_headlines": len(base)}
        if record and ind["id"] not in done:
            src = "data/processed/forecast/watch.json"
            if met:
                core.forecast_observe(case, ind["id"], True, (met + timedelta(days=6)).isoformat(), src, "A",
                                      f"виміряно автоматично ({m['type']}), тиждень з {met.isoformat()}")
            elif status == "window_closed":
                core.forecast_observe(case, ind["id"], False, ind["window_end"], src, "A",
                                      f"вікно закрилося без події (виміряно автоматично, {m['type']})")
    res = {"covered_until_exclusive": until.isoformat() if until else None, "complete_weeks": len(weeks),
           "headlines": len(items), "indicators": results}
    d = case / "data/processed/forecast"
    d.mkdir(parents=True, exist_ok=True)
    (d / "watch.json").write_text(json.dumps(res, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return res
