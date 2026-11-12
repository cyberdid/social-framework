"""Scenario forecast engine: mutually exclusive scenarios, indicators, Bayesian updates, Brier scoring.

The model (scenarios, indicators with P(indicator | scenario), standalone forecasts) is a frozen precommit
JSON. Everything that happens later goes into an append-only journal (JSONL, fsynced per record):

  observe  — an indicator occurred, or its window closed without it (status occurred / not_occurred)
  revise   — an explicit analyst revision of scenario or forecast probabilities, with a reason
  resolve  — a forecast (F*) or the scenario set (S) is resolved against its criterion

State is recomputed from model + journal every time, so the history of estimates is the journal itself.

Usage:
  python -m social.tools.forecast state   --model M.json --journal J.jsonl --out reports/x_forecast
  python -m social.tools.forecast observe --journal J.jsonl --id I2 --status occurred --date 2026-12-03 \
         --source URL --level A --note "..."
  python -m social.tools.forecast revise  --journal J.jsonl --target scenarios --p A=0.3,B=0.3,C=0.4 \
         --date 2026-12-03 --reason "..."
  python -m social.tools.forecast resolve --journal J.jsonl --id F2 --outcome 1 --date 2026-12-31 --source URL --level A
"""
from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

TEMPER = 0.5  # exponent for later observations within one correlated indicator group


def load(model_p: Path, journal_p: Path):
    model = json.loads(model_p.read_text())
    journal = [json.loads(x) for x in journal_p.open()] if journal_p.exists() else []
    journal.sort(key=lambda e: (e["date"], e.get("recorded_at", "")))
    return model, journal


def append(journal_p: Path, rec: dict):
    rec["recorded_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    journal_p.parent.mkdir(parents=True, exist_ok=True)
    with journal_p.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    print("recorded:", json.dumps(rec, ensure_ascii=False))


def bayes(p: dict, lik: dict, power: float = 1.0) -> dict:
    post = {k: p[k] * (lik[k] ** power) for k in p}
    z = sum(post.values())
    return {k: v / z for k, v in post.items()}


def likelihood(ind: dict, status: str) -> dict:
    return {k: (v if status == "occurred" else 1 - v) for k, v in ind["p_given"].items()}


def compute(model: dict, journal: list, today: date):
    inds = {i["id"]: i for i in model["indicators"]}
    p = {s["id"]: s["p"] for s in model["scenarios"]}
    fc = {f["id"]: {**f, "p_now": f["history"][-1]["p"], "status": "open"} for f in model["forecasts"]}
    groups_used: dict[str, int] = {}
    seen: set[str] = set()
    hist = [{"date": model["fixed_at"], "event": "старт (попередня фіксація)", **p}]
    istat = {i: {"status": "pending"} for i in inds}
    scen_res = None
    for e in journal:
        t = e["type"]
        if t == "observe":
            if e["id"] in seen:
                print(f"warning: second observation of {e['id']} ignored")
                continue
            seen.add(e["id"])
            ind = inds[e["id"]]
            g = ind["group"]
            power = 1.0 if groups_used.get(g, 0) == 0 else TEMPER
            groups_used[g] = groups_used.get(g, 0) + 1
            p = bayes(p, likelihood(ind, e["status"]), power)
            istat[e["id"]] = {"status": e["status"], "date": e["date"], "source": e.get("source", ""),
                              "level": e.get("level", ""), "power": power}
            word = "сталося" if e["status"] == "occurred" else "не сталося"
            hist.append({"date": e["date"], "event": f"{e['id']} {word}", **p})
        elif t == "revise":
            if e["target"] == "scenarios":
                p = {k: float(v) for k, v in e["p"].items()}
                hist.append({"date": e["date"], "event": f"перегляд: {e['reason']}", **p})
            else:
                fc[e["target"]]["p_now"] = float(e["p"])
                fc[e["target"]]["history"] = fc[e["target"]]["history"] + [{"date": e["date"], "p": float(e["p"]), "note": e["reason"]}]
        elif t == "resolve":
            if e["id"] == "S":
                o = {k: 1.0 if k == e["outcome"] else 0.0 for k in p}
                scen_res = {"outcome": e["outcome"], "date": e["date"], "brier": sum((p[k] - o[k]) ** 2 for k in p), "p_at": dict(p)}
            else:
                f = fc[e["id"]]
                f.update(status="resolved", outcome=int(e["outcome"]), resolved=e["date"],
                         brier=(f["p_now"] - int(e["outcome"])) ** 2, source=e.get("source", ""))
    for i, ind in inds.items():
        if istat[i]["status"] == "pending" and date.fromisoformat(ind["window_end"]) < today:
            istat[i]["status"] = "overdue"
    for f in fc.values():
        if f["status"] == "open" and date.fromisoformat(f["resolve_by"]) < today:
            f["status"] = "overdue"
    # what-if from the current state, for indicators still open
    whatif = []
    for i, ind in inds.items():
        if istat[i]["status"] not in ("pending", "overdue"):
            continue
        power = 1.0 if groups_used.get(ind["group"], 0) == 0 else TEMPER
        whatif.append({"id": i, "name": ind["name"], "window_end": ind["window_end"],
                       "implied": sum(p[k] * ind["p_given"][k] for k in p),
                       "yes": bayes(p, likelihood(ind, "occurred"), power),
                       "no": bayes(p, likelihood(ind, "not_occurred"), power)})
    coher = []
    for w in whatif:
        lf = inds[w["id"]].get("linked_forecast")
        if lf:
            d = w["implied"] - fc[lf]["p_now"]
            coher.append({"indicator": w["id"], "forecast": lf, "implied": w["implied"], "stated": fc[lf]["p_now"],
                          "diff_pp": 100 * d, "flag": abs(d) > 0.10})
    briers = [f["brier"] for f in fc.values() if f["status"] == "resolved"]
    return {"as_of": today.isoformat(), "scenarios": p, "scenario_names": {s["id"]: s["name"] for s in model["scenarios"]},
            "history": hist, "indicators": istat, "whatif": whatif, "coherence": coher,
            "forecasts": list(fc.values()), "scenario_resolution": scen_res,
            "track_record": {"resolved": len(briers), "open": sum(f["status"] != "resolved" for f in fc.values()),
                             "mean_brier": (sum(briers) / len(briers)) if briers else None}}


def write_state(st: dict, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    (out / "state.json").write_text(json.dumps(st, ensure_ascii=False, indent=1))
    ks = list(st["scenarios"])
    with (out / "history.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "event", *ks])
        for h in st["history"]:
            w.writerow([h["date"], h["event"], *[round(h[k], 4) for k in ks]])
    with (out / "whatif.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "window_end", "implied", *[f"yes_{k}" for k in ks], *[f"no_{k}" for k in ks]])
        for r in st["whatif"]:
            w.writerow([r["id"], r["name"], r["window_end"], round(r["implied"], 4),
                        *[round(r["yes"][k], 4) for k in ks], *[round(r["no"][k], 4) for k in ks]])


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("state")
    s.add_argument("--model", type=Path, required=True)
    s.add_argument("--journal", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--today", default=date.today().isoformat())
    o = sub.add_parser("observe")
    o.add_argument("--journal", type=Path, required=True)
    o.add_argument("--id", required=True)
    o.add_argument("--status", choices=["occurred", "not_occurred"], required=True)
    o.add_argument("--date", required=True)
    o.add_argument("--source", default="")
    o.add_argument("--level", choices=list("ABCD"), required=True)
    o.add_argument("--note", default="")
    r = sub.add_parser("revise")
    r.add_argument("--journal", type=Path, required=True)
    r.add_argument("--target", required=True, help="'scenarios' or a forecast id")
    r.add_argument("--p", required=True, help="A=0.3,B=0.3,C=0.4 for scenarios; 0.4 for a forecast")
    r.add_argument("--date", required=True)
    r.add_argument("--reason", required=True)
    v = sub.add_parser("resolve")
    v.add_argument("--journal", type=Path, required=True)
    v.add_argument("--id", required=True, help="forecast id, or S for the scenario set")
    v.add_argument("--outcome", required=True, help="1/0 for a forecast; scenario id for S")
    v.add_argument("--date", required=True)
    v.add_argument("--source", default="")
    v.add_argument("--level", choices=list("ABCD"), required=True)
    v.add_argument("--note", default="")
    a = ap.parse_args()
    if a.cmd == "state":
        model, journal = load(a.model, a.journal)
        st = compute(model, journal, date.fromisoformat(a.today))
        write_state(st, a.out)
        print(json.dumps({"scenarios": {k: round(x, 3) for k, x in st["scenarios"].items()},
                          "coherence_flags": [c for c in st["coherence"] if c["flag"]],
                          "track_record": st["track_record"]}, ensure_ascii=False))
    elif a.cmd == "observe":
        append(a.journal, {"type": "observe", "id": a.id, "status": a.status, "date": a.date,
                           "source": a.source, "level": a.level, "note": a.note})
    elif a.cmd == "revise":
        p = dict(kv.split("=") for kv in a.p.split(",")) if a.target == "scenarios" else a.p
        if a.target == "scenarios" and abs(sum(map(float, p.values())) - 1) > 1e-6:
            raise SystemExit("scenario probabilities must sum to 1")
        append(a.journal, {"type": "revise", "target": a.target, "p": p, "date": a.date, "reason": a.reason})
    else:
        append(a.journal, {"type": "resolve", "id": a.id, "outcome": a.outcome, "date": a.date,
                           "source": a.source, "level": a.level, "note": a.note})


if __name__ == "__main__":
    main()
