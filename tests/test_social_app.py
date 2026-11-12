"""Tests for the Social app layer (social/): case workspace, tools, jobs, agents, MCP server, CLI."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from social import agents, jobs
from social.case import create_case, find_case, read_jsonl
from social.tools import core


@pytest.fixture()
def case(tmp_path):
    return create_case(tmp_path / "c", "Тестовий кейс", "Що сталося?")


def test_create_case_layout(case):
    for f in ["case.yaml", "AGENTS.md", "CLAUDE.md", "GEMINI.md", ".gitignore", "log.md", ".mcp.json", "opencode.json",
              "report/report.md", "methodology/03-fact-check.md", ".claude/skills/fact-check/SKILL.md"]:
        assert (case / f).exists(), f
    assert not (case / "dot-gitignore").exists()
    assert "Тестовий кейс" in (case / "case.yaml").read_text(encoding="utf-8")
    mcp = json.loads((case / ".mcp.json").read_text())["mcpServers"]["social"]
    assert mcp["args"][-2:] == ["--case", str(case)]
    if (case / ".git").exists():
        assert subprocess.run(["git", "log", "--oneline"], cwd=case, capture_output=True, text=True).stdout.strip()
    with pytest.raises(FileExistsError):
        create_case(case, "again")


def test_find_case_walks_up(case, monkeypatch):
    monkeypatch.delenv("SOCIAL_CASE", raising=False)
    assert find_case(case / "wiki" / "precommit") == case


def test_precommit_is_committed_and_never_overwritten(case):
    r = core.precommit(case, "Питання і критерії", "- Q1: …")
    assert (case / r["file"]).exists()
    with pytest.raises(FileExistsError):
        core.precommit(case, "Питання і критерії", "інше")
    assert "попередня фіксація" in (case / "log.md").read_text(encoding="utf-8")


def test_fact_check_and_judgment_records(case):
    a = core.fact_check(case, "X сказав Y", "перекручено", "B", "пояснення", ["https://example.org"])
    b = core.fact_check(case, "Z", "правда", "A", "документ", [])
    assert (a["id"], b["id"]) == ("F1", "F2")
    with pytest.raises(ValueError):
        core.fact_check(case, "Q", "можливо", "A", "", [])
    j = core.judgment(case, "Збір організовано згори", 0.65, "C", "середній внесок завеликий")
    assert j["id"] == "J1" and len(core.list_records(case, "judgments")) == 1
    with pytest.raises(ValueError):
        core.judgment(case, "x", 1.5, "C", "")
    st = core.case_status(case)
    assert st["factchecks"] == 2 and st["judgments"] == 1
    assert any("сценарі" in h for h in st["recommendations"])


def test_news_queries_tsv():
    tsv = core.news_queries_tsv(["тестовий запит"], ["uk", "en"])
    assert "тестовий запит\tuk\tUA\tUA:uk\tyes" in tsv and "тестовий запит\ten-US\tUS\tUS:en\tyes" in tsv


MODEL = {"horizon": "2027-05-12",
         "scenarios": [{"id": "A", "name": "a", "p": 0.25}, {"id": "B", "name": "b", "p": 0.35},
                       {"id": "C", "name": "c", "p": 0.40}],
         "indicators": [{"id": "I5", "name": "bill", "group": "attack", "window_end": "2027-02-11",
                         "p_given": {"A": 0.10, "B": 0.20, "C": 0.65}},
                        {"id": "I6", "name": "accuse", "group": "attack", "window_end": "2027-01-12",
                         "p_given": {"A": 0.15, "B": 0.35, "C": 0.80}}],
         "forecasts": [{"id": "F1", "text": "t", "p": 0.6, "resolve_by": "2027-02-11", "resolve": "r"}]}


def test_forecast_bayes_with_correlated_group(case):
    core.forecast_define(case, json.loads(json.dumps(MODEL)))
    core.forecast_observe(case, "I5", True, "2026-12-01", "https://x", "A")
    st = core.forecast_state(case, today="2026-12-02")
    assert round(st["scenarios"]["C"], 2) == 0.73  # 0.4*0.65 / (0.025+0.07+0.26)
    core.forecast_observe(case, "I6", True, "2026-12-03", "https://y", "B")
    st2 = core.forecast_state(case, today="2026-12-04")
    assert st2["indicators"]["I6"]["power"] == 0.5 and st2["scenarios"]["C"] > st["scenarios"]["C"]
    core.forecast_resolve(case, "F1", "1", "2027-02-11", "https://z", "A")
    st3 = core.forecast_state(case, today="2027-02-12")
    assert st3["track_record"]["resolved"] == 1 and abs(st3["track_record"]["mean_brier"] - 0.16) < 1e-9
    assert (case / "data/processed/forecast/history.csv").exists()


def test_forecast_define_validates(case):
    bad = json.loads(json.dumps(MODEL))
    bad["scenarios"][0]["p"] = 0.5
    with pytest.raises(ValueError):
        core.forecast_define(case, bad)
    bad = json.loads(json.dumps(MODEL))
    del bad["indicators"][0]["p_given"]["C"]
    with pytest.raises(ValueError):
        core.forecast_define(case, bad)


def test_background_job_finishes(case):
    rec = jobs.start(case, "echo", [sys.executable, "-c", "print('hello from job')"])
    for _ in range(100):
        st = jobs.status(case, rec["id"])[0]
        if st["state"] == "finished":
            break
        time.sleep(0.1)
    assert st["state"] == "finished" and st["exit_code"] == 0 and "hello from job" in "\n".join(st["log_tail"])


def test_report_build_markdown(case):
    out = core.report_build(case)
    assert (case / out["html"]).exists()
    with pytest.raises(ValueError):
        core.report_build(case, "../outside.md")


def test_agent_argv(case, tmp_path):
    c = agents.BACKENDS["claude"].interactive(case, "claude-opus-5-5")
    assert c[:3] == ["claude", "--model", "claude-opus-5-5"] and str(case / ".mcp.json") in c
    argv, on_stdout = agents.BACKENDS["codex"].headless(case, "gpt-x", "hi", tmp_path / "o.md", readonly=True)
    assert not on_stdout and "-o" in argv and "read-only" in argv
    assert any(a.startswith("mcp_servers.social.args=") and "--readonly" in a for a in argv)
    setup = agents.BACKENDS["agy"].setup_cmd()
    assert setup[:5] == ["agy", "mcp", "add", "social", "--"] and "--case" not in setup
    assert agents.BACKENDS["opencode"].headless(case, "opencode/x", "hi", tmp_path / "o.md")[0][:4] == ["opencode", "run", "-m", "opencode/x"]


def test_mcp_server_lists_tools(case):
    from social.mcp_server import build
    tools = asyncio.run(build(case).list_tools())
    names = {t.name for t in tools}
    assert {"case_status", "precommit", "collect_telegram", "collect_news", "fact_check", "judgment",
            "forecast_define", "forecast_state", "report_build", "job_status"} <= names


def test_mcp_readonly_has_no_write_tools(case):
    from social.mcp_server import READONLY_TOOLS, build
    names = {t.name for t in asyncio.run(build(case, readonly=True).list_tools())}
    assert names == set(READONLY_TOOLS)
    assert "--readonly" in json.loads((case / ".mcp-readonly.json").read_text())["mcpServers"]["social"]["args"]


def test_mcp_tool_call(case):
    from social.mcp_server import build
    res = asyncio.run(build(case).call_tool("judgment", {"statement": "s", "probability": 0.7, "level": "C",
                                                         "reasoning": "r"}))
    assert read_jsonl(case / "wiki/judgments.jsonl")[0]["probability"] == 0.7
    assert res is not None


def test_collectors_import():
    for m in ["social.collectors.telegram_web", "social.collectors.news", "social.collectors.youtube", "social.tools.forecast"]:
        r = subprocess.run([sys.executable, "-m", m, "--help"], capture_output=True, text=True)
        assert r.returncode == 0, (m, r.stderr[-500:])


def test_cli_new_and_status(tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIAL_CONFIG_DIR", str(tmp_path / "cfg"))
    r = subprocess.run([sys.executable, "-m", "social", "new", "CLI кейс", "--dir", str(tmp_path / "k")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, "-m", "social", "status", "--case", str(tmp_path / "k")], capture_output=True, text=True)
    assert r.returncode == 0 and "CLI кейс" in r.stdout
