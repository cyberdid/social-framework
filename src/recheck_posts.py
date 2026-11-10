"""Re-check every relevant post of a snapshot via the public embed page (t.me/<ch>/<id>?embed=1):
still available? text changed? views now? Detects deletions/edits and view growth between snapshots.

Usage: python src/recheck_posts.py <snapshot> [--delay 1.0]
Out:   data/raw/telegram_recheck/<snapshot>__<UTC now>/recheck.jsonl + manifest.json
"""
import argparse
import gzip
import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) research-recheck (public posts only)"


def parse_count(s):
    if not s:
        return None
    s = s.strip().replace(",", ".")
    m = {"K": 1_000, "M": 1_000_000}.get(s[-1], 1)
    try:
        return int(round(float(s[:-1] if m > 1 else s) * m))
    except ValueError:
        return None


def text_of(page):
    m = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', page, re.S)
    if not m:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", m.group(1))
    return html.unescape(re.sub(r"<[^>]+>", "", t)).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    posts = [json.loads(l) for l in (ROOT / "data/processed" / a.snapshot / "posts_clean.jsonl").open()]
    rel = [p for p in posts if p["relevant"]]
    if a.limit:
        rel = rel[: a.limit]
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "data/raw/telegram_recheck" / f"{a.snapshot}__{now}"
    out.mkdir(parents=True)
    pages = gzip.open(out / "pages.jsonl.gz", "wt", encoding="utf-8")
    counts = {"checked": 0, "available": 0, "not_found": 0, "error": 0, "text_changed": 0, "edited_flag": 0}
    with (out / "recheck.jsonl").open("w", encoding="utf-8") as f:
        for p in rel:
            url = f"{p['url']}?embed=1&mode=tme"
            rec = {"url": p["url"], "channel": p["channel"], "msg_id": p["msg_id"], "views_before": p["views"],
                   "fetched_before": p["fetched_at"], "fetched_at": datetime.now(timezone.utc).isoformat()}
            try:
                r = S.get(url, timeout=30)
                page = r.text
                pages.write(json.dumps({"url": url, "status": r.status_code, "html": page}, ensure_ascii=False) + "\n")
                if "tgme_widget_message_error" in page or "Post not found" in page:
                    rec["status"] = "not_found"
                elif "tgme_widget_message_text" in page or "tgme_widget_message_bubble" in page:
                    rec["status"] = "available"
                    v = re.search(r'tgme_widget_message_views">([^<]+)<', page)
                    rec["views_now"] = parse_count(v.group(1)) if v else None
                    rec["edited_flag"] = bool(re.search(r">\s*edited\s*<", page))
                    t_now = text_of(page)
                    rec["text_changed"] = bool(t_now) and re.sub(r"\s+", " ", t_now) != re.sub(r"\s+", " ", p["text"] or "")
                    if rec["text_changed"]:
                        rec["text_now"] = t_now
                else:
                    rec["status"] = f"unknown_{r.status_code}"
            except requests.RequestException as e:
                rec["status"] = "error"
                rec["error"] = f"{type(e).__name__}: {e}"
            counts["checked"] += 1
            counts[rec["status"]] = counts.get(rec["status"], 0) + 1
            counts["text_changed"] += int(rec.get("text_changed", False))
            counts["edited_flag"] += int(rec.get("edited_flag", False))
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            time.sleep(a.delay)
    pages.close()
    sha = hashlib.sha256((out / "recheck.jsonl").read_bytes()).hexdigest()
    (out / "manifest.json").write_text(json.dumps({"snapshot": a.snapshot, "recheck_utc": now, "counts": counts,
                                                   "sha256_recheck": sha}, indent=2))
    print(json.dumps(counts), "->", out)


if __name__ == "__main__":
    main()
