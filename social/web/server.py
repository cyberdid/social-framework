"""Local web app for Social: cases, chat with any agent, fact-checks, judgments, scenarios, data, report, log.

Binds to 127.0.0.1 only. Every API call needs the per-run token (header X-Social-Token or ?t= for files),
and the Host header must be the local address, which blocks other web pages and DNS rebinding.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import webbrowser
from importlib import resources
from pathlib import Path

import markdown as md
import uvicorn
import yaml
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__, agents, jobs
from ..case import CASE_FILE, CONFIG_DIR, create_case, read_jsonl, slugify
from ..tools import core
from . import chat
from .sanitize import sanitize

STATIC = Path(str(resources.files("social") / "web" / "static"))
MD_EXT = ["tables", "fenced_code", "sane_lists"]


LIST_AFTER_TEXT = re.compile(r"(?m)^((?![ \t]*(?:[-*+]|\d+[.)])[ \t]).*\S.*)\n([ \t]*(?:[-*+]|\d+[.)])[ \t])")


def render_md(text: str) -> str:
    # agents often start a list right under a text line; Markdown needs a blank line there
    text = LIST_AFTER_TEXT.sub(r"\1\n\n\2", text or "")
    return sanitize(md.markdown(text, extensions=MD_EXT))


CSP_PAGE = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; frame-src 'self'; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
CSP_FILE = "sandbox; default-src 'self' 'unsafe-inline' data:; script-src 'none'"


class Guard(BaseHTTPMiddleware):
    def __init__(self, app, token: str, hosts: set[str]):
        super().__init__(app)
        self.token, self.hosts = token, hosts

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("host", "") not in self.hosts:
            return PlainTextResponse("forbidden host", 403)
        if request.url.path.startswith("/api/"):
            tok = request.headers.get("x-social-token") or request.query_params.get("t")
            if not tok or not secrets.compare_digest(tok, self.token):
                return JSONResponse({"error": "unauthorized"}, 401)
        resp = await call_next(request)
        if request.url.path.endswith("/file"):
            resp.headers["content-security-policy"] = CSP_FILE
        else:
            resp.headers.setdefault("content-security-policy", CSP_PAGE)
        resp.headers["x-content-type-options"] = "nosniff"
        resp.headers["referrer-policy"] = "no-referrer"
        return resp


def make_app(token: str, port: int, cases_dir: Path, extra_cases: list[Path] | None = None) -> Starlette:
    from ..cli import load_config, pick, save_config

    extra = [p.resolve() for p in (extra_cases or [])]

    def all_cases() -> dict[str, Path]:
        found: dict[str, Path] = {}
        if cases_dir.exists():
            for d in sorted(cases_dir.iterdir()):
                if (d / CASE_FILE).exists():
                    found[d.name] = d
        for d in extra:
            if (d / CASE_FILE).exists():
                found.setdefault(d.name, d)
        return found

    def case_of(request: Request) -> Path:
        c = all_cases().get(request.path_params["slug"])
        if not c:
            raise FileNotFoundError("case")
        return c

    def err(e: Exception, code: int = 400):
        return JSONResponse({"error": str(e)}, code)

    async def index(request: Request):
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))

    async def meta(request: Request):
        cfg = load_config()
        return JSONResponse({"version": __version__, "config": cfg, "reviewer": pick(cfg, "reviewer", None, None),
                             "agents": [{"name": b.name, "title": b.title, "installed": b.installed(),
                                         "known_models": b.known_models} for b in agents.BACKENDS.values()]})

    async def models(request: Request):
        b = agents.get(request.path_params["agent"])
        return JSONResponse({"models": b.models() if b.installed() else []})

    async def config_put(request: Request):
        body = await request.json()
        cfg = load_config()
        for role in ("analyst", "reviewer"):
            if role in body:
                cfg[role] = {"agent": body[role].get("agent"), "model": body[role].get("model") or None}
        save_config(cfg)
        return JSONResponse(cfg)

    def summary(slug: str, c: Path) -> dict:
        cy = yaml.safe_load((c / CASE_FILE).read_text(encoding="utf-8")) or {}
        logp = c / "log.md"
        return {"slug": slug, "title": cy.get("title") or slug, "question": cy.get("question") or "",
                "factchecks": len(read_jsonl(c / "wiki/factchecks.jsonl")),
                "judgments": len(read_jsonl(c / "wiki/judgments.jsonl")),
                "scenarios": len(list((c / "wiki/forecasts").glob("model-*.json"))),
                "chats": len(list((c / "wiki/chats").glob("*.jsonl"))),
                "updated": logp.stat().st_mtime if logp.exists() else (c / CASE_FILE).stat().st_mtime}

    async def cases(request: Request):
        rows = [summary(s, c) for s, c in all_cases().items()]
        return JSONResponse(sorted(rows, key=lambda r: -r["updated"]))

    async def case_new(request: Request):
        body = await request.json()
        title = (body.get("title") or "").strip()
        if not title:
            return err(ValueError("потрібна назва"))
        base = cases_dir / slugify(title)
        if base.exists():
            return err(FileExistsError("кейс з такою назвою вже є"))
        c = create_case(base, title, body.get("question") or "", body.get("language") or "uk")
        return JSONResponse(summary(c.name, c))

    async def status(request: Request):
        try:
            return JSONResponse(core.case_status(case_of(request)), headers={"cache-control": "no-store"})
        except FileNotFoundError as e:
            return err(e, 404)

    async def records(request: Request):
        try:
            return JSONResponse(core.list_records(case_of(request), request.path_params["kind"]))
        except (FileNotFoundError, ValueError) as e:
            return err(e, 404)

    async def forecast(request: Request):
        c = case_of(request)
        if not list((c / "wiki/forecasts").glob("model-*.json")):
            return JSONResponse(None)
        st = core.forecast_state(c)
        model = json.loads(sorted((c / "wiki/forecasts").glob("model-*.json"))[-1].read_text(encoding="utf-8"))
        return JSONResponse({**st, "model": model}, headers={"cache-control": "no-store"})

    async def case_log(request: Request):
        c = case_of(request)
        p = c / "log.md"
        return JSONResponse({"html": render_md(p.read_text(encoding="utf-8") if p.exists() else "")})

    async def case_jobs(request: Request):
        return JSONResponse(jobs.status(case_of(request), tail=8))

    async def case_files(request: Request):
        c = case_of(request)
        sub = (c / request.query_params.get("dir", "report")).resolve()
        if c.resolve() not in [sub, *sub.parents] or not sub.exists():
            return JSONResponse([])
        out = []
        for p in sorted(sub.rglob("*")):
            if p.is_file() and ".git" not in p.parts and "pages" not in p.parts:
                out.append({"path": str(p.relative_to(c)), "size": p.stat().st_size, "mtime": p.stat().st_mtime})
        return JSONResponse(out[:500])

    async def case_file(request: Request):
        c = case_of(request).resolve()
        p = (c / request.query_params.get("path", "")).resolve()
        if c not in p.parents or not p.is_file() or ".git" in p.parts:
            return PlainTextResponse("not found", 404)
        return FileResponse(p, media_type=mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                            headers={"cache-control": "no-store"})

    async def report_build(request: Request):
        try:
            body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
            return JSONResponse(core.report_build(case_of(request), body.get("source") or "report/report.md"))
        except Exception as e:  # noqa: BLE001 - shown to the user
            return err(e)

    async def chats_list(request: Request):
        return JSONResponse(chat.list_chats(case_of(request)))

    async def chats_new(request: Request):
        body = await request.json()
        role = body.get("role") or "analyst"
        a, m = pick(load_config(), role, body.get("agent") or None, body.get("model") or None)
        try:
            return JSONResponse(chat.create_chat(case_of(request), a, m, role))
        except SystemExit as e:
            return err(Exception(str(e)))

    async def chat_get(request: Request):
        try:
            rows = chat.load_chat(case_of(request), request.path_params["cid"])
        except FileNotFoundError as e:
            return err(e, 404)
        for r in rows:
            if r.get("role") == "assistant":
                r["html"] = render_md(r.get("text", ""))
        return JSONResponse(rows)

    async def chat_send(request: Request):
        c, cid = case_of(request), request.path_params["cid"]
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            return err(ValueError("порожнє повідомлення"))
        if body.get("review"):
            from ..cli import REVIEW_PROMPT
            text = REVIEW_PROMPT

        async def stream():
            async for e in chat.send(c, cid, text):
                if e["t"] == "done":
                    e["html"] = render_md(e.get("text", ""))
                yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"cache-control": "no-store", "x-accel-buffering": "no"})

    async def chat_stop(request: Request):
        return JSONResponse({"stopped": chat.stop(request.path_params["cid"])})

    async def guard_404(request: Request, exc: Exception):
        return JSONResponse({"error": "not found"}, 404)

    routes = [
        Route("/", index),
        Route("/api/meta", meta),
        Route("/api/config", config_put, methods=["PUT"]),
        Route("/api/agents/{agent}/models", models),
        Route("/api/cases", cases),
        Route("/api/cases", case_new, methods=["POST"]),
        Route("/api/cases/{slug}/status", status),
        Route("/api/cases/{slug}/records/{kind}", records),
        Route("/api/cases/{slug}/forecast", forecast),
        Route("/api/cases/{slug}/log", case_log),
        Route("/api/cases/{slug}/jobs", case_jobs),
        Route("/api/cases/{slug}/files", case_files),
        Route("/api/cases/{slug}/file", case_file),
        Route("/api/cases/{slug}/report", report_build, methods=["POST"]),
        Route("/api/cases/{slug}/chats", chats_list),
        Route("/api/cases/{slug}/chats", chats_new, methods=["POST"]),
        Route("/api/cases/{slug}/chats/{cid}", chat_get),
        Route("/api/cases/{slug}/chats/{cid}/send", chat_send, methods=["POST"]),
        Route("/api/cases/{slug}/chats/{cid}/stop", chat_stop, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ]
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    return Starlette(routes=routes, middleware=[Middleware(Guard, token=token, hosts=hosts)],
                     exception_handlers={FileNotFoundError: guard_404})


def serve(port: int = 8765, open_browser: bool = True, case: Path | None = None) -> None:
    from ..cli import load_config

    token = os.environ.get("SOCIAL_WEB_TOKEN") or secrets.token_urlsafe(24)
    cases_dir = Path(load_config()["cases_dir"]).expanduser()
    cases_dir.mkdir(parents=True, exist_ok=True)
    app = make_app(token, port, cases_dir, [case] if case else None)
    url = f"http://127.0.0.1:{port}/#t={token}"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Social {__version__} — веб-інтерфейс: {url}\n(посилання містить одноразовий ключ; не діліться ним)", flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
