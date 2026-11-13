"""Case tools as plain functions (the MCP server wraps them; tests call them directly)."""
from __future__ import annotations

import gzip
import hashlib
import html as htmllib
import json
import re
import shutil
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
import yaml

from .. import jobs
from ..case import append_jsonl, commit, log, now_utc, read_jsonl, slugify
from . import forecast as FE

VERDICTS = ["правда", "ймовірно правда", "частково", "перекручено", "не підтверджено", "припущення", "неправда", "фейк"]
LEVELS = ["A", "B", "C", "D"]
NEWS_LANG = {"uk": ("uk", "UA", "UA:uk"), "ru": ("ru", "RU", "RU:ru"), "en": ("en-US", "US", "US:en"),
             "pl": ("pl", "PL", "PL:pl"), "de": ("de", "DE", "DE:de")}
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) social-research-archive"}


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ------------------------------------------------------------------ state and log
def case_status(case: Path) -> dict:
    cy = yaml.safe_load((case / "case.yaml").read_text(encoding="utf-8")) or {}
    raw = sorted(str(p.relative_to(case)) for p in (case / "data/raw").glob("*/*") if p.is_dir())
    fc, jd = read_jsonl(case / "wiki/factchecks.jsonl"), read_jsonl(case / "wiki/judgments.jsonl")
    pre = sorted(p.name for p in (case / "wiki/precommit").glob("*.md"))
    models = sorted((case / "wiki/forecasts").glob("model-*.json"))
    log_tail = (case / "log.md").read_text(encoding="utf-8").splitlines()[-12:] if (case / "log.md").exists() else []
    js = [{k: j[k] for k in ("id", "state", "exit_code")} for j in jobs.status(case, tail=0)]
    hints = []
    if not cy.get("question"):
        hints.append("Питання кейсу не записано в case.yaml.")
    if not pre:
        hints.append("Попередньої фіксації ще немає (рекомендація: precommit до аналізу).")
    if raw and not fc:
        hints.append("Дані зібрано, але фактчеків ще немає.")
    if fc and not jd:
        hints.append("Є фактчеки, але немає власних висновків з ймовірностями (judgment).")
    if jd and not models:
        hints.append("Є висновки, але немає сценаріїв і прогнозів (forecast_define).")
    return {"case": str(case), "case_yaml": cy, "raw_snapshots": raw, "precommits": pre,
            "factchecks": len(fc), "judgments": len(jd), "forecast_models": [m.name for m in models],
            "jobs": js, "log_tail": log_tail, "recommendations": hints}


def log_entry(case: Path, text: str) -> str:
    log(case, text)
    return "записано в log.md"


def precommit(case: Path, title: str, markdown: str) -> dict:
    p = case / "wiki/precommit" / f"{date.today().isoformat()}-{slugify(title)}.md"
    if p.exists():
        raise FileExistsError(f"{p.name} вже існує; попередня фіксація не переписується — створи нову з іншою назвою")
    p.write_text(f"# {title}\n\n**Зафіксовано:** {now_utc()}\n\n{markdown.strip()}\n", encoding="utf-8")
    h = commit(case, [str(p.relative_to(case))], f"precommit: {title}")
    log(case, f"попередня фіксація «{title}» ({p.name}, коміт {h})")
    return {"file": str(p.relative_to(case)), "commit": h}


# ------------------------------------------------------------------ collection (background jobs)
def collect_telegram(case: Path, channels: list[str], relevance_regex: str, start: str, end: str | None = None,
                     depth: int = 1, max_channels: int = 150, max_pages: int = 150) -> dict:
    re.compile(relevance_regex)
    inp = case / "data/inputs"
    inp.mkdir(parents=True, exist_ok=True)
    seeds = inp / f"tg_seeds_{_stamp()}.txt"
    seeds.write_text("\n".join(c.strip().lstrip("@").removeprefix("https://t.me/").split("/")[0] for c in channels) + "\n")
    argv = [sys.executable, "-m", "social.collectors.telegram_web", "--seeds", str(seeds), "--start", start,
            "--relevance", relevance_regex, "--depth", str(depth), "--max-channels", str(max_channels),
            "--max-pages", str(max_pages), "--out-root", str(case / "data/raw/telegram_web")]
    if end:
        argv += ["--end", end]
    return jobs.start(case, "telegram", argv)


def collect_youtube(case: Path, queries: list[str], since: str, per_query: int = 40, comment_videos: int = 20,
                    max_comments: int = 1000) -> dict:
    inp = case / "data/inputs"
    inp.mkdir(parents=True, exist_ok=True)
    qf = inp / f"yt_queries_{_stamp()}.txt"
    qf.write_text("\n".join(queries) + "\n", encoding="utf-8")
    argv = [sys.executable, "-m", "social.collectors.youtube", "--queries", str(qf), "--since", since.replace("-", ""),
            "--per-query", str(per_query), "--comment-videos", str(comment_videos), "--max-comments", str(max_comments),
            "--out-root", str(case / "data/raw/youtube")]
    return jobs.start(case, "youtube", argv)


def news_queries_tsv(queries: list[str], languages: list[str]) -> str:
    rows = ["# query\thl\tgl\tceid\twindowed"]
    for q in queries:
        for lang in languages:
            hl, gl, ceid = NEWS_LANG.get(lang, (lang, lang.upper(), f"{lang.upper()}:{lang}"))
            rows.append(f"{q}\t{hl}\t{gl}\t{ceid}\tyes")
    return "\n".join(rows) + "\n"


def collect_news(case: Path, queries: list[str], start: str, end: str, languages: list[str] | None = None,
                 window_days: int = 7, out_name: str = "gnews") -> dict:
    inp = case / "data/inputs"
    inp.mkdir(parents=True, exist_ok=True)
    qf = inp / f"news_queries_{_stamp()}.tsv"
    qf.write_text(news_queries_tsv(queries, languages or ["uk", "ru", "en"]), encoding="utf-8")
    argv = [sys.executable, "-m", "social.collectors.news", "--queries", str(qf), "--start", start, "--end", end,
            "--window-days", str(window_days), "--out", str(case / "data/raw" / out_name)]
    return jobs.start(case, "news", argv)


def _html_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</h\d>|</li>", "\n", raw)
    txt = htmllib.unescape(re.sub(r"<[^>]+>", " ", raw))
    return "\n".join(l.strip() for l in re.sub(r"[ \t]+", " ", txt).splitlines() if l.strip())


def archive_url(case: Path, url: str, timeout: int = 40) -> dict:
    """Fetch one public page now: raw gzip + sha256 in data/raw/web, plain text in data/processed/web_text."""
    r = requests.get(url, headers=UA, timeout=timeout)
    body = r.content
    sha = hashlib.sha256(body).hexdigest()
    raw_dir, txt_dir = case / "data/raw/web", case / "data/processed/web_text"
    raw_dir.mkdir(parents=True, exist_ok=True)
    txt_dir.mkdir(parents=True, exist_ok=True)
    rawp = raw_dir / f"{sha[:16]}.html.gz"
    if not rawp.exists():
        rawp.write_bytes(gzip.compress(body))
    text = _html_text(r.text) if "html" in r.headers.get("content-type", "html") else ""
    if text:
        (txt_dir / f"{sha[:16]}.txt").write_text(text, encoding="utf-8")
    rec = append_jsonl(raw_dir / "index.jsonl", {"url": url, "final_url": r.url, "status": r.status_code, "sha256": sha,
                                                 "bytes": len(body), "fetched_at": now_utc(), "file": rawp.name})
    log(case, f"архів сторінки {url} → {rawp.name} (HTTP {r.status_code})")
    return {**rec, "text_preview": text[:1500]}


# ------------------------------------------------------------------ fact-checks and judgments
def fact_check(case: Path, claim: str, verdict: str, level: str, explanation: str, sources: list[str],
               kernel: str = "", first_seen: str = "", spread: str = "") -> dict:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    if level not in LEVELS:
        raise ValueError("level must be A, B, C or D")
    n = len(read_jsonl(case / "wiki/factchecks.jsonl")) + 1
    rec = append_jsonl(case / "wiki/factchecks.jsonl", {"id": f"F{n}", "claim": claim, "verdict": verdict, "level": level,
                                                        "kernel": kernel, "explanation": explanation,
                                                        "first_seen": first_seen, "spread": spread, "sources": sources})
    log(case, f"фактчек F{n}: «{claim[:80]}» — {verdict} ({level})")
    return rec


def judgment(case: Path, statement: str, probability: float, level: str, reasoning: str,
             alternatives: str = "", would_change_my_mind: str = "") -> dict:
    if not 0 <= probability <= 1:
        raise ValueError("probability is 0..1")
    if level not in LEVELS:
        raise ValueError("level must be A, B, C or D")
    n = len(read_jsonl(case / "wiki/judgments.jsonl")) + 1
    rec = append_jsonl(case / "wiki/judgments.jsonl", {"id": f"J{n}", "statement": statement, "probability": probability,
                                                       "level": level, "reasoning": reasoning,
                                                       "alternatives": alternatives,
                                                       "would_change_my_mind": would_change_my_mind})
    log(case, f"висновок J{n}: «{statement[:80]}» — {round(100 * probability)} % ({level})")
    return rec


def list_records(case: Path, kind: str) -> list[dict]:
    files = {"factchecks": "wiki/factchecks.jsonl", "judgments": "wiki/judgments.jsonl",
             "forecast_journal": "wiki/forecasts/journal.jsonl", "archive": "data/raw/web/index.jsonl"}
    if kind not in files:
        raise ValueError(f"kind is one of {list(files)}")
    return read_jsonl(case / files[kind])


# ------------------------------------------------------------------ scenarios and forecasts
def _model_path(case: Path) -> Path:
    ms = sorted((case / "wiki/forecasts").glob("model-*.json"))
    if not ms:
        raise FileNotFoundError("Сценарної моделі ще немає: спершу forecast_define")
    return ms[-1]


def forecast_define(case: Path, model: dict) -> dict:
    """Freeze a scenario model (scenarios with p summing to 1, indicators with p_given per scenario, forecasts)."""
    sc = model.get("scenarios") or []
    if len(sc) < 2 or abs(sum(s["p"] for s in sc) - 1) > 1e-6:
        raise ValueError("потрібно щонайменше 2 сценарії з p, що разом дають 1")
    need = {"scenarios": ("id", "name", "p"), "indicators": ("id", "name", "window_end", "p_given"),
            "forecasts": ("id", "text", "p", "resolve_by")}
    for part, keys in need.items():
        for x in model.get(part, []):
            miss = [k for k in keys if k not in x]
            if miss:
                raise ValueError(f"{part}: у {x.get('id', '?')} бракує {miss}")
    ids = {s["id"] for s in sc}
    for i in model.get("indicators", []):
        if set(i["p_given"]) != ids:
            raise ValueError(f"індикатор {i['id']}: p_given має бути для кожного сценарію {sorted(ids)}")
        i.setdefault("group", i["id"])
    for f in model.get("forecasts", []):
        f.setdefault("history", [{"date": date.today().isoformat(), "p": f["p"], "note": "початкова оцінка"}])
    model.setdefault("fixed_at", date.today().isoformat())
    n = len(list((case / "wiki/forecasts").glob("model-*.json"))) + 1
    p = case / "wiki/forecasts" / f"model-{n:02d}-{model['fixed_at']}.json"
    p.write_text(json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8")
    h = commit(case, [str(p.relative_to(case))], f"precommit: scenario model {n}")
    log(case, f"сценарна модель {p.name} зафіксована (коміт {h})")
    return {"file": str(p.relative_to(case)), "commit": h}


def _journal(case: Path) -> Path:
    return case / "wiki/forecasts/journal.jsonl"


def forecast_observe(case: Path, indicator: str, occurred: bool, date_: str, source: str, level: str, note: str = "") -> dict:
    rec = append_jsonl(_journal(case), {"type": "observe", "id": indicator, "status": "occurred" if occurred else "not_occurred",
                                        "date": date_, "source": source, "level": level, "note": note})
    log(case, f"прогноз: {indicator} {'сталося' if occurred else 'не сталося'} {date_}")
    return rec


def forecast_revise(case: Path, target: str, p, date_: str, reason: str) -> dict:
    if target == "scenarios" and abs(sum(float(v) for v in p.values()) - 1) > 1e-6:
        raise ValueError("ймовірності сценаріїв мають давати 1")
    rec = append_jsonl(_journal(case), {"type": "revise", "target": target, "p": p, "date": date_, "reason": reason})
    log(case, f"прогноз: перегляд {target} — {reason[:80]}")
    return rec


def forecast_resolve(case: Path, target: str, outcome: str, date_: str, source: str, level: str, note: str = "") -> dict:
    rec = append_jsonl(_journal(case), {"type": "resolve", "id": target, "outcome": outcome, "date": date_,
                                        "source": source, "level": level, "note": note})
    log(case, f"прогноз: перевірено {target} → {outcome}")
    return rec


def forecast_state(case: Path, today: str | None = None) -> dict:
    model, journal = FE.load(_model_path(case), _journal(case))
    st = FE.compute(model, journal, date.fromisoformat(today) if today else date.today())
    FE.write_state(st, case / "data/processed/forecast")
    return st


# ------------------------------------------------------------------ report
def report_build(case: Path, source: str = "report/report.md") -> dict:
    src = (case / source).resolve()
    if case.resolve() not in src.parents:
        raise ValueError("джерело звіту має бути всередині кейсу")
    out = {}
    if src.suffix == ".tex":
        if not shutil.which("tectonic"):
            raise RuntimeError("для .tex потрібен tectonic (https://tectonic-typesetting.github.io)")
        r = subprocess.run(["tectonic", "-X", "compile", "-r", "2", src.name], cwd=src.parent, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(r.stderr[-3000:])
        out["pdf"] = str(src.with_suffix(".pdf").relative_to(case))
    else:
        import markdown
        body = markdown.markdown(src.read_text(encoding="utf-8"), extensions=["tables", "toc", "fenced_code"])
        page = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
                "<style>body{font:17px/1.6 system-ui,sans-serif;max-width:860px;margin:2em auto;padding:0 16px;color:#1f2933}"
                "img{max-width:100%}table{border-collapse:collapse}td,th{border:1px solid #ccd;padding:4px 8px}"
                "h1,h2{color:#1F3D55}</style>" + body)
        h = src.with_suffix(".html")
        h.write_text(page, encoding="utf-8")
        out["html"] = str(h.relative_to(case))
        if shutil.which("pandoc") and (shutil.which("tectonic") or shutil.which("xelatex")):
            eng = "tectonic" if shutil.which("tectonic") else "xelatex"
            r = subprocess.run(["pandoc", src.name, "-o", src.with_suffix(".pdf").name, f"--pdf-engine={eng}",
                                "-V", "mainfont=DejaVu Serif", "--resource-path", "."], cwd=src.parent,
                               capture_output=True, text=True)
            if r.returncode == 0:
                out["pdf"] = str(src.with_suffix(".pdf").relative_to(case))
            else:
                out["pdf_error"] = r.stderr[-1500:]
    log(case, f"звіт зібрано з {source}: {', '.join(v for k, v in out.items() if k in ('pdf', 'html'))}")
    return out


# ------------------------------------------------------------------ more public sources (background jobs)
def _inp(case: Path, name: str, lines: list[str], ext: str = "txt") -> Path:
    inp = case / "data/inputs"
    inp.mkdir(parents=True, exist_ok=True)
    p = inp / f"{name}_{_stamp()}.{ext}"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _py(module: str, *args: str) -> list[str]:
    return [sys.executable, "-m", f"social.collectors.{module}", *args]


def collect_telegram_search(case: Path, channels: list[str], terms: list[str], start: str, end: str | None = None,
                            relevance_regex: str | None = None, snowball_max: int = 200) -> dict:
    ch = _inp(case, "tgsearch_channels", [c.strip().lstrip("@").removeprefix("https://t.me/").split("/")[0] for c in channels])
    argv = _py("telegram_search", "--channels", str(ch), "--terms", *terms, "--start", start,
               "--out", str(case / "data/raw/telegram_search" / _stamp()), "--snowball-max", str(snowball_max))
    if end:
        argv += ["--end", end]
    if relevance_regex:
        re.compile(relevance_regex)
        argv += ["--relevance", relevance_regex]
    return jobs.start(case, "tgsearch", argv)


def collect_gdelt(case: Path, queries: list[str], start: str, end: str) -> dict:
    return jobs.start(case, "gdelt", _py("gdelt", "--queries", *queries, "--start", start, "--end", end,
                                         "--out", str(case / "data/raw/gdelt")))


def collect_reddit(case: Path, terms: list[str], after: str, before: str, subreddits: list[str] | None = None) -> dict:
    argv = _py("reddit", "--terms", *terms, "--after", after, "--before", before, "--out", str(case / "data/raw/reddit"))
    if subreddits:
        argv += ["--subreddits", *subreddits]
    return jobs.start(case, "reddit", argv)


def resolve_news_articles(case: Path, since: str = "", domains: list[str] | None = None, decode_only: bool = False,
                          news_dirs: list[str] | None = None) -> dict:
    dirs = news_dirs or [p.parent.name for p in (case / "data/raw").glob("*/items.jsonl")]
    files = [str(case / "data/raw" / d / "items.jsonl") for d in dirs if (case / "data/raw" / d / "items.jsonl").exists()]
    if not files:
        raise FileNotFoundError("немає data/raw/*/items.jsonl — спершу collect_news")
    argv = _py("news_resolve", "--gnews", *files, "--out", str(case / "data/raw/news_articles"), "--since", since)
    if domains:
        argv += ["--domains", *domains]
    if decode_only:
        argv.append("--decode-only")
    return jobs.start(case, "articles", argv)


def fetch_commoncrawl(case: Path, urls: list[str], crawls: list[str]) -> dict:
    src = _inp(case, "cc_urls", [json.dumps({"url": u}) for u in urls], "jsonl")
    return jobs.start(case, "commoncrawl", _py("commoncrawl", "--urls", str(src), "--crawls", *crawls,
                                               "--out", str(case / "data/raw/commoncrawl")))


def archive_site_listing(case: Path, name: str, listing_url: str, item_regex: str, since: str,
                         page_pattern: str = "{base}page/{n}/", max_pages: int = 200) -> dict:
    re.compile(item_regex)
    return jobs.start(case, f"site-{slugify(name)}", _py("site_listing", "--name", name, "--listing", listing_url,
                                                          "--page-pattern", page_pattern, "--item-regex", item_regex,
                                                          "--since", since, "--max-pages", str(max_pages),
                                                          "--out", str(case / "data/raw/sites" / slugify(name))))


def collect_court_news_hacc(case: Path, since: str) -> dict:
    return jobs.start(case, "hacc", _py("court_news_hacc", "--since", since, "--out", str(case / "data/raw/court_news_hacc")))


def wayback_versions(case: Path, urls: list[str], save: bool = False) -> dict:
    src = _inp(case, "wayback_urls", urls)
    wb = case / "data/processed/wayback" / f"wayback_{_stamp()}.json"
    wb.parent.mkdir(parents=True, exist_ok=True)
    check = _py("wayback_check", "--sources", str(src), "--out", str(wb), *(["--save"] if save else []))
    diff = _py("wayback_diff", "--wayback", str(wb), "--raw", str(case / "data/raw/wayback"),
               "--out", str(wb.with_name(wb.stem + "_diff.jsonl")))
    return jobs.start(case, "wayback", [sys.executable, "-m", "social.jobrunner_chain", "--", json.dumps([check, diff])])


def channel_liveness(case: Path, channels: list[str]) -> dict:
    src = _inp(case, "liveness", [c.strip().lstrip("@") for c in channels])
    return jobs.start(case, "liveness", _py("channel_liveness", str(src), str(case / "data/processed/channel_liveness.jsonl")))


def official_declarations_ua(case: Path, officials: list[dict]) -> dict:
    """officials: [{key, query, workplace_regex, official_role, role_source}] — public officials in service role only."""
    for o in officials:
        missing = [k for k in ("key", "query", "workplace_regex", "official_role", "role_source") if not o.get(k)]
        if missing:
            raise ValueError(f"{o.get('key', '?')}: бракує {missing} (роль і її джерело обов'язкові: лише публічні посадовці)")
        re.compile(o["workplace_regex"])
    cfg = _inp(case, "officials", [json.dumps({"officials": officials}, ensure_ascii=False)], "json")
    return jobs.start(case, "declarations", _py("declarations_ua", "--config", str(cfg), "--raw", str(case / "data/raw/nazk"),
                                                "--out", str(case / "data/processed/declarations.json")))
