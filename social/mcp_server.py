"""Social MCP server: the case tools for any MCP-capable agent (Claude Code, Codex, Antigravity, opencode, ...)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import jobs
from .case import find_case
from .tools import core

INSTRUCTIONS = (
    "Social case tools. Read AGENTS.md in the case folder first: it holds the methodology as recommendations "
    "(you decide how much of it a case needs) and a few hard limits (public data only, no profiling of private "
    "persons, raw data is append-only). Collectors run as background jobs; poll them with job_status."
)


READONLY_TOOLS = ("case_status", "list_records", "forecast_state", "job_status")
WRITE_TOOLS = ("log_entry", "precommit", "collect_telegram", "collect_youtube", "collect_news", "archive_url", "fact_check",
               "judgment", "forecast_define", "forecast_observe", "forecast_revise", "forecast_resolve", "report_build")


def build(case: Path, readonly: bool = False) -> MCPServer:
    """readonly: expose only tools that read the case (used for the red-team reviewer)."""
    srv = MCPServer("social", instructions=INSTRUCTIONS + (" READ-ONLY session: only reading tools." if readonly else ""))

    def out(x: Any) -> str:
        return json.dumps(x, ensure_ascii=False, indent=1, default=str)

    @srv.tool()
    def case_status() -> str:
        """Case overview: case.yaml, raw snapshots, precommits, fact-checks, judgments, forecast models,
        background jobs, last log lines and soft recommendations on what is missing."""
        return out(core.case_status(case))

    @srv.tool()
    def log_entry(text: str) -> str:
        """Append one line to the case log (log.md). Use it for every meaningful operation or decision."""
        return core.log_entry(case, text)

    @srv.tool()
    def precommit(title: str, markdown: str) -> str:
        """Freeze questions, claims and verification criteria BEFORE analysis: writes wiki/precommit/<date>-<slug>.md
        and commits it to the case git repo. Existing precommits are never overwritten."""
        return out(core.precommit(case, title, markdown))

    @srv.tool()
    def collect_telegram(channels: list[str], relevance_regex: str, start: str, end: str | None = None,
                         depth: int = 1, max_channels: int = 150, max_pages: int = 150) -> str:
        """Background job: public Telegram channels via the t.me/s web preview (no login). Keeps every post in the
        window [start, end] (ISO datetimes with offset); posts matching relevance_regex (case-insensitive) drive a
        snowball to forwarded/linked channels up to `depth` hops. Output: data/raw/telegram_web/<UTC>/."""
        return out(core.collect_telegram(case, channels, relevance_regex, start, end, depth, max_channels, max_pages))

    @srv.tool()
    def collect_youtube(queries: list[str], since: str, per_query: int = 40, comment_videos: int = 20,
                        max_comments: int = 1000) -> str:
        """Background job: YouTube search, video metadata and comments (yt-dlp, no key; comment authors are
        pseudonymised). since = YYYY-MM-DD. Output: data/raw/youtube/<UTC>/."""
        return out(core.collect_youtube(case, queries, since, per_query, comment_videos, max_comments))

    @srv.tool()
    def collect_news(queries: list[str], start: str, end: str, languages: list[str] | None = None,
                     window_days: int = 7, out_name: str = "gnews") -> str:
        """Background job: Google News RSS headlines per query, language (uk, ru, en, pl, de) and date window
        (start/end YYYY-MM-DD). If a window returns 100 items (capped), rerun with smaller window_days and another
        out_name. Resumable. Output: data/raw/<out_name>/items.jsonl."""
        return out(core.collect_news(case, queries, start, end, languages, window_days, out_name))

    @srv.tool()
    def job_status(job_id: str | None = None) -> str:
        """State of background jobs (running / finished / stopped), exit codes and the last log lines."""
        return out(jobs.status(case, job_id))

    @srv.tool()
    def archive_url(url: str) -> str:
        """Fetch one public page now and archive it (gzip + sha256 in data/raw/web, text in data/processed/web_text).
        Returns a text preview. Use for documents, court decisions, official statements, articles."""
        return out(core.archive_url(case, url))

    @srv.tool()
    def fact_check(claim: str, verdict: str, level: str, explanation: str, sources: list[str], kernel: str = "",
                   first_seen: str = "", spread: str = "") -> str:
        """Record a checked claim. verdict: правда | ймовірно правда | частково | перекручено | не підтверджено |
        припущення | неправда | фейк. level: A (document/measured) | B (several independent sources) | C (one source)
        | D (no verifiable data). kernel = the true part, first_seen = earliest source, spread = how it travelled."""
        return out(core.fact_check(case, claim, verdict, level, explanation, sources, kernel, first_seen, spread))

    @srv.tool()
    def judgment(statement: str, probability: float, level: str, reasoning: str, alternatives: str = "",
                 would_change_my_mind: str = "") -> str:
        """Record your own analyst verdict: a checkable statement with a probability 0..1, the evidence level behind
        it, reasoning, alternatives and what would change your mind. Take positions; uncertainty is a probability."""
        return out(core.judgment(case, statement, probability, level, reasoning, alternatives, would_change_my_mind))

    @srv.tool()
    def list_records(kind: str) -> str:
        """List records: factchecks | judgments | forecast_journal | archive."""
        return out(core.list_records(case, kind))

    @srv.tool()
    def forecast_define(model: dict) -> str:
        """Freeze a scenario model (committed). Format:
        {"horizon": "YYYY-MM-DD",
         "scenarios": [{"id": "A", "name": "...", "p": 0.3, "summary": "...", "resolve": "criterion"}, ...],  # p sum to 1
         "indicators": [{"id": "I1", "name": "...", "window_end": "YYYY-MM-DD", "group": "g1",
                         "p_given": {"A": 0.8, "B": 0.3, ...}, "linked_forecast": "F1"?}, ...],
         "forecasts": [{"id": "F1", "text": "...", "p": 0.6, "resolve_by": "YYYY-MM-DD", "resolve": "criterion"}]}
        Indicators of one group are treated as correlated (later ones count with exponent 0.5)."""
        return out(core.forecast_define(case, model))

    @srv.tool()
    def forecast_observe(indicator: str, occurred: bool, date: str, source: str, level: str, note: str = "") -> str:
        """Journal an indicator outcome (it happened, or its window closed without it). Probabilities are recomputed
        by Bayes in forecast_state."""
        return out(core.forecast_observe(case, indicator, occurred, date, source, level, note))

    @srv.tool()
    def forecast_revise(target: str, p: Any, date: str, reason: str) -> str:
        """Explicit analyst revision with a reason: target 'scenarios' with p={"A":0.3,...} (sum 1) or a forecast id
        with p=0.4. Shown as a revision, never hidden."""
        return out(core.forecast_revise(case, target, p, date, reason))

    @srv.tool()
    def forecast_resolve(target: str, outcome: str, date: str, source: str, level: str, note: str = "") -> str:
        """Resolve a forecast (target F*, outcome '1' or '0') or the scenario set (target 'S', outcome = scenario id).
        Feeds the Brier track record."""
        return out(core.forecast_resolve(case, target, outcome, date, source, level, note))

    @srv.tool()
    def forecast_state(today: str | None = None) -> str:
        """Current scenario probabilities, history, what-if for each open indicator, coherence flags between the
        scenario model and linked forecasts (>10 pp), forecasts and Brier track record. Also writes
        data/processed/forecast/{state.json,history.csv,whatif.csv} for charts."""
        st = core.forecast_state(case, today)
        return out({k: st[k] for k in ("as_of", "scenarios", "scenario_names", "whatif", "coherence", "track_record")}
                   | {"forecasts": [{k: f.get(k) for k in ("id", "text", "p_now", "resolve_by", "status", "brier")}
                                    for f in st["forecasts"]]})

    @srv.tool()
    def report_build(source: str = "report/report.md") -> str:
        """Build the report: Markdown -> HTML (and PDF if pandoc + tectonic/xelatex exist), or a .tex file -> PDF with
        tectonic. Paths are relative to the case folder."""
        return out(core.report_build(case, source))

    if readonly:
        for t in WRITE_TOOLS:
            srv.remove_tool(t)
    return srv


def main(case_arg: str | None = None, readonly: bool = False) -> None:
    case = Path(case_arg).resolve() if case_arg else find_case()
    build(case, readonly).run("stdio")
