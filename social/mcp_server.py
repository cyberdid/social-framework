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
               "collect_telegram_search", "collect_gdelt", "collect_reddit", "resolve_news_articles", "fetch_commoncrawl",
               "archive_site_listing", "collect_court_news_hacc", "wayback_versions", "channel_liveness",
               "official_declarations_ua", "lexicon_template", "analyze_corpus", "analyze", "make_figures", "forecast_watch",
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
    def collect_telegram_search(channels: list[str], terms: list[str], start: str, end: str | None = None,
                                relevance_regex: str | None = None, snowball_max: int = 200) -> str:
        """Background job: keyword search inside public Telegram channels (t.me/s/<ch>?q=<term>, no login), every word
        form as a separate term; posts matching relevance_regex (default: any term) drive a one-hop snowball.
        Faster than full timelines when the topic is narrow. Output: data/raw/telegram_search/<UTC>/."""
        return out(core.collect_telegram_search(case, channels, terms, start, end, relevance_regex, snowball_max))

    @srv.tool()
    def collect_gdelt(queries: list[str], start: str, end: str) -> str:
        """Background job: media coverage list from the open GDELT DOC 2.0 API, day windows (YYYY-MM-DD).
        Output: data/raw/gdelt/."""
        return out(core.collect_gdelt(case, queries, start, end))

    @srv.tool()
    def collect_reddit(terms: list[str], after: str, before: str, subreddits: list[str] | None = None) -> str:
        """Background job: Reddit posts and comments from the open Arctic Shift archive (reddit.com itself is not
        bypassed); authors pseudonymised. Retries are safe (resumable). Output: data/raw/reddit/."""
        return out(core.collect_reddit(case, terms, after, before, subreddits))

    @srv.tool()
    def resolve_news_articles(since: str = "", domains: list[str] | None = None, decode_only: bool = False,
                              news_dirs: list[str] | None = None) -> str:
        """Background job: turn Google News links from collect_news into publisher URLs and archive the article pages
        (needs the 'news' extra). decode_only for publishers blocked in your network (then fetch_commoncrawl /
        wayback_versions). Output: data/raw/news_articles/."""
        return out(core.resolve_news_articles(case, since, domains, decode_only, news_dirs))

    @srv.tool()
    def fetch_commoncrawl(urls: list[str], crawls: list[str]) -> str:
        """Background job: fetch exact URLs from the open Common Crawl archive (e.g. crawls ["CC-MAIN-2026-43"]),
        for pages you cannot reach directly. Output: data/raw/commoncrawl/."""
        return out(core.fetch_commoncrawl(case, urls, crawls))

    @srv.tool()
    def archive_site_listing(name: str, listing_url: str, item_regex: str, since: str,
                             page_pattern: str = "{base}page/{n}/", max_pages: int = 200) -> str:
        """Background job: archive every item of a news site's paginated listing since a date (topic selection later,
        on full text). item_regex captures item URLs on a listing page. Output: data/raw/sites/<name>/."""
        return out(core.archive_site_listing(case, name, listing_url, item_regex, since, page_pattern, max_pages))

    @srv.tool()
    def collect_court_news_hacc(since: str) -> str:
        """Background job: official press releases of Ukraine's High Anti-Corruption Court since a date.
        Output: data/raw/court_news_hacc/."""
        return out(core.collect_court_news_hacc(case, since))

    @srv.tool()
    def wayback_versions(urls: list[str], save: bool = False) -> str:
        """Background job: Wayback Machine captures per URL (and optionally request a new capture), then the text
        difference between the earliest and latest capture — shows silent edits of statements and articles.
        Output: data/processed/wayback/."""
        return out(core.wayback_versions(case, urls, save))

    @srv.tool()
    def channel_liveness(channels: list[str]) -> str:
        """Background job: is each Telegram channel public and alive (last post date, subscribers)? Use before
        collecting. Output: data/processed/channel_liveness.jsonl."""
        return out(core.channel_liveness(case, channels))

    @srv.tool()
    def official_declarations_ua(officials: list[dict]) -> str:
        """Background job (Ukraine): career of PUBLIC OFFICIALS in their service role from public NAZK declarations —
        year, workplace, position, own declared income. Each item: {key, query ("Прізвище Ім'я"), workplace_regex,
        official_role, role_source (URL/document naming them in that role)}. Never use for private persons; assets,
        property and family data are not extracted. Output: data/processed/declarations.json."""
        return out(core.official_declarations_ua(case, officials))

    # ---------------------------------------------------------------- analysis (reads analysis/lexicon.yaml)
    from .analysis import coordination as A_coord, corpus as A_corpus, lexicon as A_lex, measures as A_meas, sources as A_src

    @srv.tool()
    def lexicon_template() -> str:
        """Create analysis/lexicon.yaml (+ analysis/channel_groups.csv) — the case's measurement definitions:
        relevance (mention/context/exclude), channel groups, periods, frames, claims, topics, versions, actors,
        reciprocity, media origins, emotions. Fill it before looking at matches; precommit it."""
        return out({"created": str(A_lex.write_template(case).relative_to(case))})

    @srv.tool()
    def analyze_corpus() -> str:
        """Merge every Telegram snapshot (timelines + search) into one cleaned, de-duplicated corpus labelled from the
        lexicon (relevance reason, group, period, language, frames, topics, claims). Run again after new collection
        or lexicon changes. Output: data/processed/corpus/."""
        return out(A_corpus.build(case))

    @srv.tool()
    def analyze(measure: str) -> str:
        """Run one measure over the corpus (after analyze_corpus). measure:
        volume (daily posts by group, top channels) | frames (shares by group x period with Wilson CIs and view shares)
        | claims (spread of each claim: first post, groups, views, before the official document) | mood (reactions ->
        emotions by group/topic/period) | comments (stratified sample for manual coding + repeated texts) | versions
        (explanatory versions across posts, comments, archived pages) | coordination (copy network, synchronous copies
        vs a permutation null) | recall (search completeness vs full timelines) | media (outlets by origin, claims in
        headlines) | relations (actor co-mention sentences) | reciprocity (official action -> reply between bodies).
        Outputs: data/processed/analysis/<measure>/."""
        fn = {"volume": A_meas.volume, "frames": A_meas.frames, "claims": A_meas.claims, "mood": A_meas.mood,
              "comments": A_meas.comment_sample, "versions": A_meas.versions, "coordination": A_coord.analyze,
              "recall": A_src.recall, "media": A_src.media, "relations": A_src.relations, "reciprocity": A_src.reciprocity}
        if measure not in fn:
            raise ValueError(f"measure is one of {list(fn)}")
        return out(fn[measure](case))

    @srv.tool()
    def make_figures(names: list[str] | None = None) -> str:
        """Draw report figures from processed outputs into report/fig/ (PNG + PDF): volume, frames, mood, verdicts,
        judgments, copy_network, claims_timeline, scenarios. Missing inputs are skipped with a reason. Embed them in
        report/report.md (or .tex) and explain each one in plain words under the figure."""
        from . import figures
        return out(figures.make(case, names))

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
                         "p_given": {"A": 0.8, "B": 0.3, ...}, "linked_forecast": "F1"?,
                         "measure": {...}? (automatic, see forecast_watch)}, ...],
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
    def forecast_watch(record: bool = True) -> str:
        """Evaluate indicators that carry a "measure" block on news headlines (collect_news with window_days=1 keeps
        them current): attention_below (N weeks under ratio x baseline) or share_increase (a week where a regex's share
        of headlines >= factor x baseline). Met indicators are journaled automatically (level A); closed windows as
        not_occurred. Run it after each news collection, then forecast_state."""
        from .tools import forecast_watch as FW
        return out(FW.watch(case, record))

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
