"""Signals of possible automation / coordinated commenting in a TikTok comment snapshot.

No ground truth exists, so the output is SIGNALS for manual review, never a "bot share".
Accounts are pseudonymised with HMAC-SHA256 (secret salt in ~/.config/social/pseudo_salt, mode 600):
no handle, nickname, uid or avatar leaves this script. CPU only.

Signals (each documented with its threshold; literature basis in wiki/methods/research-methodology-review.md):
  S1 concentration   - share of comments by top accounts, Gini, accounts with >= 5 comments
  S2 copypasta       - same normalized text (>= 15 chars, not emoji-only) from >= 2 distinct accounts;
                       near-duplicates: exact 5-char-shingle Jaccard >= 0.8 between different accounts
  S3 rapid fire      - same account, consecutive comments < 10 s apart (human typing floor, heuristic)
  S4 synchronized    - >= 3 distinct accounts posting near-duplicate text within 10 minutes
  S5 bursts          - 5-minute bins with count > median + 4*MAD of the comment series
  S6 profile proxies - default-looking handle (^user\\d{6,}$), nickname == handle, empty nickname
                       (weak: common for real low-activity users; reported only in aggregate and as a co-signal)
Cross-tab with the manual stance categories in reports/tiktok-fire/comment_candidate_review.csv.

Usage: python src/analysis/automation_signals.py data/raw/tiktok/<snap>
"""
import csv
import hashlib
import hmac
import json
import os
import re
import secrets
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SALT = Path.home() / ".config/social/pseudo_salt"


def salt() -> bytes:
    if not SALT.exists():
        os.umask(0o077)
        SALT.write_text(secrets.token_hex(32))
    return bytes.fromhex(SALT.read_text().strip())


def pseudo(uid: str, key: bytes) -> str:
    return "acct_" + hmac.new(key, uid.encode(), hashlib.sha256).hexdigest()[:10]


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (t or "").lower())).strip()


def shingles(t: str, k=5):
    return {t[i:i + k] for i in range(max(1, len(t) - k + 1))}


def gini(xs):
    xs = sorted(xs)
    n = len(xs)
    return 0.0 if n == 0 or sum(xs) == 0 else (2 * sum((i + 1) * x for i, x in enumerate(xs)) / (n * sum(xs))) - (n + 1) / n


def load(snapdir: Path, key: bytes):
    recs = {}
    for f in sorted((snapdir / "responses").iterdir()):
        try:
            d = json.loads(f.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        for c in d.get("comments") or []:
            u = c.get("user") or {}
            uid = str(u.get("uid") or u.get("sec_uid") or "")
            if not uid or c["cid"] in recs:
                continue
            handle = u.get("unique_id") or ""
            nick = u.get("nickname") or ""
            recs[c["cid"]] = {
                "cid": c["cid"], "acct": pseudo(uid, key),
                "t": datetime.fromtimestamp(c["create_time"], timezone.utc),
                "text": c.get("text") or "", "lang": c.get("comment_language"),
                "likes": c.get("digg_count") or 0, "parent": None if str(c.get("reply_id", "0")) == "0" else str(c.get("reply_id")),
                "default_handle": bool(re.fullmatch(r"user\d{6,}", handle)),
                "nick_eq_handle": bool(nick) and nick == handle, "nick_empty": not nick.strip(),
                "verified": bool(u.get("custom_verify") or u.get("enterprise_verify_reason")),
            }
    return list(recs.values())


def main():
    snapdir = Path(sys.argv[1]).resolve()
    key = salt()
    C = load(snapdir, key)
    out = ROOT / ("reports/tiktok-fire" if "-fire-" in snapdir.name else f"reports/tiktok/{snapdir.name}")
    out.mkdir(parents=True, exist_ok=True)
    by_acct = defaultdict(list)
    for c in C:
        by_acct[c["acct"]].append(c)
    n, na = len(C), len(by_acct)
    per = sorted((len(v) for v in by_acct.values()), reverse=True)
    res = {"source": str(snapdir.relative_to(ROOT)), "comments": n, "accounts": na,
           "comments_per_account": {"max": per[0] if per else 0, "median": statistics.median(per) if per else 0,
                                    "accounts_ge2": sum(x >= 2 for x in per), "accounts_ge5": sum(x >= 5 for x in per)},
           "S1_top1_share": round(per[0] / n, 3) if n else None, "S1_top5_share": round(sum(per[:5]) / n, 3) if n else None,
           "S1_top10pct_accounts_share": round(sum(per[:max(1, na // 10)]) / n, 3) if n else None,
           "S1_gini": round(gini(per), 3)}
    flags = defaultdict(set)
    for a, v in by_acct.items():
        if len(v) >= 5:
            flags[a].add("S1_ge5_comments")

    # S2 copypasta: exact
    groups = defaultdict(list)
    for c in C:
        t = norm(c["text"])
        if len(t) >= 15 and re.search(r"[a-zа-яіїєґё]", t):
            groups[t].append(c)
    exact = [(t, v) for t, v in groups.items() if len({c["acct"] for c in v}) >= 2]
    for _, v in exact:
        for c in v:
            flags[c["acct"]].add("S2_exact_copy")
    # S2 near-duplicates between different accounts
    cand = [c for c in C if len(norm(c["text"])) >= 20]
    sh = {c["cid"]: shingles(norm(c["text"])) for c in cand}
    near = []
    for a, b in combinations(cand, 2):
        if a["acct"] == b["acct"]:
            continue
        sa, sb = sh[a["cid"]], sh[b["cid"]]
        j = len(sa & sb) / len(sa | sb)
        if j >= 0.8:
            near.append((a, b, j))
            flags[a["acct"]].add("S2_near_copy")
            flags[b["acct"]].add("S2_near_copy")
    res["S2_exact_copy_groups"] = len(exact)
    res["S2_exact_copy_comments"] = sum(len(v) for _, v in exact)
    res["S2_near_copy_pairs"] = len(near)
    res["S2_examples"] = [{"text": t[:120], "comments": len(v), "accounts": len({c["acct"] for c in v}),
                           "span_min": round((max(c["t"] for c in v) - min(c["t"] for c in v)).total_seconds() / 60, 1)}
                          for t, v in sorted(exact, key=lambda x: -len(x[1]))[:10]]

    # S3 rapid fire
    rapid = 0
    for a, v in by_acct.items():
        v = sorted(v, key=lambda c: c["t"])
        for x, y in zip(v, v[1:]):
            if (y["t"] - x["t"]).total_seconds() < 10:
                rapid += 1
                flags[a].add("S3_rapid_fire")
    res["S3_rapid_pairs"] = rapid

    # S4 synchronized near-duplicates (>=3 distinct accounts within 10 min)
    adj = defaultdict(set)
    for a, b, _ in near:
        if abs((a["t"] - b["t"]).total_seconds()) <= 600:
            adj[a["cid"]].add(b["cid"])
            adj[b["cid"]].add(a["cid"])
    byid = {c["cid"]: c for c in C}
    seen, sync = set(), []
    for cid in adj:
        if cid in seen:
            continue
        comp, stack = set(), [cid]
        while stack:
            x = stack.pop()
            if x in comp:
                continue
            comp.add(x)
            stack.extend(adj[x] - comp)
        seen |= comp
        accts = {byid[x]["acct"] for x in comp}
        if len(accts) >= 3:
            sync.append(comp)
            for x in comp:
                flags[byid[x]["acct"]].add("S4_synchronized")
    res["S4_synchronized_groups"] = len(sync)

    # S5 bursts (5-min bins)
    bins = Counter(c["t"].replace(minute=c["t"].minute - c["t"].minute % 5, second=0, microsecond=0) for c in C)
    vals = list(bins.values())
    med = statistics.median(vals)
    mad = statistics.median(abs(x - med) for x in vals) or 1
    bursts = sorted(((k, v) for k, v in bins.items() if v > med + 4 * mad), key=lambda kv: -kv[1])
    res["S5_bins"] = len(vals)
    res["S5_median_per_5min"] = med
    res["S5_burst_bins"] = [{"bin_utc": k.isoformat(), "comments": v} for k, v in bursts[:10]]
    first, last = min(c["t"] for c in C), max(c["t"] for c in C)
    res["time_span_utc"] = [first.isoformat(), last.isoformat()]
    res["share_first_6h"] = round(sum(1 for c in C if (c["t"] - first).total_seconds() <= 6 * 3600) / n, 3)

    # S6 profile proxies (aggregate)
    acct_prof = {}
    for a, v in by_acct.items():
        acct_prof[a] = v[0]
    res["S6_default_handle_share"] = round(sum(p["default_handle"] for p in acct_prof.values()) / na, 3)
    res["S6_nick_eq_handle_share"] = round(sum(p["nick_eq_handle"] for p in acct_prof.values()) / na, 3)
    res["S6_nick_empty_share"] = round(sum(p["nick_empty"] for p in acct_prof.values()) / na, 3)
    res["verified_accounts"] = sum(p["verified"] for p in acct_prof.values())
    res["lang"] = dict(Counter(c["lang"] for c in C).most_common(6))

    # Combined: accounts with >= 2 independent behavioural signals (S1..S4), plus profile proxy as co-signal
    behav = {a: {s for s in f if s[:2] in ("S1", "S2", "S3", "S4")} for a, f in flags.items()}
    multi = {a: s for a, s in behav.items() if len({x.split("_")[0] for x in s}) >= 2}
    res["accounts_any_behavioural_signal"] = sum(1 for s in behav.values() if s)
    res["accounts_ge2_signal_types"] = len(multi)
    res["comments_from_ge2_signal_accounts"] = sum(len(by_acct[a]) for a in multi)

    # Cross-tab with manual stance categories
    cat = {}
    rv = ROOT / "reports/tiktok-fire/comment_candidate_review.csv"  # manual stances exist only for the fire video
    if rv.exists():
        for r in csv.DictReader(rv.open()):
            cat[r["comment_id"]] = r["category"]
    xt = defaultdict(lambda: {"comments": 0, "accounts": set(), "flagged_comments": 0})
    for c in C:
        k = cat.get(c["cid"], "not_candidate")
        xt[k]["comments"] += 1
        xt[k]["accounts"].add(c["acct"])
        xt[k]["flagged_comments"] += int(bool(behav.get(c["acct"])))
    res["by_manual_category"] = {k: {"comments": v["comments"], "accounts": len(v["accounts"]),
                                     "comments_from_flagged_accounts": v["flagged_comments"]} for k, v in sorted(xt.items())}

    (out / "automation_signals.json").write_text(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    with (out / "automation_flagged_accounts.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["pseudo_account", "comments", "signals", "default_handle", "first_utc", "last_utc"])
        for a, s in sorted(behav.items(), key=lambda kv: -len(by_acct[kv[0]])):
            if s:
                v = sorted(by_acct[a], key=lambda c: c["t"])
                w.writerow([a, len(v), ";".join(sorted(s)), int(acct_prof[a]["default_handle"]), v[0]["t"].isoformat(), v[-1]["t"].isoformat()])
    print(json.dumps({k: res[k] for k in ["comments", "accounts", "S1_top5_share", "S1_gini", "S2_exact_copy_groups",
                                           "S2_near_copy_pairs", "S3_rapid_pairs", "S4_synchronized_groups",
                                           "accounts_any_behavioural_signal", "accounts_ge2_signal_types",
                                           "S6_default_handle_share"]}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
