"""Case workspace: a folder (and git repo) that holds everything about one case.

The case state lives in files, not in a chat, so any agent can pick up where another stopped.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

CASE_FILE = "case.yaml"
CONFIG_DIR = Path(os.environ.get("SOCIAL_CONFIG_DIR", Path.home() / ".config/social"))


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text: str) -> str:
    s = re.sub(r"[^\w-]+", "-", text.strip().lower(), flags=re.U).strip("-")
    return s[:60] or "case"


def find_case(start: Path | None = None) -> Path:
    """The case root: $SOCIAL_CASE, or the nearest folder upwards that has case.yaml."""
    env = os.environ.get("SOCIAL_CASE")
    if env:
        return Path(env).resolve()
    p = (start or Path.cwd()).resolve()
    for d in [p, *p.parents]:
        if (d / CASE_FILE).exists():
            return d
    raise FileNotFoundError("Not inside a Social case (no case.yaml). Create one with: social new <name>")


def durable_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line.rstrip("\n") + "\n")
        f.flush()
        os.fsync(f.fileno())


def append_jsonl(path: Path, rec: dict) -> dict:
    rec = {**rec, "recorded_at": rec.get("recorded_at") or now_utc()}
    durable_append(path, json.dumps(rec, ensure_ascii=False))
    return rec


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.open(encoding="utf-8") if x.strip()]


def log(case: Path, text: str) -> None:
    """One line per operation in log.md (who did what, when)."""
    durable_append(case / "log.md", f"- {now_utc()} — {text}")


def git(case: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", *args], cwd=case, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def commit(case: Path, paths: list[str], message: str) -> str | None:
    """Commit the given paths in the case repo; returns the short hash or None if git is unavailable."""
    if not shutil.which("git") or not (case / ".git").exists():
        return None
    git(case, "add", "--", *paths)
    if not git(case, "diff", "--cached", "--name-only", check=False):
        return git(case, "rev-parse", "--short", "HEAD", check=False) or None
    env_args = []
    if not git(case, "config", "user.name", check=False):
        env_args = ["-c", "user.name=Social", "-c", "user.email=social@localhost"]
    git(case, *env_args, "commit", "-q", "-m", message)
    return git(case, "rev-parse", "--short", "HEAD")


def mcp_command(case: Path, readonly: bool = False) -> list[str]:
    """How an agent starts the Social MCP server for this case (same interpreter as this install)."""
    return [sys.executable, "-m", "social", "mcp", *(["--readonly"] if readonly else []), "--case", str(case)]


def write_agent_configs(case: Path) -> None:
    cmd = mcp_command(case)
    (case / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"social": {"command": cmd[0], "args": cmd[1:]}}}, indent=2) + "\n")
    ro = mcp_command(case, readonly=True)
    (case / ".mcp-readonly.json").write_text(json.dumps(
        {"mcpServers": {"social": {"command": ro[0], "args": ro[1:]}}}, indent=2) + "\n")
    (case / "opencode.json").write_text(json.dumps(
        {"$schema": "https://opencode.ai/config.json",
         "mcp": {"social": {"type": "local", "command": cmd, "enabled": True}},
         "instructions": ["AGENTS.md"]}, indent=2) + "\n")


def _copy_tree(src, dst: Path) -> None:
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_tree(item, target)
        else:
            target.write_bytes(item.read_bytes())


def create_case(path: Path, title: str, question: str = "", language: str = "uk") -> Path:
    path = path.resolve()
    if (path / CASE_FILE).exists():
        raise FileExistsError(f"{path} is already a case")
    path.mkdir(parents=True, exist_ok=True)
    tpl = resources.files("social") / "templates"
    _copy_tree(tpl / "case", path)
    if (path / "dot-gitignore").exists():
        (path / "dot-gitignore").rename(path / ".gitignore")
    meth = resources.files("social") / "methodology"
    (path / "methodology").mkdir(exist_ok=True)
    _copy_tree(meth, path / "methodology")
    # the same methodology as Claude Code skills (other agents read methodology/ via AGENTS.md)
    for f in sorted((path / "methodology").glob("*.md")):
        text = f.read_text(encoding="utf-8")
        if text.startswith("---"):
            d = path / ".claude/skills" / f.stem.split("-", 1)[-1]
            d.mkdir(parents=True, exist_ok=True)
            (d / "SKILL.md").write_text(text, encoding="utf-8")
    cy = (path / CASE_FILE).read_text(encoding="utf-8")
    cy = cy.replace("{{title}}", title.replace('"', "'")).replace("{{question}}", question.replace('"', "'")) \
           .replace("{{created}}", now_utc()).replace("{{language}}", language)
    (path / CASE_FILE).write_text(cy, encoding="utf-8")
    for d in ["data/raw", "data/processed", "wiki/precommit", "wiki/forecasts", "analysis", "report", "logs"]:
        (path / d).mkdir(parents=True, exist_ok=True)
    write_agent_configs(path)
    log(path, f"кейс створено: {title}")
    if shutil.which("git") and not (path / ".git").exists():
        git(path, "init", "-q")
        commit(path, ["."], f"case: {title}")
    return path
