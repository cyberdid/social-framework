"""Snapshot public Telegram posts about an incident (read-only).

Subcommands:
  probe    - check login, public-post-search quota, and how Telegram matches search terms
  collect  - per-channel keyword search over seed channels, then one snowball hop
             via forward origins; optional public post search within the free quota

Output (never edited afterwards):
  data/raw/telegram/<UTC snapshot ts>/messages.jsonl
  data/raw/telegram/<UTC snapshot ts>/channels.jsonl
  data/raw/telegram/<UTC snapshot ts>/manifest.json   (params, counts, sha256)

Only public channels are read. Views/forwards/reactions are as of `fetched_at`.
Paid search (Telegram Stars) is never used.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import telethon
from telethon import TelegramClient, errors, utils
from telethon.tl import types
from telethon.tl.functions.channels import (
    CheckSearchPostsFloodRequest,
    GetFullChannelRequest,
    SearchPostsRequest,
)

ROOT = Path(__file__).resolve().parents[1]
ENV = Path.home() / ".config/social/telegram.env"


def load_env() -> dict:
    out = {}
    for line in ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def read_list(path: Path) -> list[str]:
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(d):
    return d.astimezone(timezone.utc).isoformat() if d else None


def make_client(env) -> TelegramClient:
    return TelegramClient(env["TG_SESSION"], int(env["TG_API_ID"]), env["TG_API_HASH"],
                          flood_sleep_threshold=300)


class connected:
    """Connect without interactive start(): never prompts for phone/code."""

    def __init__(self, env):
        self.client = make_client(env)

    async def __aenter__(self):
        await self.client.connect()
        return self.client

    async def __aexit__(self, *exc):
        await self.client.disconnect()


def peer_id(p):
    return utils.get_peer_id(p) if p is not None else None


def msg_record(m: types.Message, chat, found_by: str) -> dict:
    fwd = m.fwd_from
    reactions = None
    if m.reactions and m.reactions.results:
        reactions = [{"r": getattr(r.reaction, "emoticon", None) or type(r.reaction).__name__,
                      "n": r.count} for r in m.reactions.results]
    urls = []
    for e in m.entities or []:
        if isinstance(e, types.MessageEntityTextUrl):
            urls.append(e.url)
    return {
        "chat_id": peer_id(m.peer_id),
        "chat_username": getattr(chat, "username", None),
        "chat_title": getattr(chat, "title", None),
        "msg_id": m.id,
        "date": iso(m.date),
        "edit_date": iso(m.edit_date),
        "text": m.message,
        "views": m.views,
        "forwards": m.forwards,
        "replies": m.replies.replies if m.replies else None,
        "reactions": reactions,
        "fwd_from": None if not fwd else {
            "from_id": peer_id(fwd.from_id),
            "from_name": fwd.from_name,
            "date": iso(fwd.date),
            "channel_post": fwd.channel_post,
        },
        "reply_to_msg_id": getattr(m.reply_to, "reply_to_msg_id", None),
        "grouped_id": m.grouped_id,
        "media": type(m.media).__name__ if m.media else None,
        "post_author": m.post_author,
        "urls": urls,
        "found_by": found_by,
        "fetched_at": iso(now_utc()),
    }


async def channel_record(client, ent) -> dict:
    rec = {
        "chat_id": peer_id(ent), "username": getattr(ent, "username", None),
        "title": getattr(ent, "title", None), "broadcast": getattr(ent, "broadcast", None),
        "megagroup": getattr(ent, "megagroup", None), "verified": getattr(ent, "verified", None),
        "scam": getattr(ent, "scam", None), "fake": getattr(ent, "fake", None),
        "date": iso(getattr(ent, "date", None)), "participants": None, "about": None,
        "fetched_at": iso(now_utc()),
    }
    try:
        full = await client(GetFullChannelRequest(ent))
        rec["participants"] = full.full_chat.participants_count
        rec["about"] = full.full_chat.about
    except Exception as e:  # noqa: BLE001 - record, don't stop the snapshot
        rec["error"] = f"{type(e).__name__}: {e}"
    return rec


async def search_channel(client, ent, kw, start, end):
    async for m in client.iter_messages(ent, search=kw, offset_date=end):
        if m.date < start:
            break
        if isinstance(m, types.Message):
            yield m


async def cmd_probe(args, env):
    async with connected(env) as client:
        if not await client.is_user_authorized():
            sys.exit("Not logged in: run src/tg_login.py in your own terminal first.")
        me = await client.get_me()
        print(f"logged in: premium={getattr(me, 'premium', None)}")
        try:
            flood = await client(CheckSearchPostsFloodRequest(query=args.term))
            print("public post search quota:", flood.to_dict())
        except Exception as e:  # noqa: BLE001
            print("public post search quota check failed:", type(e).__name__, e)
        ent = await client.get_entity(args.channel)
        start = datetime.fromisoformat(args.start)
        end = datetime.fromisoformat(args.end) if args.end else now_utc()
        for kw in [args.term, args.term[:6]]:
            n = 0
            async for _ in search_channel(client, ent, kw, start, end):
                n += 1
            print(f"@{args.channel} search {kw!r}: {n} posts in window")


async def cmd_collect(args, env):
    start = datetime.fromisoformat(args.start)
    end = datetime.fromisoformat(args.end) if args.end else now_utc()
    seeds = read_list(ROOT / args.seeds)
    kws = read_list(ROOT / args.keywords)
    snap = now_utc().strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "data/raw/telegram" / snap
    out.mkdir(parents=True, exist_ok=False)
    os.umask(0o077)

    msgs: dict[tuple, dict] = {}
    chans: dict[int, object] = {}
    unresolved: list[str] = []
    log = []

    async with connected(env) as client:
        if not await client.is_user_authorized():
            sys.exit("Not logged in: run src/tg_login.py in your own terminal first.")

        async def harvest(ent, method):
            cid = peer_id(ent)
            chans[cid] = ent
            n = 0
            for kw in kws:
                try:
                    async for m in search_channel(client, ent, kw, start, end):
                        key = (cid, m.id)
                        if key not in msgs:
                            msgs[key] = msg_record(m, ent, f"{method}:{kw}")
                            n += 1
                except (errors.ChannelPrivateError, errors.ChatAdminRequiredError) as e:
                    log.append({"chat_id": cid, "error": type(e).__name__})
                    break
            print(f"  {method} @{getattr(ent, 'username', cid)}: +{n}", flush=True)

        print(f"seeds: {len(seeds)}, keywords: {len(kws)}, window {iso(start)} .. {iso(end)}")
        for s in seeds:
            try:
                ent = await client.get_entity(s)
            except Exception as e:  # noqa: BLE001
                unresolved.append(s)
                log.append({"seed": s, "error": f"{type(e).__name__}: {e}"})
                print(f"  unresolved: {s}")
                continue
            await harvest(ent, "seed")

        if args.public_search:
            try:
                flood = await client(CheckSearchPostsFloodRequest(query=kws[0]))
                log.append({"public_search_quota": flood.to_dict()})
                free = getattr(flood, "remains", 0) or 0
            except Exception as e:  # noqa: BLE001
                free = 0
                log.append({"public_search_quota_error": f"{type(e).__name__}: {e}"})
            for kw in kws[: max(0, free)]:
                rate, opeer, oid = 0, types.InputPeerEmpty(), 0
                for _page in range(args.public_pages):
                    try:
                        res = await client(SearchPostsRequest(offset_rate=rate, offset_peer=opeer,
                                                              offset_id=oid, limit=100, query=kw))
                    except Exception as e:  # noqa: BLE001
                        log.append({"public_search": kw, "error": f"{type(e).__name__}: {e}"})
                        break
                    ents = {peer_id(c): c for c in res.chats}
                    got = [m for m in res.messages if isinstance(m, types.Message)]
                    for m in got:
                        if not (start <= m.date <= end):
                            continue
                        ent = ents.get(peer_id(m.peer_id))
                        if ent is not None:
                            chans.setdefault(peer_id(ent), ent)
                        key = (peer_id(m.peer_id), m.id)
                        msgs.setdefault(key, msg_record(m, ent, f"public_search:{kw}"))
                    if not got or not getattr(res, "next_rate", None):
                        break
                    last = got[-1]
                    rate, oid = res.next_rate, last.id
                    opeer = await client.get_input_entity(last.peer_id)
                print(f"  public_search {kw!r}: total msgs now {len(msgs)}", flush=True)

        if args.snowball:
            origins = {r["fwd_from"]["from_id"] for r in msgs.values()
                       if r["fwd_from"] and r["fwd_from"]["from_id"]}
            new = [o for o in origins if o not in chans]
            print(f"snowball: {len(new)} new channels from forward origins")
            for cid in new[: args.snowball_max]:
                try:
                    ent = await client.get_entity(cid)
                except Exception as e:  # noqa: BLE001
                    log.append({"snowball": cid, "error": f"{type(e).__name__}: {e}"})
                    continue
                if isinstance(ent, types.Channel):
                    await harvest(ent, "snowball")

        print(f"channel metadata for {len(chans)} channels...")
        chan_recs = []
        for ent in chans.values():
            if isinstance(ent, types.Channel):
                chan_recs.append(await channel_record(client, ent))

    def dump(name, rows):
        p = out / name
        with p.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return p

    files = [dump("messages.jsonl", sorted(msgs.values(), key=lambda r: r["date"])),
             dump("channels.jsonl", chan_recs),
             dump("errors.jsonl", log)]
    manifest = {
        "snapshot_utc": snap, "window": [iso(start), iso(end)],
        "seeds": seeds, "unresolved_seeds": unresolved, "keywords": kws,
        "public_search": args.public_search, "snowball": args.snowball,
        "counts": {"messages": len(msgs), "channels": len(chan_recs)},
        "telethon": telethon.__version__,
        "sha256": {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(manifest["counts"]), "->", out)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--channel", default="borshchagovkanews")
    p.add_argument("--term", default="Борщагівка")
    p.add_argument("--start", default="2026-11-02T00:00:00+02:00")
    p.add_argument("--end", default=None)
    c = sub.add_parser("collect")
    c.add_argument("--seeds", default="config/borshchahivka_seeds.txt")
    c.add_argument("--keywords", default="config/borshchahivka_keywords.txt")
    c.add_argument("--start", default="2026-11-02T00:00:00+02:00")
    c.add_argument("--end", default=None)
    c.add_argument("--public-search", action="store_true")
    c.add_argument("--public-pages", type=int, default=10)
    c.add_argument("--snowball", action="store_true")
    c.add_argument("--snowball-max", type=int, default=300)
    args = ap.parse_args()
    env = load_env()
    asyncio.run({"probe": cmd_probe, "collect": cmd_collect}[args.cmd](args, env))


if __name__ == "__main__":
    main()
