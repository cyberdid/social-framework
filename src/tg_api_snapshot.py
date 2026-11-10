"""Read public channel timelines through an authorized Telegram session.

No dialogs, contacts, private groups, joining, messages, or paid search.
An immutable snapshot keeps per-channel coverage and SHA-256 receipts.
"""
import argparse
import asyncio
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from telethon import errors, types
from telethon.tl.functions.channels import CheckSearchPostsFloodRequest, SearchPostsRequest

from tg_collect import ROOT, channel_record, iso, load_env, make_client, msg_record, now_utc


async def harvest_timeline(client, ent, start, end, cap):
    messages = []
    seen = 0
    try:
        async for m in client.iter_messages(ent, offset_date=end, limit=cap, wait_time=0.6):
            seen += 1
            if m.date < start:
                return messages, 'complete'
            if start <= m.date < end:
                messages.append(m)
            if seen >= cap:
                return messages, 'cap_reached'
        return messages, 'complete'
    except errors.FloodWaitError as exc:
        return messages, f'FloodWaitError:{exc.seconds}'
    except errors.RPCError as exc:
        return messages, type(exc).__name__


async def collect(args):
    os.umask(0o077)
    start, end = datetime.fromisoformat(args.start), datetime.fromisoformat(args.end)
    if start.tzinfo is None or end.tzinfo is None or start >= end:
        raise ValueError('Explicit timezone and start < end required')
    seeds = [s.strip().lower().lstrip('@') for s in Path(args.seeds).read_text().splitlines()
             if s.strip() and not s.lstrip().startswith('#')]
    seeds = list(dict.fromkeys(seeds))
    out = ROOT / 'data/raw/telegram_api' / now_utc().strftime('%Y%m%dT%H%M%SZ')
    out.mkdir(parents=True, exist_ok=False)
    client = make_client(load_env())
    client.flood_sleep_threshold = 0
    client._request_retries = 1
    status, records, discovery = {}, {}, []
    targets = dict.fromkeys(seeds)
    files = {name: (out / name).open('w', encoding='utf-8')
             for name in ('messages.jsonl', 'channels.jsonl', 'search.jsonl')}
    def write(name, row):
        files[name].write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        files[name].flush()
    try:
        await asyncio.wait_for(client.connect(), 20)
        if not await client.is_user_authorized():
            raise RuntimeError('Session is not authorized')
        if args.public_search:
            for term in ['Борщаговка', 'Борщагівка', 'Концлагерь на Борщаговке']:
                rec = {'query': term, 'fetched_at': iso(now_utc())}
                try:
                    quota = await client(CheckSearchPostsFloodRequest(query=term))
                    rec['quota'] = quota.to_dict()
                    # Search with an explicit zero Stars spending limit.
                    if not (getattr(quota, 'query_is_free', False) or (getattr(quota, 'remains', 0) or 0) > 0):
                        rec['status'] = 'no_free_quota'
                    else:
                        res = await client(SearchPostsRequest(offset_rate=0, offset_peer=types.InputPeerEmpty(),
                                                             offset_id=0, limit=100, query=term, allow_paid_stars=0))
                        rec['status'] = 'ok'
                        rec['returned'] = len(res.messages)
                        rec['total_count'] = getattr(res, 'count', None)
                        rec['coverage'] = 'discovery_only_first_page; not an exhaustive global sample'
                        ents = {e.id: e for e in res.chats if isinstance(e, types.Channel) and e.broadcast and e.username}
                        for m in res.messages:
                            ent = ents.get(getattr(m.peer_id, 'channel_id', None))
                            if ent and start <= m.date < end:
                                targets.setdefault(ent.username.lower())
                                write('search.jsonl', msg_record(m, ent, 'global_discovery:' + term))
                except errors.RPCError as exc:
                    rec['status'] = type(exc).__name__
                discovery.append(rec)
                print('search', term, rec['status'], flush=True)
        for i, name in enumerate(targets, 1):
            try:
                # Resolve from the session cache when available, to avoid repeat username RPCs.
                peer = await asyncio.wait_for(client.get_input_entity(name), 25)
                ent = await asyncio.wait_for(client.get_entity(peer), 25)
                if not isinstance(ent, types.Channel) or not ent.broadcast or not ent.username:
                    status[name] = {'status': 'not_public_broadcast_channel'}
                    continue
                if ent.id in records:
                    status[name] = {'status': 'alias', 'canonical': records[ent.id]}
                    continue
                records[ent.id] = ent.username.lower()
                meta = await channel_record(client, ent)
                meta['requested_username'] = name
                write('channels.jsonl', meta)
                messages, st = await harvest_timeline(client, ent, start, end, args.cap)
                for m in messages:
                    if isinstance(m, types.Message):
                        write('messages.jsonl', msg_record(m, ent, 'full_timeline'))
                status[name] = {'status': st, 'posts': len(messages), 'channel_id': ent.id,
                                'canonical_username': ent.username.lower()}
                print(f'[{i}/{len(targets)}] @{name}: {len(messages)} {st}', flush=True)
                if st.startswith('FloodWaitError:'):
                    print('Stopped at rate limit; completed channels retained.', flush=True)
                    break
            except (errors.RPCError, asyncio.TimeoutError, ValueError) as exc:
                status[name] = {'status': type(exc).__name__}
                if isinstance(exc, errors.FloodWaitError):
                    status[name]['wait_seconds'] = exc.seconds
                print(f'[{i}/{len(targets)}] @{name}: {type(exc).__name__}', flush=True)
                if isinstance(exc, errors.FloodWaitError):
                    break
            await asyncio.sleep(args.delay)
    finally:
        await client.disconnect()
        for f in files.values():
            f.close()
        manifest = {'snapshot_utc': out.name, 'finished_utc': iso(now_utc()), 'window': [iso(start), iso(end)],
                    'end_exclusive': True, 'method': 'full public broadcast channel timelines, Telegram API',
                    'cap_per_channel': args.cap, 'requested_channels': list(targets), 'channels': status,
                    'unvisited': [n for n in targets if n not in status], 'discovery': discovery,
                    'paid_search': False,
                    'sha256': {name: hashlib.sha256((out / name).read_bytes()).hexdigest() for name in files}}
        (out / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    print('SNAPSHOT', out, flush=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--seeds', required=True)
    p.add_argument('--start', default='2026-11-02T00:00:00+02:00')
    p.add_argument('--end', default='2026-11-05T10:10:13+00:00')
    p.add_argument('--cap', type=int, default=10000)
    p.add_argument('--delay', type=float, default=3.0)
    p.add_argument('--public-search', action='store_true')
    asyncio.run(collect(p.parse_args()))
