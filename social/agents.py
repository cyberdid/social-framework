"""Agent backends: the coding agents Social can drive, each with its own model choice.

Each backend runs on the user's own installation and login (subscription or local models).
Social never handles the agent's credentials.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .case import mcp_command

from .mcp_server import READONLY_TOOLS

READONLY_TOOLS_CLAUDE = [f"mcp__social__{t}" for t in READONLY_TOOLS]


@dataclass
class Backend:
    name: str
    binary: str
    title: str
    known_models: list[str] = field(default_factory=list)
    list_cmd: list[str] | None = None  # command that prints available models, one per line (first column)

    def installed(self) -> bool:
        return shutil.which(self.binary) is not None

    def models(self, timeout: int = 40) -> list[str]:
        if self.list_cmd and self.installed():
            try:
                r = subprocess.run(self.list_cmd, capture_output=True, text=True, timeout=timeout)
                found = [l.split()[0] for l in r.stdout.splitlines()
                         if l.strip() and not l.lower().startswith(("fetching", "available", "model"))]
                if found:
                    return found
            except (subprocess.TimeoutExpired, OSError):
                pass
        return list(self.known_models)

    # --- argv builders -------------------------------------------------------------------------
    def interactive(self, case: Path, model: str | None) -> list[str]:
        raise NotImplementedError

    def headless(self, case: Path, model: str | None, prompt: str, out_file: Path,
                 readonly: bool = False) -> tuple[list[str], bool]:
        """argv and whether the answer is on stdout (False: the agent writes out_file itself).
        readonly: the agent may read the case and call read-only Social tools, but not change anything."""
        raise NotImplementedError


class Claude(Backend):
    def interactive(self, case, model):
        return ["claude", *(["--model", model] if model else []), "--mcp-config", str(case / ".mcp.json"),
                "--allowedTools", "mcp__social"]

    def headless(self, case, model, prompt, out_file, readonly=False):
        allowed = ["Read", "Grep", "Glob", *(READONLY_TOOLS_CLAUDE if readonly else ["mcp__social", "Write", "Edit", "Bash"])]
        cfg = case / (".mcp-readonly.json" if readonly else ".mcp.json")
        return ["claude", "-p", prompt, *(["--model", model] if model else []), "--mcp-config", str(cfg),
                "--strict-mcp-config", "--allowedTools", ",".join(allowed), "--output-format", "text"], True


class Codex(Backend):
    def _mcp(self, case, readonly=False):
        # Social tools are auto-approved: they only touch the case folder (raw data append-only)
        cmd = mcp_command(case, readonly)
        return ["-c", f"mcp_servers.social.command={json.dumps(cmd[0])}", "-c", f"mcp_servers.social.args={json.dumps(cmd[1:])}",
                "-c", 'mcp_servers.social.default_tools_approval_mode="approve"']

    def interactive(self, case, model):
        return ["codex", *(["-m", model] if model else []), *self._mcp(case)]

    def headless(self, case, model, prompt, out_file, readonly=False):
        return ["codex", "exec", *(["-m", model] if model else []), *self._mcp(case, readonly),
                "--sandbox", "read-only" if readonly else "workspace-write",
                "--skip-git-repo-check", "-o", str(out_file), prompt], False


class Antigravity(Backend):
    """agy keeps MCP servers in its own config: `social setup agy` registers Social once (case = current folder)."""

    def interactive(self, case, model):
        return ["agy", *(["--model", model] if model else [])]

    def headless(self, case, model, prompt, out_file, readonly=False):
        allow = [f"mcp(social/{t})" for t in READONLY_TOOLS] if readonly else ["mcp(social/*)"]
        return ["agy", "-p", prompt, "--settings", json.dumps({"permissions": {"allow": allow}}), *(["--model", model] if model else []), "--output-format", "text"], True

    def mcp_registered(self) -> bool:
        if not self.installed():
            return False
        r = subprocess.run(["agy", "mcp", "list"], capture_output=True, text=True, timeout=30)
        return "social" in r.stdout

    def setup_cmd(self) -> list[str]:
        cmd = mcp_command(Path("."))
        # no --case: the server finds the case from $SOCIAL_CASE or the current folder; "--" lets args start with "-"
        return ["agy", "mcp", "add", "social", "--", cmd[0], *cmd[1:-2]]


class OpenCode(Backend):
    def interactive(self, case, model):
        return ["opencode", *(["-m", model] if model else []), str(case)]

    def headless(self, case, model, prompt, out_file, readonly=False):
        return ["opencode", "run", *(["-m", model] if model else []), prompt], True


BACKENDS: dict[str, Backend] = {
    "claude": Claude("claude", "claude", "Claude Code (Anthropic)",
                     ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2", "opus", "sonnet", "haiku"]),
    "codex": Codex("codex", "codex", "Codex CLI (OpenAI)", []),
    "agy": Antigravity("agy", "agy", "Antigravity CLI (Google)", [], ["agy", "models"]),
    "opencode": OpenCode("opencode", "opencode", "opencode (open source; free and local models)", [], ["opencode", "models"]),
}


def get(name: str) -> Backend:
    if name not in BACKENDS:
        raise SystemExit(f"Невідомий агент «{name}». Доступні: {', '.join(BACKENDS)}")
    return BACKENDS[name]


def run_headless(name: str, case: Path, model: str | None, prompt: str, out_file: Path, timeout: int = 3600,
                 readonly: bool = False) -> str:
    b = get(name)
    if not b.installed():
        raise SystemExit(f"{b.title} не встановлено ({b.binary} не знайдено в PATH)")
    argv, on_stdout = b.headless(case, model, prompt, out_file, readonly)
    env = {**os.environ, "SOCIAL_CASE": str(case)}
    r = subprocess.run(argv, cwd=case, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env)
    text = r.stdout if on_stdout else (out_file.read_text(encoding="utf-8") if out_file.exists() else "")
    if r.returncode != 0 and not text.strip():
        raise SystemExit(f"{b.title} завершився з кодом {r.returncode}:\n{r.stderr[-2000:]}")
    if on_stdout:
        out_file.write_text(text, encoding="utf-8")
    return text
