"""social — command line for the Social app.

  social new "Назва кейсу" [--question ...]     create a case folder (git repo) with methodology and tools
  social chat [--agent claude|codex|agy|opencode] [--model M]   talk to the analyst agent in the case
  social ask "питання" [--agent ...] [--model ...]              one non-interactive turn, answer saved to wiki/sessions/
  social review [--agent ...] [--model ...]     red team: another agent challenges the conclusions (wiki/reviews/)
  social backends [--models]                    installed agents and their models
  social config --agent A --model M [--role reviewer]           defaults (~/.config/social/config.yaml)
  social setup agy                              register the Social tools in Antigravity once
  social web [--port 8765]                      web interface in the browser
  social status | jobs | doctor | mcp
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import __version__, agents, jobs
from .case import CONFIG_DIR, create_case, find_case, log, slugify, write_agent_configs
from .tools import core

DEFAULTS = {"analyst": {"agent": "claude", "model": "claude-opus-5-5"}, "reviewer": {"agent": None, "model": None},
            "cases_dir": str(Path.home() / "SocialCases")}
VENDOR = {"claude": "anthropic", "codex": "openai", "agy": "google", "opencode": "open"}

REVIEW_PROMPT = """Ти — червона команда для кейсу Social у цій теці. Твоє завдання — знайти, де аналітик помиляється.
Прочитай AGENTS.md, case.yaml, log.md, wiki/factchecks.jsonl, wiki/judgments.jsonl, wiki/forecasts/ і report/ (якщо є).
Не змінюй жодних файлів. Відповідай мовою кейсу (за замовчуванням українською), у Markdown:

## Підсумок
3–5 речень: наскільки висновкам можна довіряти і що найслабше.

## Висновки аналітика
Таблиця: ID | твердження | оцінка аналітика | моя оцінка | чому (найслабше місце, альтернативне пояснення, якого доказу бракує).

## Фактчеки
Які вердикти завищені чи занижені і чому.

## Сценарії й прогнози
Чи справді сценарії взаємовиключні й вичерпні, чи індикатори — ранні сигнали, а не критерії; що пропущено.

## Пропущене
Джерела, версії, методи, яких бракує.

## Межі
Чи немає профілювання приватних осіб, деанонімізації, недоведених звинувачень названих людей.
"""


def load_config() -> dict:
    p = CONFIG_DIR / "config.yaml"
    cfg = json.loads(json.dumps(DEFAULTS))
    if p.exists():
        user = yaml.safe_load(p.read_text()) or {}
        for k, v in user.items():
            cfg[k] = {**cfg.get(k, {}), **v} if isinstance(v, dict) else v
    return cfg


def save_config(cfg: dict) -> Path:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CONFIG_DIR, 0o700)
    p = CONFIG_DIR / "config.yaml"
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
    return p


def pick(cfg: dict, role: str, agent: str | None, model: str | None) -> tuple[str, str | None]:
    if agent:  # an explicit agent without a model means that agent's default model
        return agent, model
    a, m = cfg[role].get("agent"), cfg[role].get("model")
    if role == "reviewer" and not a:
        analyst = cfg["analyst"]["agent"]
        for cand in ("codex", "agy", "opencode", "claude"):
            if agents.get(cand).installed() and VENDOR[cand] != VENDOR.get(analyst):
                return cand, model
        return analyst, model
    return a, model or m


def _case(args) -> Path:
    return Path(args.case).resolve() if getattr(args, "case", None) else find_case()


def cmd_new(args):
    cfg = load_config()
    base = Path(args.dir) if args.dir else Path(cfg["cases_dir"]) / slugify(args.title)
    case = create_case(base, args.title, args.question or "", args.lang)
    print(f"Кейс створено: {case}\n\nДалі:\n  cd {case}\n  social chat            # аналітик: {cfg['analyst']['agent']} / {cfg['analyst']['model']}\n"
          f"  social chat --agent codex --model <модель>   # або інший агент\n")


def cmd_chat(args):
    cfg = load_config()
    case = _case(args)
    name, model = pick(cfg, "analyst", args.agent, args.model)
    b = agents.get(name)
    if not b.installed():
        raise SystemExit(f"{b.title} не встановлено. Перевір: social backends")
    write_agent_configs(case)
    if name == "agy" and not b.mcp_registered():
        print("Antigravity ще не знає інструментів Social. Зареєструвати зараз (одноразово)? [Y/n] ", end="", flush=True)
        if input().strip().lower() in ("", "y", "yes", "т", "так"):
            subprocess.run(b.setup_cmd(), check=True)
    log(case, f"сеанс чату: {name} / {model or 'модель за замовчуванням'}")
    argv = b.interactive(case, model)
    if args.prompt:
        argv.append(args.prompt)
    os.chdir(case)
    os.environ["SOCIAL_CASE"] = str(case)
    os.execvp(argv[0], argv)


def _stamp():
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def cmd_ask(args):
    cfg = load_config()
    case = _case(args)
    name, model = pick(cfg, "analyst", args.agent, args.model)
    write_agent_configs(case)
    out = case / "wiki/sessions" / f"{_stamp()}-{name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    text = agents.run_headless(name, case, model, args.prompt, out)
    log(case, f"запит до {name}/{model or 'default'}: «{args.prompt[:60]}» → {out.relative_to(case)}")
    print(text)


def cmd_review(args):
    cfg = load_config()
    case = _case(args)
    name, model = pick(cfg, "reviewer", args.agent, args.model)
    write_agent_configs(case)
    out = case / "wiki/reviews" / f"{_stamp()}-{name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Червона команда: {agents.get(name).title} / {model or 'модель за замовчуванням'} …", file=sys.stderr)
    text = agents.run_headless(name, case, model, REVIEW_PROMPT, out, readonly=True)
    log(case, f"рецензія червоної команди: {name}/{model or 'default'} → {out.relative_to(case)}")
    print(text)


def cmd_backends(args):
    for b in agents.BACKENDS.values():
        mark = "✓" if b.installed() else "—"
        print(f"{mark} {b.name:9s} {b.title}")
        if args.models and b.installed():
            ms = b.models()
            print("    моделі: " + (", ".join(ms) if ms else "будь-яка назва, яку приймає агент (-m/--model)"))
    cfg = load_config()
    print(f"\nАналітик за замовчуванням: {cfg['analyst']['agent']} / {cfg['analyst']['model']}")
    r = pick(cfg, "reviewer", None, None)
    print(f"Червона команда: {r[0]} / {r[1] or 'модель агента за замовчуванням'}")


def cmd_config(args):
    cfg = load_config()
    if args.agent:
        agents.get(args.agent)
        cfg[args.role]["agent"] = args.agent
        cfg[args.role]["model"] = args.model
    elif args.model:
        cfg[args.role]["model"] = args.model
    if args.cases_dir:
        cfg["cases_dir"] = str(Path(args.cases_dir).expanduser().resolve())
    p = save_config(cfg)
    print(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False) + f"→ {p}")


def cmd_setup(args):
    b = agents.get(args.agent)
    if args.agent != "agy":
        print(f"{b.title} отримує інструменти Social автоматично під час `social chat`; окреме налаштування не потрібне.")
        return
    if b.mcp_registered():
        print("Antigravity вже знає інструменти Social.")
        return
    subprocess.run(b.setup_cmd(), check=True)
    print("Готово: Social зареєстровано в Antigravity (кейс визначається за поточною текою).")


def cmd_status(args):
    st = core.case_status(_case(args))
    print(yaml.safe_dump(st, allow_unicode=True, sort_keys=False))


def cmd_jobs(args):
    for j in jobs.status(_case(args), args.id, tail=args.tail):
        print(f"{j['id']}: {j['state']} (exit {j['exit_code']}) — {j['log']}")
        for line in j["log_tail"]:
            print("   ", line)


def cmd_doctor(args):
    ok = lambda b: "✓" if b else "—"  # noqa: E731
    print(f"social {__version__} · Python {sys.version.split()[0]} · {sys.executable}")
    for b in agents.BACKENDS.values():
        print(f"{ok(b.installed())} агент {b.name}")
    for tool, why in [("git", "фіксації й історія кейсу"), ("tectonic", "PDF з LaTeX"), ("pandoc", "PDF з Markdown")]:
        print(f"{ok(shutil.which(tool))} {tool} — {why}")
    try:
        import yt_dlp  # noqa: F401
        print("✓ yt-dlp — YouTube")
    except ImportError:
        print("— yt-dlp — YouTube (pip install 'social-framework[youtube]')")
    print(f"{ok(CONFIG_DIR.exists())} {CONFIG_DIR} — ключі, сесії й налаштування (не в кейсі)")


def cmd_web(args):
    from .web.server import serve
    serve(args.port, not args.no_browser, Path(args.case).resolve() if args.case else None)


def cmd_mcp(args):
    from .mcp_server import main as mcp_main
    mcp_main(args.case, args.readonly)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="social", description="Social: аналіз інформаційного простору з агентом на вибір.")
    ap.add_argument("--version", action="version", version=f"social {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("new", help="створити кейс")
    p.add_argument("title")
    p.add_argument("--question", default="")
    p.add_argument("--dir")
    p.add_argument("--lang", default="uk")
    p.set_defaults(fn=cmd_new)

    for name, fn, hlp in [("chat", cmd_chat, "розмова з агентом-аналітиком"), ("ask", cmd_ask, "один запит без інтерфейсу"),
                          ("review", cmd_review, "червона команда: інший агент оскаржує висновки")]:
        p = sub.add_parser(name, help=hlp)
        if name == "ask":
            p.add_argument("prompt")
        elif name == "chat":
            p.add_argument("prompt", nargs="?", default=None, help="перше повідомлення (необов'язково)")
        p.add_argument("--agent", choices=list(agents.BACKENDS))
        p.add_argument("--model")
        p.add_argument("--case")
        p.set_defaults(fn=fn)

    p = sub.add_parser("backends", help="встановлені агенти й моделі")
    p.add_argument("--models", action="store_true", help="запитати в кожного агента перелік моделей")
    p.set_defaults(fn=cmd_backends)

    p = sub.add_parser("config", help="агент і модель за замовчуванням")
    p.add_argument("--role", choices=["analyst", "reviewer"], default="analyst")
    p.add_argument("--agent", choices=list(agents.BACKENDS))
    p.add_argument("--model")
    p.add_argument("--cases-dir")
    p.set_defaults(fn=cmd_config)

    p = sub.add_parser("setup", help="одноразове налаштування агента")
    p.add_argument("agent", choices=list(agents.BACKENDS))
    p.set_defaults(fn=cmd_setup)

    for name, fn in [("status", cmd_status), ("jobs", cmd_jobs), ("mcp", cmd_mcp)]:
        p = sub.add_parser(name)
        p.add_argument("--case")
        if name == "mcp":
            p.add_argument("--readonly", action="store_true", help="лише інструменти читання (для рецензента)")
        if name == "jobs":
            p.add_argument("--id")
            p.add_argument("--tail", type=int, default=5)
        p.set_defaults(fn=fn)
    sub.add_parser("doctor", help="перевірити середовище").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("web", help="веб-інтерфейс у браузері")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--case", help="додатково показати кейс поза текою кейсів")
    p.set_defaults(fn=cmd_web)

    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
