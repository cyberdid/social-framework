"""Tests for the Social web app: auth guard, case API, chat stream parsing and streaming, sanitizer."""
from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("httpx")
from starlette.testclient import TestClient  # noqa: E402

from social.case import create_case  # noqa: E402
from social.web import chat  # noqa: E402
from social.web.sanitize import sanitize  # noqa: E402
from social.web.server import make_app  # noqa: E402

TOKEN = "test-token"
BASE = "http://127.0.0.1:8765"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("SOCIAL_CONFIG_DIR", str(tmp_path / "cfg"))
    cases = tmp_path / "cases"
    cases.mkdir()
    create_case(cases / "demo", "Демо", "Що сталося?")
    client = TestClient(make_app(TOKEN, 8765, cases), base_url=BASE)
    client.headers["X-Social-Token"] = TOKEN
    return client, cases


def test_guard_requires_token_and_local_host(env):
    client, _ = env
    assert TestClient(client.app, base_url=BASE).get("/api/cases").status_code == 401
    assert TestClient(client.app, base_url="http://evil.example").get("/api/cases", headers={"X-Social-Token": TOKEN}).status_code == 403
    r = client.get("/")
    assert r.status_code == 200 and "script-src 'self'" in r.headers["content-security-policy"]


def test_cases_api(env):
    client, cases = env
    assert [c["slug"] for c in client.get("/api/cases").json()] == ["demo"]
    r = client.post("/api/cases", json={"title": "Новий кейс", "question": "Чому?"})
    assert r.status_code == 200 and (cases / r.json()["slug"] / "case.yaml").exists()
    assert client.post("/api/cases", json={"title": ""}).status_code == 400
    st = client.get("/api/cases/demo/status").json()
    assert st["factchecks"] == 0 and st["recommendations"]
    assert client.get("/api/cases/demo/records/factchecks").json() == []
    assert client.get("/api/cases/demo/forecast").json() is None
    assert "кейс створено" in client.get("/api/cases/demo/log").json()["html"]
    assert client.get("/api/cases/nope/status").status_code == 404


def test_file_serving_is_confined_and_sandboxed(env):
    client, cases = env
    r = client.get("/api/cases/demo/file", params={"path": "report/report.md"})
    assert r.status_code == 200 and r.headers["content-security-policy"].startswith("sandbox")
    assert client.get("/api/cases/demo/file", params={"path": "../../cfg/x"}).status_code == 404
    assert client.get("/api/cases/demo/file", params={"path": ".git/config"}).status_code == 404
    assert any(f["path"] == "report/report.md" for f in client.get("/api/cases/demo/files", params={"dir": "report"}).json())
    assert client.get("/api/cases/demo/files", params={"dir": "../.."}).json() == []


def test_report_build_api(env):
    client, cases = env
    r = client.post("/api/cases/demo/report")
    assert r.status_code == 200 and (cases / "demo" / r.json()["html"]).exists()


def test_parsers_cover_each_agent():
    assert chat.parse("claude", {"type": "system", "subtype": "init", "session_id": "s1"}) == [{"t": "session", "id": "s1"}]
    ev = chat.parse("claude", {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__social__case_status", "input": {}}, {"type": "text", "text": "Готово"}]}})
    assert ev[0]["name"] == "case_status" and ev[1] == {"t": "text", "text": "Готово"}
    assert chat.parse("claude", {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "ToolSearch"}]}}) == []
    ev = chat.parse("agy", {"event": "step_update", "step_update": {"step_type": "tool", "state": "ACTIVE", "tool_name": "call_mcp_tool",
                                                                   "tool_info": {"parameters": {"ServerName": "social", "ToolName": "case_status", "Arguments": {}}}}})
    assert ev[0]["name"] == "case_status"
    assert chat.parse("agy", {"event": "step_update", "step_update": {"step_type": "agent_response", "text_delta": "1 фактчек"}})[0]["text"] == "1 фактчек"
    assert chat.parse("codex", {"type": "thread.started", "thread_id": "t1"}) == [{"t": "session", "id": "t1"}]
    assert chat.parse("codex", {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}})[0]["text"] == "ok"
    assert chat.parse("codex", {"type": "turn.failed", "error": {"message": "limit"}})[0] == {"t": "error", "text": "limit"}
    ev = chat.parse("opencode", {"type": "tool_use", "sessionID": "ses1", "part": {"tool": "social_case_status", "state": {"input": {}}}})
    assert ev[0] == {"t": "session", "id": "ses1"} and ev[1]["name"] == "case_status"


def test_chat_argv_resume_and_readonly(tmp_path):
    c = create_case(tmp_path / "c", "t")
    a = chat.chat_argv("claude", c, "claude-opus-5-5", "hi", "sess", readonly=True)
    assert "--resume" in a and str(c / ".mcp-readonly.json") in a and "acceptEdits" not in a
    a = chat.chat_argv("codex", c, None, "hi", "th1", readonly=False)
    assert a[:4] == ["codex", "exec", "resume", "th1"] and 'sandbox_mode="workspace-write"' in a
    assert "--conversation" in chat.chat_argv("agy", c, None, "hi", "cv1", readonly=False)
    assert chat.chat_argv("opencode", c, "opencode/x", "hi", "s", readonly=False)[:6] == ["opencode", "run", "--format", "json", "-m", "opencode/x"]


def test_chat_streams_and_persists(env, monkeypatch):
    client, cases = env
    lines = [{"type": "system", "subtype": "init", "session_id": "S42"},
             {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "mcp__social__case_status", "input": {}}]}},
             {"type": "assistant", "message": {"content": [{"type": "text", "text": "**Готово**: 0 фактчеків <script>x()</script>"}]}},
             {"type": "result", "subtype": "success", "is_error": False}]
    script = "import json\n" + "".join(f"print(json.dumps({l!r}))\n" for l in lines)
    monkeypatch.setattr(chat, "chat_argv", lambda *a, **k: [sys.executable, "-c", script])
    monkeypatch.setattr(chat, "get", lambda name: SimpleNamespace(installed=lambda: True))
    cid = client.post("/api/cases/demo/chats", json={"agent": "claude"}).json()["id"]
    with client.stream("POST", f"/api/cases/demo/chats/{cid}/send", json={"text": "Привіт"}) as r:
        events = [json.loads(x[6:]) for x in r.iter_lines() if x.startswith("data: ")]
    kinds = [e["t"] for e in events]
    assert kinds == ["tool", "text", "done"]
    assert "<strong>Готово</strong>" in events[-1]["html"] and "<script>" not in events[-1]["html"]
    rows = client.get(f"/api/cases/demo/chats/{cid}").json()
    assert [r["role"] for r in rows] == ["meta", "user", "assistant"] and rows[-1]["session"] == "S42"
    assert client.get("/api/cases/demo/chats").json()[0]["title"] == "Привіт"


def test_sanitizer():
    out = sanitize('<p>ok <a href="javascript:alert(1)">x</a> <a href="https://e.org">y</a><script>bad()</script>'
                   '<img src=x onerror=1><b onclick=1>b</b><iframe src=//e></iframe></p>')
    assert "script" not in out and "onerror" not in out and "onclick" not in out and "iframe" not in out
    assert '<a href="https://e.org"' in out and "<a>x</a>" in out


def test_render_md_lists_after_text():
    from social.web.server import render_md
    html = render_md("**Фактчеки**\n- перший\n- другий\n\nТекст\n1. раз\n2. два")
    assert html.count("<li>") == 4 and "<ul>" in html and "<ol>" in html
