#!/usr/bin/env python3
"""Snapshot one public TikTok's comments without login or browser cookies.

Requires curl_cffi. Internal public web endpoints can change or filter results.
No retries after a denial/platform error. Each run creates a new snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse


def now():
    return datetime.now(timezone.utc).isoformat()


def walk_pages(fetch, max_pages=100):
    rows, cursor, seen = [], 0, {0}
    status = {"complete_pagination": False, "pages": 0, "reported_totals": []}
    for _ in range(max_pages):
        page = fetch(cursor)
        status["pages"] += 1
        if page.get("status_code") != 0:
            status.update(stop_reason="platform_error", platform_status=page.get("status_code"),
                          status_msg=page.get("status_msg"))
            break
        comments = page.get("comments") or []
        if not isinstance(comments, list):
            status["stop_reason"] = "invalid_comments_shape"
            break
        rows.extend(comments)
        if page.get("total") is not None:
            status["reported_totals"].append(page["total"])
        if page.get("has_more") in (False, 0) and "has_more" in page:
            status.update(stop_reason="has_more_false", complete_pagination=True)
            break
        nxt = page.get("cursor")
        if nxt is None or nxt in seen:
            status["stop_reason"] = "cursor_missing_or_repeated"
            break
        cursor = nxt
        seen.add(cursor)
    else:
        status["stop_reason"] = "page_cap"
    status.update(records_returned=len(rows), distinct_ids=len({str(c.get("cid")) for c in rows if c.get("cid")}))
    return rows, status


def normalize_comment(comment, video_id, video_url, fetched_at, parent_id=None):
    stamp = comment.get("create_time")
    return {
        "platform": "tiktok", "video_id": str(video_id), "video_url": video_url,
        "comment_id": str(comment["cid"]), "parent_id": parent_id,
        "text": comment.get("text", ""),
        "created_at_utc": datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp is not None else None,
        "likes": comment.get("digg_count"), "reply_count": comment.get("reply_comment_total"),
        "fetched_at_utc": fetched_at, "pinned": bool(comment["author_pin"]) if "author_pin" in comment else None,
    }


def read_seed_receipts(seed_dir, video_id, repo_root):
    seeds = {}
    if seed_dir is None:
        return seeds
    for receipt in sorted(seed_dir.glob("tiktok-fire-*-access.json")):
        obj = json.loads(receipt.read_text())
        calls = obj if isinstance(obj, list) else obj.get("calls", [])
        for call in calls:
            url = call.get("url", "")
            query = parse_qs(urlparse(url).query)
            if query.get("aweme_id", query.get("item_id", [None]))[0] != video_id:
                continue
            source = call.get("response_file")
            if not source:
                continue
            path = repo_root / source
            if not path.is_file():
                continue
            key = (query.get("comment_id", [None])[0], int(query.get("cursor", [0])[0]))
            seeds.setdefault(key, {**call, "source": path})
    return seeds


class Collector:
    def __init__(self, video_id, video_url, output, seeds, delay, request_cap):
        self.video_id, self.video_url, self.output = video_id, video_url, output
        self.seeds, self.delay, self.request_cap = seeds, delay, request_cap
        self.calls, self.network_count, self.blocked = [], 0, False
        self.observed = {}
        (output / "responses").mkdir(parents=True)

    def fetch(self, parent_id, cursor):
        index = len(self.calls)
        params = {"aid": 1988, "count": 50, "cursor": cursor}
        if parent_id:
            params.update(item_id=self.video_id, comment_id=parent_id)
        else:
            params["aweme_id"] = self.video_id
        url = "https://www.tiktok.com/api/comment/list/" + ("reply/" if parent_id else "") + "?" + urlencode(params)
        call = {"url": url, "parent_id": parent_id, "cursor_in": cursor, "fetched_at_utc": now()}
        seed = self.seeds.get((parent_id, cursor))
        if seed:
            body = seed["source"].read_bytes()
            call.update(source="imported_previous_public_response", imported_from=str(seed["source"]),
                        fetched_at_utc=seed.get("accessed_at_utc", call["fetched_at_utc"]),
                        http_status=seed.get("status"))
        else:
            if self.blocked or self.network_count >= self.request_cap:
                return {"status_code": -1, "status_msg": "stopped_after_error_or_request_cap"}
            if self.network_count:
                time.sleep(self.delay)
            from curl_cffi import requests
            self.network_count += 1
            call["source"] = "anonymous_public_http_no_user_cookies"
            try:
                response = requests.get(url, impersonate="chrome", timeout=20,
                                        headers={"Referer": self.video_url})
                body = response.content
                call.update(http_status=response.status_code,
                            content_type=response.headers.get("Content-Type"))
            except Exception as exc:
                self.blocked = True
                call["error"] = str(exc)
                body = b""
        path = self.output / "responses" / f"{index:04d}.response"
        path.write_bytes(body)
        call.update(response_file=str(path), sha256=hashlib.sha256(body).hexdigest(), byte_length=len(body))
        try:
            page = json.loads(body)
            if not isinstance(page, dict):
                raise ValueError("Expected JSON object")
        except (ValueError, TypeError) as exc:
            page = {"status_code": -1, "status_msg": "invalid_json_or_http_error"}
            call["parse_error"] = str(exc)
        if call.get("http_status") != 200 or page.get("status_code") != 0:
            self.blocked = True
        call.update(platform_status=page.get("status_code"), cursor_out=page.get("cursor"),
                    has_more=page.get("has_more"), returned=len(page.get("comments") or []))
        self.calls.append(call)
        for comment in page.get("comments") or []:
            self.observed[(parent_id, str(comment.get("cid")))] = call["fetched_at_utc"]
        return page


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video-id", required=True)
    parser.add_argument("--video-url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--normalized-output", type=Path,
                        help="Defaults to data/processed/tiktok/<snapshot>/comments.normalized.jsonl")
    parser.add_argument("--seed-dir", type=Path)
    parser.add_argument("--delay", type=float, default=0.75)
    parser.add_argument("--max-requests", type=int, default=200)
    args = parser.parse_args(argv)
    if not args.video_id.isdigit() or args.delay < 0 or args.max_requests < 0:
        parser.error("Numeric video ID, nonnegative delay and request cap required")
    if urlparse(args.video_url).hostname not in {"www.tiktok.com", "tiktok.com"}:
        parser.error("Use the canonical public tiktok.com video URL")
    root = Path(__file__).resolve().parents[1]
    normalized_path = args.normalized_output or root / "data/processed/tiktok" / args.output.name / "comments.normalized.jsonl"
    if normalized_path.exists():
        parser.error("Refusing to overwrite normalized output; use a new snapshot")
    if args.output.exists():
        parser.error("Refusing to overwrite a snapshot; use a new output directory")
    args.output.mkdir(parents=True)
    seeds = read_seed_receipts(args.seed_dir, args.video_id, root)
    collector = Collector(args.video_id, args.video_url, args.output, seeds, args.delay, args.max_requests)
    started = now()
    top, top_status = walk_pages(lambda cursor: collector.fetch(None, cursor))
    unique_top = {str(row["cid"]): row for row in top if row.get("cid")}
    normalized = {}
    for cid, row in unique_top.items():
        normalized[cid] = normalize_comment(row, args.video_id, args.video_url, collector.observed[(None, cid)])
    threads = []
    for cid, row in unique_top.items():
        expected = row.get("reply_comment_total") or 0
        if expected < 1:
            continue
        replies, status = walk_pages(lambda cursor: collector.fetch(cid, cursor))
        status.update(parent_id=cid, advertised_reply_count=expected,
                      count_matches_advertised=status["distinct_ids"] == expected)
        threads.append(status)
        for reply in replies:
            if not reply.get("cid"):
                continue
            rid = str(reply["cid"])
            normalized[rid] = normalize_comment(reply, args.video_id, args.video_url,
                                                collector.observed.get((cid, rid), now()), cid)
    normalized_path.parent.mkdir(parents=True, exist_ok=True)
    with normalized_path.open("w") as handle:
        for row in sorted(normalized.values(), key=lambda row: (row["created_at_utc"] or "", row["comment_id"])):
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    manifest = {
        "video_id": args.video_id, "video_url": args.video_url, "started_at_utc": started,
        "finished_at_utc": now(), "script": str(Path(__file__).resolve()),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "authentication": "none; no user cookies, sessions, login, proxy rotation or captcha solving",
        "top_level": top_status, "threads": threads, "network_requests": collector.network_count,
        "stopped_after_error": collector.blocked, "all_reply_threads_paginated": all(t["complete_pagination"] for t in threads),
        "normalized_records": len(normalized), "top_level_unique": len(unique_top),
        "reply_unique": sum(row["parent_id"] is not None for row in normalized.values()),
        "normalized_file": str(normalized_path), "calls": collector.calls,
        "limitations": ["Live ranked public comments; pagination exhaustion is not completeness of historical/all comments.",
                        "Totals can include replies and drift during collection; deleted, filtered or private comments are unavailable.",
                        "Advertised reply counts and returned counts may differ; per-thread checks are retained.",
                        "No author profile fields in normalized output; public source bodies remain in ignored data/raw."],
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({key: manifest[key] for key in ["video_id", "network_requests", "top_level_unique", "reply_unique", "normalized_records", "all_reply_threads_paginated", "stopped_after_error"]}))


if __name__ == "__main__":
    main()
