"""Background jobs: long collectors run detached (nice 19), survive the chat, and report status from disk."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from .case import append_jsonl, log, now_utc, read_jsonl

JOBS = ".social/jobs.jsonl"


def start(case: Path, name: str, argv: list[str]) -> dict:
    jid = f"{name}-{uuid.uuid4().hex[:6]}"
    logs = case / "logs/jobs"
    logs.mkdir(parents=True, exist_ok=True)
    log_p, exit_p = logs / f"{jid}.log", logs / f"{jid}.exit"
    cmd = [sys.executable, "-m", "social.jobrunner", str(exit_p), "--", *argv]
    if sys.platform != "win32" and Path("/usr/bin/nice").exists():
        cmd = ["nice", "-n", "19", *cmd]
    with log_p.open("ab") as out:
        proc = subprocess.Popen(cmd, cwd=case, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                start_new_session=True)
    rec = append_jsonl(case / JOBS, {"id": jid, "name": name, "argv": argv, "pid": proc.pid, "started": now_utc(),
                                     "log": str(log_p.relative_to(case))})
    log(case, f"фоновий збір {jid}: {' '.join(argv[:6])}…")
    return rec


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:  # a finished child that nobody reaped is a zombie: treat as finished
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(") ")[-1][0] != "Z"
    except OSError:
        return True


def status(case: Path, job_id: str | None = None, tail: int = 15) -> list[dict]:
    out = []
    for j in read_jsonl(case / JOBS):
        if job_id and j["id"] != job_id:
            continue
        exit_p = case / "logs/jobs" / f"{j['id']}.exit"
        code = exit_p.read_text().strip() if exit_p.exists() else None
        state = "finished" if code is not None else ("running" if _alive(j["pid"]) else "stopped (no exit code: killed or power cut)")
        lines = (case / j["log"]).read_text(errors="replace").splitlines()[-tail:] if (case / j["log"]).exists() else []
        out.append({**j, "state": state, "exit_code": int(code) if code not in (None, "") else None, "log_tail": lines})
    return out


def jobrunner_main(argv: list[str]) -> int:
    exit_file, cmd = argv[0], argv[argv.index("--") + 1:]
    rc = subprocess.call(cmd)
    Path(exit_file).write_text(str(rc))
    return rc


if __name__ == "__main__":
    sys.exit(jobrunner_main(sys.argv[1:]))
