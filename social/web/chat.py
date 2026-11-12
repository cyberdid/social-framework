"""Browser chat over the agents' own non-interactive streaming modes.

Every turn runs the chosen agent once in the case folder, resuming the agent's own session, and turns its
JSON event stream into a small common format: session, text, tool, done, error. Conversations are stored
in the case (wiki/chats/<id>.jsonl), so they survive restarts and stay readable as files.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import AsyncIterator

from ..agents import get
from ..case import append_jsonl, log, mcp_command, now_utc, read_jsonl, write_agent_configs
from ..mcp_server import READONLY_TOOLS

CLAUDE_TOOLS = "mcp__social,Read,Write,Edit,Glob,Grep,Bash,WebSearch,WebFetch"


def chat_argv(agent: str, case: Path, model: str | None, prompt: str, session: str | None, readonly: bool) -> list[str]:
    m = ["--model", model] if model else []
    if agent == "claude":
        cfg = case / (".mcp-readonly.json" if readonly else ".mcp.json")
        tools = "Read,Glob,Grep," + ",".join(f"mcp__social__{t}" for t in READONLY_TOOLS) if readonly else CLAUDE_TOOLS
        return ["claude", "-p", prompt, *m, "--mcp-config", str(cfg), "--allowedTools", tools,
                *([] if readonly else ["--permission-mode", "acceptEdits"]),
                "--output-format", "stream-json", "--verbose", *(["--resume", session] if session else [])]
    if agent == "codex":
        cmd = mcp_command(case, readonly)
        c = ["-c", f"mcp_servers.social.command={json.dumps(cmd[0])}", "-c", f"mcp_servers.social.args={json.dumps(cmd[1:])}",
             "-c", 'mcp_servers.social.default_tools_approval_mode="approve"',
             "-c", f'sandbox_mode="{"read-only" if readonly else "workspace-write"}"']
        mm = ["-m", model] if model else []
        if session:
            return ["codex", "exec", "resume", session, "--json", *mm, *c, "--skip-git-repo-check", prompt]
        return ["codex", "exec", "--json", *mm, *c, "--skip-git-repo-check", prompt]
    if agent == "agy":
        allow = [f"mcp(social/{t})" for t in READONLY_TOOLS] if readonly else ["mcp(social/*)"]
        return ["agy", "-p", prompt, *m, "--output-format", "stream-json",
                "--settings", json.dumps({"permissions": {"allow": allow}}),
                *([] if readonly else ["--mode", "accept-edits"]), *(["--conversation", session] if session else [])]
    if agent == "opencode":
        return ["opencode", "run", "--format", "json", *(["-m", model] if model else []),
                *(["--session", session] if session else []), prompt]
    raise ValueError(agent)


def _short(name: str) -> str:
    for pre in ("mcp__social__", "social.", "social__", "social_"):
        name = name.removeprefix(pre)
    return name


def parse(agent: str, d: dict) -> list[dict]:
    """One JSON line of an agent's stream -> common events."""
    ev: list[dict] = []
    if agent == "claude":
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            ev.append({"t": "session", "id": d.get("session_id")})
        elif t == "assistant":
            for c in d.get("message", {}).get("content", []):
                if c.get("type") == "text" and c.get("text"):
                    ev.append({"t": "text", "text": c["text"]})
                elif c.get("type") == "tool_use" and c.get("name") != "ToolSearch":
                    ev.append({"t": "tool", "name": _short(c["name"]), "detail": _detail(c.get("input"))})
        elif t == "result":
            ev.append({"t": "error", "text": d.get("result") or "помилка"} if d.get("is_error") else {"t": "end"})
    elif agent == "agy":
        e = d.get("event")
        if e == "init":
            ev.append({"t": "session", "id": d.get("conversation_id")})
        elif e == "step_update":
            s = d["step_update"]
            if s.get("step_type") == "tool" and s.get("state") == "ACTIVE":
                info = s.get("tool_info", {})
                p = info.get("parameters", {})
                name = p.get("ToolName") if s.get("tool_name") == "call_mcp_tool" else s.get("tool_name")
                ev.append({"t": "tool", "name": _short(name or "tool"), "detail": _detail(p.get("Arguments", p))})
            elif s.get("step_type") == "agent_response" and s.get("text_delta"):
                ev.append({"t": "text", "text": s["text_delta"]})
        elif e == "result":
            r = d.get("result", {})
            ev.append({"t": "end"} if r.get("status") == "SUCCESS" else {"t": "error", "text": r.get("error") or r.get("status")})
    elif agent == "codex":
        t = d.get("type", "")
        if t == "thread.started":
            ev.append({"t": "session", "id": d.get("thread_id")})
        elif t in ("item.started", "item.completed"):
            it = d.get("item", {})
            kind = it.get("type") or it.get("item_type")
            if kind in ("agent_message", "assistant_message") and t == "item.completed" and it.get("text"):
                ev.append({"t": "text", "text": it["text"]})
            elif t == "item.started" and kind in ("mcp_tool_call", "command_execution", "web_search", "file_change"):
                name = it.get("tool") or {"command_execution": "shell", "web_search": "web_search", "file_change": "edit"}.get(kind, kind)
                ev.append({"t": "tool", "name": _short(name), "detail": _detail(it.get("arguments") or it.get("command") or it.get("query"))})
        elif t == "turn.failed":
            e = d.get("error")
            ev.append({"t": "error", "text": (e.get("message") if isinstance(e, dict) else str(e or "помилка"))[:800]})
        elif t == "turn.completed":
            ev.append({"t": "end"})
    elif agent == "opencode":
        part = d.get("part") or {}
        sid = d.get("sessionID") or part.get("sessionID")
        if sid:
            ev.append({"t": "session", "id": sid})
        t = d.get("type")
        if t == "text" and part.get("text"):
            ev.append({"t": "text", "text": part["text"]})
        elif t in ("tool_use", "tool"):
            ev.append({"t": "tool", "name": _short(part.get("tool") or "tool"), "detail": _detail((part.get("state") or {}).get("input"))})
        elif t == "error":
            ev.append({"t": "error", "text": json.dumps(d.get("error"), ensure_ascii=False)[:500]})
    return ev


def _detail(x) -> str:
    if not x:
        return ""
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    return s[:160]


# ------------------------------------------------------------------ conversations stored in the case
def chats_dir(case: Path) -> Path:
    return case / "wiki/chats"


def list_chats(case: Path) -> list[dict]:
    out = []
    for p in sorted(chats_dir(case).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        rows = read_jsonl(p)
        if not rows:
            continue
        head = rows[0]
        first = next((r["text"] for r in rows if r.get("role") == "user"), "")
        out.append({"id": p.stem, "agent": head.get("agent"), "model": head.get("model"), "role": head.get("role_name"),
                    "title": (first or "Нова розмова")[:80], "updated": rows[-1].get("recorded_at"), "turns": sum(r.get("role") == "user" for r in rows)})
    return out


def create_chat(case: Path, agent: str, model: str | None, role_name: str = "analyst") -> dict:
    get(agent)
    cid = f"{now_utc()[:10]}-{uuid.uuid4().hex[:8]}"
    rec = append_jsonl(chats_dir(case) / f"{cid}.jsonl", {"role": "meta", "agent": agent, "model": model, "role_name": role_name})
    return {"id": cid, **rec}


def load_chat(case: Path, cid: str) -> list[dict]:
    p = chats_dir(case) / f"{cid}.jsonl"
    if not p.exists() or "/" in cid:
        raise FileNotFoundError(cid)
    return read_jsonl(p)


RUNNING: dict[str, asyncio.subprocess.Process] = {}


async def send(case: Path, cid: str, text: str) -> AsyncIterator[dict]:
    rows = load_chat(case, cid)
    meta = rows[0]
    agent, model, readonly = meta["agent"], meta.get("model"), meta.get("role_name") == "reviewer"
    session = next((r["session"] for r in reversed(rows) if r.get("session")), None)
    path = chats_dir(case) / f"{cid}.jsonl"
    append_jsonl(path, {"role": "user", "text": text})
    if not get(agent).installed():
        yield {"t": "error", "text": f"{agent} не встановлено"}
        return
    write_agent_configs(case)
    argv = chat_argv(agent, case, model, text, session, readonly)
    env = {**os.environ, "SOCIAL_CASE": str(case)}
    proc = await asyncio.create_subprocess_exec(*argv, cwd=case, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=1 << 24)
    RUNNING[cid] = proc
    reply, tools, new_session, err = [], [], None, None
    try:
        assert proc.stdout
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            for e in parse(agent, d):
                if e["t"] == "tool" and e.get("detail"):
                    e["detail"] = e["detail"].replace(str(case) + "/", "").replace(str(case), ".")
                if e["t"] == "session":
                    new_session = e["id"]
                    continue
                if e["t"] == "text":
                    reply.append(e["text"])
                elif e["t"] == "tool":
                    tools.append(e)
                elif e["t"] == "error":
                    err = e["text"]
                if e["t"] != "end":
                    yield e
        rc = await proc.wait()
        if rc != 0 and not reply and not err:
            assert proc.stderr
            err = (await proc.stderr.read()).decode("utf-8", "replace")[-1500:] or f"код завершення {rc}"
            yield {"t": "error", "text": err}
    finally:
        RUNNING.pop(cid, None)
        full = "\n\n".join(x.strip() for x in reply if x.strip())
        append_jsonl(path, {"role": "assistant", "text": full, "tools": tools, "session": new_session or session,
                            "error": err})
        log(case, f"чат {cid} ({agent}/{model or 'default'}): відповідь {len(full)} симв., інструментів {len(tools)}")
    yield {"t": "done", "text": full}


def stop(cid: str) -> bool:
    p = RUNNING.get(cid)
    if p and p.returncode is None:
        p.terminate()
        return True
    return False
