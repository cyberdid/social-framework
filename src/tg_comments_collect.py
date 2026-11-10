"""Collect public discussion comments under relevant Telegram channel posts (free Telegram API, read-only).

Privacy: commenters are pseudonymised at collection time (HMAC-SHA256, secret salt ~/.config/social/pseudo_salt);
only boolean/profile-shape features are kept (bot flag, premium, verified, scam, fake, has_username,
default-looking username, has_photo, name length bucket). No names, usernames or user IDs are written.

Usage: python src/tg_comments_collect.py continuation_20261105 [--max-per-post 3000]
Out:   data/raw/telegram_comments/<UTC>/comments.jsonl, threads.jsonl, manifest.json

Reaction fields preserve the legacy integer `reactions` plus typed aggregate
`reaction_breakdown`, `reaction_state`, and `reaction_observed_at`. Reactor
identities are never serialized; `MessageReactions.min` is marked incomplete.
"""
import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from telethon import TelegramClient, errors, types, utils

ROOT = Path(__file__).resolve().parents[1]
CFG = Path.home() / ".config/social"


def env():
    return dict(l.split("=", 1) for l in (CFG / "telegram.env").read_text().splitlines() if "=" in l)


def salt():
    p = CFG / "pseudo_salt"
    if not p.exists():
        os.umask(0o077)
        p.write_text(secrets.token_hex(32))
    return bytes.fromhex(p.read_text().strip())


def pseudo(kind, pid, key):
    return f"{kind}_" + hmac.new(key, f"{kind}:{pid}".encode(), hashlib.sha256).hexdigest()[:12]


def sender_features(s):
    if s is None:
        return {"sender_type": "unknown"}
    if isinstance(s, types.User):
        un = s.username or ""
        name = f"{s.first_name or ''} {s.last_name or ''}".strip()
        return {"sender_type": "user", "bot": bool(s.bot), "premium": bool(getattr(s, "premium", False)),
                "verified": bool(s.verified), "scam": bool(s.scam), "fake": bool(s.fake),
                "deleted": bool(s.deleted), "has_username": bool(un),
                "username_digits_tail": bool(re.search(r"\d{4,}$", un)),
                "has_photo": s.photo is not None and not isinstance(s.photo, types.UserProfilePhotoEmpty),
                "name_len": len(name), "name_has_digits": bool(re.search(r"\d", name))}
    if isinstance(s, types.Channel):
        return {"sender_type": "channel", "broadcast": bool(s.broadcast), "verified": bool(s.verified),
                "scam": bool(s.scam), "fake": bool(s.fake), "has_username": bool(s.username)}
    return {"sender_type": type(s).__name__}


def serialize_reaction_snapshot(reactions, observed_at_utc):
    """Serialize aggregate reaction counts without retaining reactor identities."""
    if reactions is None:
        return {
            "legacy_total": 0,
            "reaction_breakdown": None,
            "reaction_state": "not_reported",
            "reaction_observed_at": observed_at_utc,
        }

    breakdown = []
    for result in getattr(reactions, "results", None) or []:
        reaction = result.reaction
        if isinstance(reaction, types.ReactionEmoji):
            kind, key = "emoji", reaction.emoticon
        elif isinstance(reaction, types.ReactionCustomEmoji):
            kind, key = "custom_emoji", f"document:{int(reaction.document_id)}"
        elif isinstance(reaction, types.ReactionPaid):
            kind, key = "paid", "paid"
        else:
            kind, key = "unknown", type(reaction).__name__

        count = result.count
        if type(count) is not int or count < 0:
            raise ValueError("reaction count must be a non-negative integer")
        breakdown.append({"kind": kind, "key": key, "count": count})

    total = sum(row["count"] for row in breakdown)
    if bool(getattr(reactions, "min", False)):
        state = "current_user_excluded"
    else:
        state = "available" if breakdown else "empty"
    return {
        "legacy_total": total,
        "reaction_breakdown": breakdown,
        "reaction_state": state,
        "reaction_observed_at": observed_at_utc,
    }


async def main_async(a):
    e = env()
    key = salt()
    posts = [json.loads(l) for l in (ROOT / "data/processed" / a.dataset / "posts_clean.jsonl").open()]
    targets = [p for p in posts if p.get("relevant") and (p.get("replies") or 0) > 0]
    targets.sort(key=lambda p: -(p.get("replies") or 0))
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "data/raw/telegram_comments" / now
    out.mkdir(parents=True)
    fc = (out / "comments.jsonl").open("w", encoding="utf-8")
    ft = (out / "threads.jsonl").open("w", encoding="utf-8")
    client = TelegramClient(e["TG_SESSION"], int(e["TG_API_ID"]), e["TG_API_HASH"], flood_sleep_threshold=600)
    await client.connect()
    if not await client.is_user_authorized():
        raise SystemExit("Telegram session not authorized")
    totals = {"threads": 0, "complete": 0, "capped": 0, "errors": 0, "comments": 0}
    try:
        for i, p in enumerate(targets, 1):
            th = {"post_url": p["url"], "channel": p["channel"], "msg_id": p["msg_id"],
                  "replies_counter_at_snapshot": p.get("replies"), "started_at": datetime.now(timezone.utc).isoformat()}
            n = 0
            try:
                ent = await client.get_input_entity(p["channel"])
                async for m in client.iter_messages(ent, reply_to=p["msg_id"], limit=a.max_per_post):
                    if not isinstance(m, types.Message):
                        continue
                    s = await m.get_sender()
                    sid = utils.get_peer_id(m.from_id) if m.from_id else (utils.get_peer_id(m.peer_id) if m.post else None)
                    fetched_at = datetime.now(timezone.utc).isoformat()
                    reaction_snapshot = serialize_reaction_snapshot(m.reactions, fetched_at)
                    rec = {"post_url": p["url"], "channel": p["channel"], "post_msg_id": p["msg_id"],
                           "discussion_chat": pseudo("chat", utils.get_peer_id(m.peer_id), key), "comment_id": m.id,
                           "date": m.date.isoformat(), "edit_date": m.edit_date.isoformat() if m.edit_date else None,
                           "text": m.message or "", "has_media": m.media is not None,
                           "reply_to_comment": getattr(m.reply_to, "reply_to_msg_id", None),
                           "is_top_level": getattr(m.reply_to, "reply_to_top_id", None) is None,
                           "forwarded": m.fwd_from is not None,
                           "reactions": reaction_snapshot["legacy_total"],
                           "reaction_breakdown": reaction_snapshot["reaction_breakdown"],
                           "reaction_state": reaction_snapshot["reaction_state"],
                           "reaction_observed_at": reaction_snapshot["reaction_observed_at"],
                           "sender": pseudo("acct", sid, key) if sid else None, **sender_features(s),
                           "fetched_at": fetched_at}
                    fc.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    n += 1
                th["status"] = "capped" if n >= a.max_per_post else "complete"
            except (errors.MsgIdInvalidError, errors.ChannelPrivateError, errors.ChatAdminRequiredError, ValueError) as ex:
                th["status"] = f"error:{type(ex).__name__}"
            th["collected"] = n
            th["finished_at"] = datetime.now(timezone.utc).isoformat()
            ft.write(json.dumps(th, ensure_ascii=False) + "\n")
            fc.flush(); ft.flush()
            totals["threads"] += 1
            totals["comments"] += n
            totals["complete" if th["status"] == "complete" else "capped" if th["status"] == "capped" else "errors"] += 1
            if i % 25 == 0:
                print(f"[{i}/{len(targets)}] comments={totals['comments']}", flush=True)
    finally:
        await client.disconnect()
        fc.close(); ft.close()
    man = {"collected_utc": now, "dataset": a.dataset, "targets": len(targets), "totals": totals,
           "max_per_post": a.max_per_post, "privacy": "HMAC pseudonyms; no names/usernames/ids stored",
           "sha256": {f: hashlib.sha256((out / f).read_bytes()).hexdigest() for f in ["comments.jsonl", "threads.jsonl"]}}
    (out / "manifest.json").write_text(json.dumps(man, indent=2))
    print(json.dumps(totals), "->", out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("dataset")
    ap.add_argument("--max-per-post", type=int, default=3000)
    asyncio.run(main_async(ap.parse_args()))
