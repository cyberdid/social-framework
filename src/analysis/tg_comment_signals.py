"""Automation / coordination SIGNALS in Telegram discussion comments (pseudonymised input from tg_comments_collect.py).

No ground truth -> signals for review, never a "bot share" (Rauchfleisch & Kaiser 2020; Martini et al. 2021).
Signals:
  S1 concentration      top-k share, Gini, accounts >= 10 comments
  S2 copypasta          same normalized text (>= 20 chars) from >= 3 distinct accounts; near-dups via MinHash LSH
                        candidates verified by exact 5-gram Jaccard >= 0.8
  S2x cross-thread      same account posts the same/near-same text (>= 20 chars) in >= 3 different threads
  S3 rapid fire         same account, consecutive comments < 10 s apart
  S4 synchronized       >= 3 distinct accounts, verified near-duplicate text, within 10 minutes
  S7 template pool     >= 10 distinct phrases, each by >= 3 accounts, once per thread, same >= 3 threads
  S6 profile proxies    Telegram `bot` flag (true label for official bots only), sender is a channel,
                        no username, no photo, digits in name, username ending in >= 4 digits (weak; aggregate only)
Context: by parent channel group (config/channel_groups*.csv) and stance-neutral.
Usage: python src/analysis/tg_comment_signals.py data/raw/telegram_comments/<snap>
Out:   reports/telegram-comments/<snap>/{signals.json,signals.md-ready tables,flagged_accounts.csv,copypasta_groups.csv}
"""
import csv
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from datasketch import MinHash, MinHashLSH

ROOT = Path(__file__).resolve().parents[2]


def norm(t):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (t or "").lower())).strip()


def sh(t, k=5):
    return {t[i:i + k] for i in range(max(1, len(t) - k + 1))}


def gini(xs):
    xs = sorted(xs)
    n, s = len(xs), sum(xs)
    return 0.0 if not n or not s else (2 * sum((i + 1) * x for i, x in enumerate(xs)) / (n * s)) - (n + 1) / n


def groups():
    g = {}
    for f in ["config/channel_groups_auto.csv", "config/channel_groups.csv"]:
        p = ROOT / f
        if p.exists():
            for r in csv.DictReader(l for l in p.open() if not l.startswith("#")):
                g[r["channel"].lower()] = r["group"]
    return g


def main():
    snap = Path(sys.argv[1]).resolve()
    C = [json.loads(l) for l in (snap / "comments.jsonl").open()]
    if C and "video_id" in C[0]:  # YouTube format (src/youtube_collect.py)
        from datetime import timezone as _tz
        C = [{"sender": c["author"], "post_url": f"https://www.youtube.com/watch?v={c['video_id']}", "channel": "youtube",
              "comment_id": c["comment_id"], "text": c.get("text") or "", "sender_type": "user",
              "date": datetime.fromtimestamp(c["timestamp"], _tz.utc).isoformat()} for c in C if c.get("timestamp")]
    C = [c for c in C if c.get("sender")]
    for c in C:
        c["t"] = datetime.fromisoformat(c["date"])
        c["thread"] = c["post_url"]
        c["n"] = norm(c["text"])
    out = ROOT / ("reports/youtube-comments" if "youtube" in str(snap) else "reports/telegram-comments") / snap.name
    out.mkdir(parents=True, exist_ok=True)
    G = groups()
    by_acct = defaultdict(list)
    for c in C:
        by_acct[c["sender"]].append(c)
    n, na = len(C), len(by_acct)
    per = sorted((len(v) for v in by_acct.values()), reverse=True)
    res = {"source": str(snap.relative_to(ROOT)), "comments": n, "accounts": na,
           "threads": len({c["thread"] for c in C}),
           "comments_per_account": {"max": per[0], "median": statistics.median(per), "ge10": sum(x >= 10 for x in per),
                                    "ge50": sum(x >= 50 for x in per)},
           "S1_top10_share": round(sum(per[:10]) / n, 3), "S1_top1pct_share": round(sum(per[:max(1, na // 100)]) / n, 3),
           "S1_gini": round(gini(per), 3)}
    flags = defaultdict(set)
    for a, v in by_acct.items():
        if len(v) >= 10:
            flags[a].add("S1_ge10")

    # S2 exact copypasta
    ex = defaultdict(list)
    for c in C:
        if len(c["n"]) >= 20:
            ex[c["n"]].append(c)
    copy_groups = [(t, v) for t, v in ex.items() if len({c["sender"] for c in v}) >= 3]
    for _, v in copy_groups:
        for c in v:
            flags[c["sender"]].add("S2_copypasta")
    # near-dups via LSH + exact Jaccard
    cand = [c for c in C if len(c["n"]) >= 30]
    lsh = MinHashLSH(threshold=0.8, num_perm=64)
    S = {}
    for i, c in enumerate(cand):
        S[i] = sh(c["n"])
        m = MinHash(num_perm=64)
        for x in S[i]:
            m.update(x.encode())
        lsh.insert(i, m)
        c["_m"] = m
    near = []
    for i, c in enumerate(cand):
        for j in lsh.query(c["_m"]):
            if j <= i:
                continue
            d = cand[j]
            jac = len(S[i] & S[j]) / len(S[i] | S[j])
            if jac >= 0.8:
                near.append((c, d, jac))
    for c in cand:
        c.pop("_m", None)
    res["S2_copypasta_groups_ge3_accounts"] = len(copy_groups)
    res["S2_copypasta_comments"] = sum(len(v) for _, v in copy_groups)
    res["S2_near_pairs_verified"] = len(near)
    res["S2_near_pairs_diff_accounts"] = sum(a["sender"] != b["sender"] for a, b, _ in near)

    # S7 template pool: many distinct phrases, each used by distinct accounts at most once per thread,
    # sharing the same set of >= 3 threads (a phrase list iterated over several posts; comment-farm signature)
    sig = defaultdict(list)
    for t, v in ex.items():
        if len(t) < 20 or len({c["sender"] for c in v}) < 3:
            continue
        per_thread = Counter(c["thread"] for c in v)
        if max(per_thread.values()) == 1 and len(per_thread) >= 3:
            sig[frozenset(per_thread)].append((t, v))
    pools = [(ths, items) for ths, items in sig.items() if len(items) >= 10]
    res["S7_template_pools"] = []
    for ths, items in pools:
        cs = [c for _, v in items for c in v]
        accts = {c["sender"] for c in cs}
        for a in accts:
            flags[a].add("S7_template_pool")
        tot = {u: sum(1 for c in C if c["thread"] == u) for u in ths}
        res["S7_template_pools"].append({
            "phrases": len(items), "comments": len(cs), "accounts": len(accts), "threads": sorted(ths),
            "share_of_thread_comments": {u: round(sum(1 for c in cs if c["thread"] == u) / tot[u], 3) for u in ths},
            "first_utc": min(c["date"] for c in cs), "last_utc": max(c["date"] for c in cs),
            "examples": [t[:80] for t, _ in items[:8]]})

    # S2x cross-thread repetition by same account
    xt = 0
    for a, v in by_acct.items():
        th = defaultdict(set)
        for c in v:
            if len(c["n"]) >= 20:
                th[c["n"]].add(c["thread"])
        if any(len(s) >= 3 for s in th.values()):
            flags[a].add("S2x_cross_thread_repeat")
            xt += 1
    for a, b, _ in near:
        if a["sender"] == b["sender"] and a["thread"] != b["thread"]:
            flags[a["sender"]].add("S2x_cross_thread_near")
    res["S2x_accounts_same_text_ge3_threads"] = xt

    # S3 rapid fire
    rapid = 0
    for a, v in by_acct.items():
        v = sorted(v, key=lambda c: c["t"])
        for x, y in zip(v, v[1:]):
            if (y["t"] - x["t"]).total_seconds() < 10:
                rapid += 1
                flags[a].add("S3_rapid")
    res["S3_rapid_pairs"] = rapid

    # S4 synchronized: components of near-dup edges (diff accounts, <= 10 min) with >= 3 accounts
    adj = defaultdict(set)
    idx = {}
    for a, b, _ in near:
        if a["sender"] != b["sender"] and abs((a["t"] - b["t"]).total_seconds()) <= 600:
            ka, kb = (a["thread"], a["comment_id"]), (b["thread"], b["comment_id"])
            idx[ka], idx[kb] = a, b
            adj[ka].add(kb)
            adj[kb].add(ka)
    seen, sync = set(), []
    for k in adj:
        if k in seen:
            continue
        comp, st = set(), [k]
        while st:
            x = st.pop()
            if x not in comp:
                comp.add(x)
                st.extend(adj[x] - comp)
        seen |= comp
        if len({idx[x]["sender"] for x in comp}) >= 3:
            sync.append(comp)
            for x in comp:
                flags[idx[x]["sender"]].add("S4_sync")
    res["S4_sync_groups"] = len(sync)
    res["S4_sync_comments"] = sum(len(c) for c in sync)

    # S6 profile (aggregate over accounts)
    prof = {a: v[0] for a, v in by_acct.items()}
    users = [p for p in prof.values() if p.get("sender_type") == "user"]
    res["S6"] = {"sender_types": dict(Counter(p.get("sender_type") for p in prof.values())),
                 "telegram_bot_flag_accounts": sum(bool(p.get("bot")) for p in users),
                 "premium_share": round(sum(bool(p.get("premium")) for p in users) / max(1, len(users)), 3),
                 "no_username_share": round(sum(not p.get("has_username") for p in users) / max(1, len(users)), 3),
                 "no_photo_share": round(sum(not p.get("has_photo") for p in users) / max(1, len(users)), 3),
                 "username_digits_tail_share": round(sum(bool(p.get("username_digits_tail")) for p in users) / max(1, len(users)), 3),
                 "scam_or_fake": sum(bool(p.get("scam") or p.get("fake")) for p in users),
                 "deleted_accounts": sum(bool(p.get("deleted")) for p in users)}
    for a, p in prof.items():
        if p.get("bot"):
            flags[a].add("S6_tg_bot_flag")
        if p.get("sender_type") == "channel":
            flags[a].add("S6_channel_sender")

    behav = {a: {s for s in f if s.split("_")[0] in ("S1", "S2", "S2x", "S3", "S4", "S7")} for a, f in flags.items()}
    multi = {a for a, s in behav.items() if len({x.split("_")[0] for x in s}) >= 2}
    res["accounts_any_behavioural_signal"] = sum(1 for s in behav.values() if s)
    res["accounts_ge2_signal_types"] = len(multi)
    res["comments_from_ge2_accounts"] = sum(len(by_acct[a]) for a in multi)
    res["share_comments_from_ge2_accounts"] = round(res["comments_from_ge2_accounts"] / n, 4)

    # by parent channel group
    bg = defaultdict(lambda: {"comments": 0, "accounts": set(), "from_ge2": 0, "threads": set()})
    for c in C:
        g = G.get(c["channel"].lower(), "U")
        bg[g]["comments"] += 1
        bg[g]["accounts"].add(c["sender"])
        bg[g]["threads"].add(c["thread"])
        bg[g]["from_ge2"] += c["sender"] in multi
    res["by_parent_group"] = {g: {"comments": v["comments"], "threads": len(v["threads"]), "accounts": len(v["accounts"]),
                                  "share_from_ge2_signal_accounts": round(v["from_ge2"] / v["comments"], 4)} for g, v in sorted(bg.items())}
    # accounts active across groups (same pseudonym commenting under B and C channels)
    acct_groups = defaultdict(set)
    for c in C:
        acct_groups[c["sender"]].add(G.get(c["channel"].lower(), "U"))
    res["accounts_commenting_in_both_B_and_C"] = sum(1 for s in acct_groups.values() if {"B", "C"} <= s)

    (out / "signals.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    with (out / "copypasta_groups.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["text", "comments", "accounts", "threads", "channels", "first_utc", "last_utc"])
        for t, v in sorted(copy_groups, key=lambda x: -len(x[1])):
            w.writerow([t[:200], len(v), len({c["sender"] for c in v}), len({c["thread"] for c in v}),
                        ";".join(sorted({c["channel"] for c in v}))[:200], min(c["date"] for c in v), max(c["date"] for c in v)])
    with (out / "flagged_accounts.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pseudo_account", "comments", "threads", "signals", "sender_type", "tg_bot_flag", "groups"])
        for a, s in sorted(flags.items(), key=lambda kv: -len(by_acct[kv[0]])):
            v = by_acct[a]
            w.writerow([a, len(v), len({c["thread"] for c in v}), ";".join(sorted(s)), prof[a].get("sender_type"),
                        int(bool(prof[a].get("bot"))), "".join(sorted(acct_groups[a]))])
    print(json.dumps({k: res[k] for k in ["comments", "accounts", "threads", "S1_top10_share", "S1_gini",
                                           "S2_copypasta_groups_ge3_accounts", "S2_near_pairs_diff_accounts",
                                           "S2x_accounts_same_text_ge3_threads", "S3_rapid_pairs", "S4_sync_groups",
                                           "accounts_ge2_signal_types", "share_comments_from_ge2_accounts"]} | {"S7_pools": [(x["phrases"], x["comments"], x["accounts"], len(x["threads"])) for x in res["S7_template_pools"]]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
