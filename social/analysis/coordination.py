"""Copy network and synchronous copies among relevant Telegram posts.

Edges: exact Jaccard of 5-character shingles >= threshold between posts of DIFFERENT channels (texts >= min_chars),
the same definition as the research code (coordination_lsh_audit), computed exhaustively with a sparse matrix product.
Synchronous copy: lag <= sync_minutes. Null model: post times are permuted among relevant posts of the same
channel and day (keeps each channel's daily activity, breaks cross-channel alignment); the statistic is the
number of synchronous edges. A low p-value says copies are more synchronous than chance — not who coordinates.
Output: data/processed/analysis/coordination/{edges.csv, components.json, coordination.json}
"""
from __future__ import annotations

import json
import random
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from .corpus import load_posts
from .measures import _csv, _out


def shingles(text: str, k: int = 5) -> set[str]:
    t = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (text or "").lower())).strip()
    return {t[i:i + k] for i in range(max(1, len(t) - k + 1))}


def edges(posts: list[dict], threshold: float = 0.6, min_chars: int = 80) -> list[dict]:
    """Exact cross-channel Jaccard over 5-character shingles via a sparse matrix product (scipy)."""
    import numpy as np
    from scipy.sparse import csr_matrix

    el = sorted([p for p in posts if len(p.get("text_clean") or "") >= min_chars and p.get("channel")], key=lambda p: p["url"])
    if len(el) < 2:
        return []
    sh = [shingles(p["text_clean"]) for p in el]
    vocab: dict[str, int] = {}
    rows, cols = [], []
    for i, s in enumerate(sh):
        for x in s:
            rows.append(i)
            cols.append(vocab.setdefault(x, len(vocab)))
    m = csr_matrix((np.ones(len(rows), dtype=np.int32), (rows, cols)), shape=(len(el), len(vocab)))
    inter = (m @ m.T).tocoo()
    sizes = np.asarray([len(s) for s in sh])
    out = []
    for i, j, k in zip(inter.row, inter.col, inter.data):
        i, j, k = int(i), int(j), int(k)
        if i >= j or el[i]["channel"] == el[j]["channel"]:
            continue
        jac = k / (sizes[i] + sizes[j] - k)
        if jac < threshold:
            continue
        first, second = sorted((el[i], el[j]), key=lambda p: (p["t_local"], p["url"]))
        lag = (datetime.fromisoformat(second["t_local"]) - datetime.fromisoformat(first["t_local"])).total_seconds() / 60
        out.append({"earlier_url": first["url"], "later_url": second["url"], "earlier_channel": first["channel"],
                    "later_channel": second["channel"], "earlier_group": first["group"], "later_group": second["group"],
                    "earlier_time": first["t_local"], "later_time": second["t_local"], "lag_minutes": round(lag, 1),
                    "jaccard": round(float(jac), 3), "earlier_text": first["text_clean"][:300]})
    return sorted(out, key=lambda e: (e["earlier_time"], e["later_url"]))


def _sync_count(es: list[dict], times: dict[str, datetime], minutes: float) -> int:
    return sum(abs((times[e["later_url"]] - times[e["earlier_url"]]).total_seconds()) <= minutes * 60 for e in es)


def analyze(case: Path, threshold: float = 0.6, sync_minutes: float = 10, permutations: int = 999, seed: int = 20261111) -> dict:
    posts = load_posts(case)
    es = edges(posts, threshold)
    out = _out(case, "coordination")
    _csv(out / "edges.csv", es)
    by_url = {p["url"]: p for p in posts}
    times = {u: datetime.fromisoformat(p["t_local"]) for u, p in by_url.items()}
    observed = _sync_count(es, times, sync_minutes)
    cells = defaultdict(list)
    for p in posts:
        cells[(p["channel"], p["day"])].append(p["url"])
    rnd = random.Random(seed)
    ge = 0
    null = []
    if es:
        for _ in range(permutations):
            perm = dict(times)
            for urls in cells.values():
                if len(urls) > 1:
                    ts = [times[u] for u in urls]
                    rnd.shuffle(ts)
                    perm.update(zip(urls, ts))
            s = _sync_count(es, perm, sync_minutes)
            null.append(s)
            ge += s >= observed
    # components of the channel graph (union-find)
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    w = Counter()
    for e in es:
        a, b = e["earlier_channel"], e["later_channel"]
        parent[find(a)] = find(b)
        w[a] += 1
        w[b] += 1
    comps = defaultdict(set)
    for ch in list(parent):
        comps[find(ch)].add(ch)
    grp = {p["channel"]: p["group"] for p in posts}
    comp_list = sorted(comps.values(), key=len, reverse=True)
    components = [{"size": len(c), "groups": dict(Counter(grp.get(x, "U") for x in c)),
                   "top": sorted(c, key=lambda x: -w[x])[:10]} for c in comp_list[:20]]
    (out / "components.json").write_text(json.dumps(components, ensure_ascii=False, indent=1), encoding="utf-8")
    res = {"edges": len(es), "channel_pairs": len({tuple(sorted((e["earlier_channel"], e["later_channel"]))) for e in es}),
           "channels": len(parent), "sync_minutes": sync_minutes, "sync_edges": observed,
           "null_mean_sync_edges": round(sum(null) / len(null), 2) if null else None,
           "p_value_one_sided": round((1 + ge) / (1 + len(null)), 4) if null else None,
           "largest_components": components[:5],
           "note": "synchrony above chance shows copying in near real time, not who coordinates"}
    (out / "coordination.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res
