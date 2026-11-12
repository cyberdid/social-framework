"""Report figures from a case's processed outputs (no case data inside). Each function writes report/fig/<name>.png and
.pdf and returns the PNG path. Colours: validated categorical slots for groups/scenarios, fixed status colours for
verdicts; every series is also labelled, so colour never carries meaning alone.

Requires the 'figures' extra (matplotlib, networkx).
"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
GROUP_ORDER = ["D", "B", "A", "E", "C", "U"]
VERDICT_COLORS = {"правда": "#0ca30c", "ймовірно правда": "#5fb85f", "частково": "#fab219", "перекручено": "#ec835a",
                  "не підтверджено": "#8a949c", "припущення": "#a6adb4", "неправда": "#d03b3b", "фейк": "#8f2525"}
INK, MUTED = "#1f2933", "#6b7386"


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False, "axes.edgecolor": "#c9ced6",
                         "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED, "pdf.fonttype": 42,
                         "font.family": ["DejaVu Sans"]})
    return plt


def _save(case: Path, fig, name: str) -> str:
    d = case / "report/fig"
    d.mkdir(parents=True, exist_ok=True)
    fig.savefig(d / f"{name}.png", dpi=180, bbox_inches="tight")
    fig.savefig(d / f"{name}.pdf", bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return str((d / f"{name}.png").relative_to(case))


def _gname(case: Path) -> dict:
    try:
        from .analysis.lexicon import load
        return load(case).group_names
    except FileNotFoundError:
        return {}


def _groups_present(gs) -> list[str]:
    return [g for g in GROUP_ORDER if g in gs] + sorted(g for g in gs if g not in GROUP_ORDER)


def volume(case: Path) -> str:
    plt = _plt()
    rows = list(csv.DictReader((case / "data/processed/analysis/volume/daily_by_group.csv").open(encoding="utf-8")))
    days = sorted({r["day"] for r in rows})
    gs = _groups_present({r["group"] for r in rows})
    names = _gname(case)
    val = defaultdict(int)
    for r in rows:
        val[(r["day"], r["group"])] += int(r["posts"])
    fig, ax = plt.subplots(figsize=(12, 4.6))
    x = [date.fromisoformat(d) for d in days]
    bottom = [0] * len(days)
    for i, g in enumerate(gs):
        h = [val[(d, g)] for d in days]
        ax.bar(x, h, bottom=bottom, color=CAT[i % len(CAT)], width=0.85, label=names.get(g, g), edgecolor="white", linewidth=0.5)
        bottom = [a + b for a, b in zip(bottom, h)]
    ax.set_ylabel("дописів на день")
    ax.legend(frameon=False, ncol=min(len(gs), 4), loc="upper left")
    fig.autofmt_xdate()
    return _save(case, fig, "volume")


def frames(case: Path) -> str:
    plt = _plt()
    rows = [r for r in csv.DictReader((case / "data/processed/analysis/frames/frames_by_group_period.csv").open(encoding="utf-8"))
            if r["period"] == "усі" and r["group"] != "усі"]
    meta = json.loads((case / "data/processed/analysis/frames/frames.json").read_text(encoding="utf-8"))["frames"]
    keys = list(meta)
    names = _gname(case)
    gs = _groups_present({r["group"] for r in rows})
    by = {r["group"]: r for r in rows}
    fig, ax = plt.subplots(figsize=(12, 0.6 * len(keys) * max(1, len(gs) / 2) + 2))
    h = 0.8 / max(len(gs), 1)
    for i, g in enumerate(gs):
        vals = [100 * float(by[g].get(f"{k}_share") or 0) for k in keys]
        ys = [j + i * h for j in range(len(keys))]
        ax.barh(ys, vals, height=h * 0.92, color=CAT[i % len(CAT)], label=f"{names.get(g, g)} (n={by[g]['posts']})")
        for y, v in zip(ys, vals):
            if v >= 1:
                ax.text(v + 0.4, y, f"{v:.0f} %", va="center", fontsize=9, color=INK)
    ax.set_yticks([j + h * (len(gs) - 1) / 2 for j in range(len(keys))])
    ax.set_yticklabels([meta[k] for k in keys])
    ax.invert_yaxis()
    ax.set_xlabel("частка релевантних дописів групи, %")
    ax.legend(frameon=False, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=min(len(gs), 3))
    return _save(case, fig, "frames")


def mood(case: Path, dim: str = "group") -> str:
    plt = _plt()
    m = json.loads((case / "data/processed/analysis/mood/mood.json").read_text(encoding="utf-8"))[dim]
    emos = ["гнів", "глузування", "схвалення", "подив", "сум", "несхвалення", "інше"]
    emo_col = {"гнів": "#d03b3b", "глузування": "#eda100", "схвалення": "#1baf7a", "подив": "#6250d6", "сум": "#2a78d6",
               "несхвалення": "#8f2525", "інше": "#c9ced6"}
    names = _gname(case) if dim == "group" else {}
    cats = [k for k in (_groups_present(m) if dim == "group" else m) if m[k].get("_total")]
    fig, ax = plt.subplots(figsize=(12, 0.55 * len(cats) + 1.8))
    for i, c in enumerate(cats):
        left = 0.0
        for e in emos:
            v = m[c].get(e, 0)
            if v:
                ax.barh(i, v, left=left, color=emo_col[e], edgecolor="white", linewidth=1)
                if v >= 0.07:
                    ax.text(left + v / 2, i, f"{v * 100:.0f}%", ha="center", va="center", fontsize=9, color="white", fontweight="bold")
                left += v
    ax.set_yticks(range(len(cats)))
    ax.set_yticklabels([f"{names.get(c, c)} ({m[c]['_total']:,})".replace(",", " ") for c in cats])
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.xaxis.set_visible(False)
    ax.spines[["left", "bottom"]].set_visible(False)
    import matplotlib.patches as mp
    ax.legend(handles=[mp.Patch(color=emo_col[e], label=e) for e in emos], frameon=False, ncol=7, loc="lower left",
              bbox_to_anchor=(0, 1.0), fontsize=9.5)
    return _save(case, fig, f"mood_{dim}")


def verdicts(case: Path) -> str:
    plt = _plt()
    fc = [json.loads(x) for x in (case / "wiki/factchecks.jsonl").open(encoding="utf-8")]
    c = Counter(f["verdict"] for f in fc)
    order = [v for v in VERDICT_COLORS if c.get(v)]
    fig, ax = plt.subplots(figsize=(12, 1.9))
    left = 0
    for v in order:
        ax.barh(0, c[v], left=left, color=VERDICT_COLORS[v], edgecolor="white", linewidth=2)
        ax.text(left + c[v] / 2, 0, str(c[v]), ha="center", va="center", color="white", fontweight="bold")
        left += c[v]
    ax.set_xlim(0, left)
    ax.axis("off")
    import matplotlib.patches as mp
    ax.legend(handles=[mp.Patch(color=VERDICT_COLORS[v], label=f"{v} · {c[v]}") for v in order], frameon=False,
              ncol=min(len(order), 4), loc="upper left", bbox_to_anchor=(0, 0))
    ax.set_title(f"Перевірено тверджень: {len(fc)}", loc="left", color=INK)
    return _save(case, fig, "verdicts")


def judgments(case: Path) -> str:
    plt = _plt()
    js = sorted([json.loads(x) for x in (case / "wiki/judgments.jsonl").open(encoding="utf-8")], key=lambda j: j["probability"])
    import textwrap
    fig, ax = plt.subplots(figsize=(12, 0.62 * len(js) + 1.2))
    ax.barh(range(len(js)), [100 * j["probability"] for j in js], color="#3987e5", height=0.6)
    for i, j in enumerate(js):
        ax.text(100 * j["probability"] + 1, i, f"{100 * j['probability']:.0f} % · {j['level']}", va="center", fontsize=10, color=INK)
    ax.set_yticks(range(len(js)))
    ax.set_yticklabels([textwrap.fill(j["statement"], 60) for j in js], fontsize=10)
    ax.set_xlim(0, 112)
    ax.axvline(50, color="#9aa3aa", lw=0.8, ls="--")
    ax.set_xlabel("моя ймовірність, % · рівень доказовості")
    return _save(case, fig, "judgments")


def copy_network(case: Path, min_jaccard: float = 0.8, labels: int = 25) -> str:
    plt = _plt()
    import networkx as nx
    rows = [r for r in csv.DictReader((case / "data/processed/analysis/coordination/edges.csv").open(encoding="utf-8"))
            if float(r["jaccard"]) >= min_jaccard]
    g, grp = nx.Graph(), {}
    for r in rows:
        a, b = r["earlier_channel"], r["later_channel"]
        grp[a], grp[b] = r["earlier_group"], r["later_group"]
        w = g.get_edge_data(a, b, {"weight": 0})["weight"] + 1
        g.add_edge(a, b, weight=w)
    names = _gname(case)
    gs = _groups_present(set(grp.values()))
    col = {x: CAT[i % len(CAT)] for i, x in enumerate(gs)}
    fig, ax = plt.subplots(figsize=(13, 10))
    ax.axis("off")
    if g.number_of_nodes():
        comp = max(nx.connected_components(g), key=len)
        h = g.subgraph(comp)
        pos = nx.kamada_kawai_layout(h) if h.number_of_nodes() < 400 else nx.spring_layout(h, seed=1)
        deg = dict(h.degree(weight="weight"))
        for a, b, d in h.edges(data=True):
            ax.plot([pos[a][0], pos[b][0]], [pos[a][1], pos[b][1]], color="#c3ccd3", lw=0.4 + 0.3 * min(d["weight"], 8), zorder=1)
        for n in h:
            ax.scatter(*pos[n], s=min(40 + 9 * deg[n], 900), color=col[grp[n]], edgecolor="white", lw=0.8, zorder=2)
        texts = [ax.text(pos[n][0], pos[n][1], n, fontsize=9, zorder=3,
                         bbox=dict(boxstyle="round,pad=0.12", fc="white", ec="none", alpha=0.8))
                 for n in sorted(h, key=lambda n: -deg[n])[:labels]]
        try:
            from adjustText import adjust_text
            adjust_text(texts, ax=ax, x=[p[0] for p in pos.values()], y=[p[1] for p in pos.values()],
                        expand=(1.3, 1.6), arrowprops=dict(arrowstyle="-", color="#777", lw=0.5))
        except ImportError:
            pass
        ax.set_title(f"Найбільше «гроно» копіювання: {len(comp)} каналів (збіг тексту ≥ {min_jaccard:.0%})", loc="left", color=INK)
    for x in gs:
        ax.scatter([], [], color=col[x], s=70, label=names.get(x, x))
    ax.legend(frameon=False, loc="lower left")
    return _save(case, fig, "copy_network")


def claims_timeline(case: Path) -> str:
    """When each claim first appeared in each channel group, and when the official document came."""
    plt = _plt()
    from datetime import datetime
    data = json.loads((case / "data/processed/analysis/claims/claims.json").read_text(encoding="utf-8"))["claims"]
    names = _gname(case)
    items = [(k, v) for k, v in data.items() if v.get("posts")]
    if not items:
        raise ValueError("жодне твердження з лексикону не знайдено в корпусі")
    gs = _groups_present({g for _, v in items for g in v["first_by_group"]})
    col = {x: CAT[i % len(CAT)] for i, x in enumerate(gs)}
    fig, ax = plt.subplots(figsize=(12, 0.6 * len(items) + 1.8))
    for i, (k, v) in enumerate(items):
        for g, f in v["first_by_group"].items():
            ax.scatter(datetime.fromisoformat(f["t_local"]), i, color=col[g], s=60, zorder=3)
        if v.get("official_at"):
            ax.scatter(datetime.fromisoformat(str(v["official_at"])), i, marker="|", s=400, color=INK, zorder=4)
    ax.set_yticks(range(len(items)))
    ax.set_yticklabels([f"{v['label']} ({v['posts']})" for _, v in items], fontsize=10)
    ax.invert_yaxis()
    for x in gs:
        ax.scatter([], [], color=col[x], s=60, label=f"перша поява: {names.get(x, x)}")
    ax.scatter([], [], marker="|", s=200, color=INK, label="офіційний документ")
    ax.legend(frameon=False, loc="lower left", bbox_to_anchor=(0, 1.0), ncol=3, fontsize=9.5)
    fig.autofmt_xdate()
    return _save(case, fig, "claims_timeline")


def scenarios(case: Path) -> str:
    """Scenario probabilities now and what each open indicator would do (from data/processed/forecast/state.json)."""
    plt = _plt()
    st = json.loads((case / "data/processed/forecast/state.json").read_text(encoding="utf-8"))
    ids = list(st["scenarios"])
    col = {k: CAT[i % len(CAT)] for i, k in enumerate(ids)}
    rows = [("ЗАРАЗ", st["scenarios"])] + [(f"{w['id']}: так", w["yes"]) for w in st["whatif"]] + \
           [(f"{w['id']}: ні", w["no"]) for w in st["whatif"]]
    rows = [rows[0]] + [r for w in st["whatif"] for r in rows[1:] if r[0].startswith(w["id"] + ":")]
    fig, ax = plt.subplots(figsize=(11, 0.42 * len(rows) + 1.5))
    for i, (lab, p) in enumerate(rows):
        left = 0
        for k in ids:
            ax.barh(i, p[k], left=left, color=col[k], edgecolor="white", linewidth=1.2, height=0.75 if i else 0.9)
            if p[k] >= 0.08:
                ax.text(left + p[k] / 2, i, f"{p[k] * 100:.0f}%", ha="center", va="center", color="white", fontsize=9, fontweight="bold")
            left += p[k]
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([r[0] for r in rows], fontsize=9.5)
    ax.invert_yaxis()
    ax.set_xlim(0, 1)
    ax.xaxis.set_visible(False)
    ax.spines[["left", "bottom"]].set_visible(False)
    import matplotlib.patches as mp
    ax.legend(handles=[mp.Patch(color=col[k], label=f"{k}. {st['scenario_names'][k]}") for k in ids], frameon=False,
              ncol=len(ids), loc="lower left", bbox_to_anchor=(0, 1.0))
    return _save(case, fig, "scenarios")


ALL = {"volume": volume, "frames": frames, "mood": mood, "verdicts": verdicts, "judgments": judgments,
       "copy_network": copy_network, "claims_timeline": claims_timeline, "scenarios": scenarios}


def make(case: Path, names: list[str] | None = None) -> dict:
    out = {}
    for n in names or list(ALL):
        try:
            out[n] = ALL[n](case)
        except FileNotFoundError as e:
            out[n] = f"пропущено: немає даних ({Path(e.filename or '').name or e})"
        except (KeyError, ValueError, StopIteration) as e:
            out[n] = f"пропущено: {type(e).__name__} {e}"
    return out
