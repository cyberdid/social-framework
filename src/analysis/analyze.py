"""Main analysis of the Borshchahivka information wave (Telegram, public web snapshot).

Inputs : data/processed/<snap>/posts_clean.jsonl, channels_features.csv, config/channel_groups.csv
Outputs: reports/<snap>/*.csv, *.png, results.json
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import pandas as pd
from datasketch import MinHash, MinHashLSH

sys.path.insert(0, str(Path(__file__).resolve().parent))
from narratives import CODEBOOK, COUNTER_DEBUNK, DEBUNK, MEDIC_CLAIM, tag  # noqa: E402
from prep import KYIV  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
GROUPS = {"A": "місцеві паблики", "B": "українські неофіційні", "C": "російські та проросійські",
          "D": "офіційні органи й редакційні медіа", "E": "іноземні та еміграційні", "U": "не класифіковано"}
COLORS = {"A": "#f28e2b", "B": "#2f6fdb", "C": "#c0392b", "D": "#27ae60", "E": "#bbbbbb", "U": "#7f7f7f"}
BIG = 100_000  # "large channel" threshold, subscribers


def kyiv(s):
    return datetime.fromisoformat(s).astimezone(KYIV)


def load(snap: str | None):
    procs = sorted((ROOT / "data/processed").iterdir())
    d = ROOT / "data/processed" / snap if snap else procs[-1]
    posts = [json.loads(l) for l in (d / "posts_clean.jsonl").open(encoding="utf-8")]
    ch = pd.read_csv(d / "channels_features.csv")
    gpath = ROOT / "config/channel_groups.csv"
    groups = pd.read_csv(gpath, comment="#") if gpath.exists() else pd.DataFrame(columns=["channel", "group"])
    gmap = {}
    apath = ROOT / "config/channel_groups_auto.csv"
    if apath.exists():
        auto = pd.read_csv(apath, comment="#")
        gmap.update(dict(zip(auto.channel.str.lower(), auto.group)))
    gmap.update(dict(zip(groups.channel.str.lower(), groups.group)))  # manual labels win
    subs = dict(zip(ch.channel, ch.subscribers))
    for p in posts:
        p["group"] = gmap.get(p["channel"], "U")
        p["subs"] = subs.get(p["channel"])
        p["dt"] = kyiv(p["date"])
    return d.name, posts, ch, gmap


def shingles(text, k=5):
    t = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", text.lower())).strip()
    return {t[i:i + k] for i in range(max(1, len(t) - k + 1))}


def minhash(text, k=5, perm=128):
    m = MinHash(num_perm=perm)
    for s in sorted(shingles(text, k)):
        m.update(s.encode())
    return m


def copy_clusters(rel, thr=0.6, min_len=80):
    """Near-duplicate clusters of post texts (5-char shingles, MinHash LSH, Jaccard>=thr)."""
    cand = [p for p in rel if len(p["text_clean"]) >= min_len]
    lsh = MinHashLSH(threshold=thr, num_perm=128)
    mh = {}
    exact = {}
    for i, p in enumerate(cand):
        mh[i] = minhash(p["text_clean"])
        exact[i] = shingles(p["text_clean"])
        lsh.insert(i, mh[i])
    parent = list(range(len(cand)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in mh:
        for j in lsh.query(mh[i]):
            if i == j or len(exact[i] & exact[j]) / max(1, len(exact[i] | exact[j])) < thr:
                continue
            a, b = find(i), find(j)
            if a != b:
                parent[a] = b
    cl = defaultdict(list)
    for i in mh:
        cl[find(i)].append(cand[i])
    return [sorted(v, key=lambda p: p["dt"]) for v in cl.values() if len({p["channel"] for p in v}) > 1]


def main(snap=None):
    name, posts, ch, gmap = load(snap)
    out = ROOT / "reports" / name
    out.mkdir(parents=True, exist_ok=True)
    rel = sorted([p for p in posts if p["relevant"]], key=lambda p: p["dt"])
    for p in rel:
        p["narr"] = tag(p["text_clean"])
        p["counter_debunk"] = bool(COUNTER_DEBUNK.search(p["text_clean"]))
        p["debunk"] = bool(DEBUNK.search(p["text_clean"])) and not p["counter_debunk"]
        p["medic_claim"] = bool(MEDIC_CLAIM.search(p["text_clean"])) and not p["debunk"]
    res = {"snapshot": name, "n_posts_window": len(posts), "n_relevant": len(rel),
           "n_channels": int(ch.shape[0]), "n_channels_relevant": len({p["channel"] for p in rel}),
           "views_relevant": sum(p["views"] or 0 for p in rel)}

    # --- relevant posts table
    pd.DataFrame([{k: p.get(k) for k in ["t_kyiv", "channel", "group", "subs", "views", "fwd_channel", "lang",
                                         "medic_claim", "debunk", "counter_debunk", "url"]} | {"narr": ",".join(p["narr"]),
                                                                              "text": p["text_clean"][:300]}
                  for p in rel]).to_csv(out / "relevant_posts.csv", index=False)

    # --- firsts
    def first(pred):
        f = next((p for p in rel if pred(p)), None)
        return None if f is None else {"t": f["t_kyiv"], "channel": f["channel"], "group": f["group"],
                                       "views": f["views"], "subs": f["subs"], "url": f["url"],
                                       "text": f["text_clean"][:200]}

    res["first_overall"] = first(lambda p: True)
    res["first_by_group"] = {g: first(lambda p, g=g: p["group"] == g) for g in GROUPS}
    res["first_ru_lang"] = first(lambda p: p["lang"] == "ru")
    res["first_big"] = first(lambda p: (p["subs"] or 0) >= BIG)
    res["first_big_40"] = first(lambda p: (p["subs"] or 0) >= BIG and re.search(r"\b40\b|сорок", p["text_clean"]))
    res["first_by_narrative"] = {k: first(lambda p, k=k: k in p["narr"]) for k in CODEBOOK}
    res["first_debunk"] = first(lambda p: p["debunk"])

    # --- KB4: lull of big channels 13:49-16:14
    t0, t1 = datetime(2026, 11, 2, 13, 50, tzinfo=KYIV), datetime(2026, 11, 2, 16, 14, tzinfo=KYIV)
    lull = [p for p in rel if t0 <= p["dt"] < t1]
    res["lull_13_50_16_14"] = {"posts": len(lull), "views": sum(p["views"] or 0 for p in lull),
                               "big_channel_posts": [(p["t_kyiv"][11:16], p["channel"], p["subs"]) for p in lull if (p["subs"] or 0) >= BIG]}

    # --- time series 10 min by group
    df = pd.DataFrame([{"dt": p["dt"], "group": p["group"], "views": p["views"] or 0} for p in rel])
    if not df.empty:
        df["bin"] = df.dt.dt.floor("10min")
        ts = df.pivot_table(index="bin", columns="group", values="views", aggfunc="count", fill_value=0)
        ts.to_csv(out / "timeseries_10min_posts.csv")
        tot = ts.sum(axis=1)
        res["peak_10min"] = {"bin": str(tot.idxmax()), "posts": int(tot.max())}
        win = tot[(tot.index >= pd.Timestamp("2026-11-02 21:30", tz=KYIV)) & (tot.index <= pd.Timestamp("2026-11-02 22:10", tz=KYIV))]
        res["max_21_30_22_10"] = {"bin": str(win.idxmax()) if len(win) else None, "posts": int(win.max()) if len(win) else 0}
        fig, ax = plt.subplots(figsize=(13, 4.8))
        bottom = None
        for g in [g for g in "BCADEU" if g in ts.columns]:
            ax.bar(ts.index, ts[g], width=1 / 144 * 0.9, bottom=bottom, color=COLORS[g], label=f"{g} · {GROUPS[g]}")
            bottom = ts[g] if bottom is None else bottom + ts[g]
        ymax = ax.get_ylim()[1]
        for j, (t, lab) in enumerate([("12:41", "12:41 gnapinfo"), ("13:57", "13:57 перший RU"), ("17:07", "17:07 НГУ"),
                                      ("17:27", "17:27 поліція"), ("19:59", "19:59 Півненко"), ("21:27", "21:27 Буданов"),
                                      ("21:32", "21:32 спростування «медика»")]):
            x = datetime.fromisoformat(f"2026-11-02T{t}:00+02:00")
            ax.axvline(x, color="#444", lw=0.6, ls=":")
            ax.text(x, ymax * (0.98 - 0.0 * j), lab, rotation=90, fontsize=7, va="top", ha="left" if j % 2 else "right")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M", tz=KYIV))
        ax.set_xlim(datetime(2026, 11, 2, 11, tzinfo=KYIV), max(df.dt) + timedelta(minutes=30))
        ax.set_ylabel("постів за 10 хв")
        ax.set_title(f"Релевантні пости за 10 хв за групами каналів (наша вибірка: {res['n_channels_relevant']} каналів)")
        ax.legend(fontsize=7, ncol=3, loc="upper right")
        fig.tight_layout()
        fig.savefig(out / "timeseries.png", dpi=150)
        ax.set_xlim(datetime(2026, 11, 2, 12, tzinfo=KYIV), datetime(2026, 11, 3, 12, tzinfo=KYIV))
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=2, tz=KYIV))
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=KYIV))
        ax.set_title("Перша доба (02.11 12:00 – 03.11 12:00): релевантні пости за 10 хв за групами каналів")
        fig.savefig(out / "timeseries_day1.png", dpi=150)
        plt.close(fig)

    # --- narratives: views by group
    nar = []
    for k in CODEBOOK:
        ps = [p for p in rel if k in p["narr"]]
        v = Counter()
        for p in ps:
            v[p["group"]] += p["views"] or 0
        tot = sum(v.values())
        nar.append({"narrative": k, "posts": len(ps), "views": tot,
                    **{f"share_{g}": round(v[g] / tot, 3) if tot else 0 for g in GROUPS}})
    pd.DataFrame(nar).to_csv(out / "narratives_by_group.csv", index=False)
    res["narratives"] = nar

    # --- medic claim vs debunk (KB8, KB9)
    cut = datetime(2026, 11, 2, 21, 32, tzinfo=KYIV)
    mc = [p for p in rel if p["medic_claim"]]
    before = sum(p["views"] or 0 for p in mc if p["dt"] < cut)
    allv = sum(p["views"] or 0 for p in mc)
    db = [p for p in rel if p["debunk"]]
    dbv = Counter()
    for p in db:
        dbv[p["group"]] += p["views"] or 0
    res["medic_claim"] = {"posts": len(mc), "channels": len({p["channel"] for p in mc}), "views": allv,
                          "share_snapshot_views_on_posts_published_before_21_32": round(before / allv, 3) if allv else None,
                          "views_accumulated_before_cutoff": None,
                          "limitation": "Counters were collected after the cutoff; historical exposure timing is not identifiable."}
    cd = [p for p in rel if p["counter_debunk"]]
    res["counter_debunk"] = {"posts": len(cd), "channels": sorted({p["channel"] for p in cd}),
                             "views": sum(p["views"] or 0 for p in cd), "first": first(lambda p: p["counter_debunk"])}
    res["debunk"] = {"posts": len(db), "channels": len({p["channel"] for p in db}), "views": sum(dbv.values()),
                     "views_by_group": dict(dbv), "first": res["first_debunk"]}

    # --- copies between groups (KB10, KB11, C2)
    clusters = copy_clusters(rel)
    lag = {"UA_from_RU": [], "RU_from_UA": []}
    first_grp = Counter()
    rows = []
    for c in clusters:
        f = c[0]
        first_grp[f["group"]] += 1
        grps = {p["group"] for p in c}
        for src, dst, key in [("C", "B", "UA_from_RU"), ("B", "C", "RU_from_UA")]:
            if f["group"] == src:
                nxt = next((p for p in c if p["group"] == dst), None)
                if nxt:
                    lag[key].append((nxt["dt"] - f["dt"]).total_seconds() / 60)
        rows.append({"size": len(c), "channels": len({p["channel"] for p in c}), "first_t": f["t_kyiv"],
                     "first_channel": f["channel"], "first_group": f["group"], "groups": "".join(sorted(grps)),
                     "views": sum(p["views"] or 0 for p in c), "text": f["text_clean"][:160]})
    pd.DataFrame(rows, columns=["size", "channels", "first_t", "first_channel", "first_group", "groups", "views", "text"]).sort_values("channels", ascending=False).to_csv(out / "copy_clusters.csv", index=False)
    res["copies"] = {"clusters_multi_channel": len(clusters), "first_by_group": dict(first_grp),
                     "method": "LSH candidates verified by exact 5-character-shingle Jaccard >= 0.6; connected components",
                     "lag_limitation": "Cluster lags are exploratory: transitive membership is not pairwise similarity or proof of copying.",
                     "cross_group_clusters": sum(1 for r in rows if len(set(r["groups"])) > 1),
                     "median_lag_min_UA_from_RU": round(median(lag["UA_from_RU"]), 1) if lag["UA_from_RU"] else None,
                     "n_UA_from_RU": len(lag["UA_from_RU"]),
                     "median_lag_min_RU_from_UA": round(median(lag["RU_from_UA"]), 1) if lag["RU_from_UA"] else None,
                     "n_RU_from_UA": len(lag["RU_from_UA"])}
    # C2: identical-text cluster among group C around 19:00
    w0, w1 = datetime(2026, 11, 2, 18, 30, tzinfo=KYIV), datetime(2026, 11, 2, 19, 30, tzinfo=KYIV)
    c2 = []
    for c in clusters:
        cc = {p["channel"] for p in c if p["group"] == "C" and w0 <= p["dt"] <= w1}
        if cc:
            c2.append((len(cc), c[0]["text_clean"][:120]))
    res["c2_ru_same_text_18_30_19_30"] = sorted(c2, reverse=True)[:5]

    # --- forwards graph summary
    fw = Counter((p["fwd_channel"], p["channel"]) for p in rel if p.get("fwd_channel"))
    pd.DataFrame([{"from": a, "to": b, "n": n, "from_group": gmap.get(a, "U"), "to_group": gmap.get(b, "U")}
                  for (a, b), n in fw.most_common()]).to_csv(out / "forwards_edges.csv", index=False)
    res["forwards"] = {"edges": len(fw), "posts_forwarded": sum(fw.values())}

    # --- channels summary by group
    grp = Counter()
    grpv = Counter()
    for p in rel:
        grp[p["group"]] += 1
        grpv[p["group"]] += p["views"] or 0
    res["by_group"] = {g: {"posts": grp[g], "views": grpv[g]} for g in GROUPS if grp[g]}

    (out / "results.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    print(json.dumps({k: res[k] for k in ["n_relevant", "n_channels_relevant", "first_overall", "peak_10min",
                                           "medic_claim", "copies"]}, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
