"""Merge immutable public Telegram snapshots; never connects to Telegram.

Run again after collectors finish: only API folders with a finished manifest
are admitted. API observations win over web, then the latest fetched_at wins.
messages_clean.jsonl retains every unique message; posts_clean.jsonl projects
known API albums to one caption member without summing member views. Web-only
records remain observations, not evidence that a message was deleted.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import prep
except ImportError:
    import prep

ROOT = Path(__file__).resolve().parents[2]
WEB_SNAPSHOTS = ('20261105T101013Z', '20261105T102914Z')
START = '2026-11-02T00:00:00+02:00'
END = '2026-11-05T10:10:13+00:00'
OUTPUT = 'continuation_20261105'


def timestamp(value):
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f'Explicit timezone required: {value!r}')
    return dt.astimezone(timezone.utc)


def username(value):
    return (value or '').lower().lstrip('@')


def channel_id(value):
    if value is None:
        return None
    value = int(value)
    return -value - 10**12 if value < -10**12 else abs(value)


def read_snapshot(root, folder, kind):
    """Read and verify bytes once, before decoding any records."""
    manifest_bytes = (folder / 'manifest.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    if not manifest.get('finished_utc'):
        raise ValueError(f'Unfinished manifest: {folder}')
    files = manifest.get('sha256', {})
    needed = ('posts.jsonl', 'channels.jsonl') if kind == 'telegram_web' else ('messages.jsonl', 'channels.jsonl')
    for name in needed:
        if name not in files:
            raise ValueError(f'Missing SHA-256 receipt for {folder / name}')
    if kind == 'telegram_api' and (folder / 'search.jsonl').exists() and 'search.jsonl' not in files:
        raise ValueError(f'Missing SHA-256 receipt for {folder / "search.jsonl"}')
    blobs, receipts = {}, []
    for name, expected in sorted(files.items()):
        path = folder / name
        if not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError(f'Unsafe manifest filename: {name}')
        blobs[name] = path.read_bytes()
        actual = hashlib.sha256(blobs[name]).hexdigest()
        if actual != expected:
            raise ValueError(f'SHA-256 mismatch: {path}')
        receipts.append({'path': str(path.relative_to(root)), 'expected': expected,
                         'actual': actual, 'verified': True, 'bytes': len(blobs[name])})
    receipts.append({'path': str((folder / 'manifest.json').relative_to(root)),
                     'actual': hashlib.sha256(manifest_bytes).hexdigest(),
                     'expected': None, 'verified': None,
                     'note': 'Manifest hash recorded; not independently signed.'})
    rows = {name: [(n, json.loads(line)) for n, line in enumerate(blob.decode('utf-8').splitlines(), 1)
                   if line.strip()] for name, blob in blobs.items() if name.endswith('.jsonl')}
    return dict(kind=kind, name=folder.name, folder=folder, manifest=manifest,
                rows=rows, receipts=receipts)


def identities(snapshots):
    """Resolve requested names and marked/ordinary channel IDs to latest names.

    A username observed on different channel IDs is ambiguous for legacy web
    records; fail explicitly instead of merging unrelated channels.
    """
    candidates, name_ids, aliases = defaultdict(list), defaultdict(set), []
    for snap in snapshots:
        if snap['kind'] != 'telegram_api':
            continue
        fallback = snap['manifest']['finished_utc']
        for _, row in snap['rows'].get('channels.jsonl', []):
            cid, name = channel_id(row.get('chat_id')), username(row.get('username'))
            if cid is not None and name:
                candidates[cid].append((timestamp(row.get('fetched_at') or fallback), name))
                name_ids[name].add(cid)
                if row.get('requested_username'):
                    name_ids[username(row['requested_username'])].add(cid)
        for name, status in snap['manifest'].get('channels', {}).items():
            canonical = username(status.get('canonical_username') or status.get('canonical'))
            cid = channel_id(status.get('channel_id'))
            if cid is not None and canonical:
                candidates[cid].append((timestamp(fallback), canonical))
                name_ids[username(name)].add(cid)
                name_ids[canonical].add(cid)
            elif canonical:
                aliases.append((username(name), canonical))
        for filename in ('messages.jsonl', 'search.jsonl'):
            for _, row in snap['rows'].get(filename, []):
                cid, name = channel_id(row.get('chat_id')), username(row.get('chat_username'))
                if cid is not None and name:
                    candidates[cid].append((timestamp(row.get('fetched_at') or fallback), name))
                    name_ids[name].add(cid)
    for name, ids in name_ids.items():
        if len(ids) > 1:
            raise ValueError(f'Ambiguous username {name!r} maps to channel IDs {sorted(ids)}')
    by_id = {cid: max(values)[1] for cid, values in candidates.items()}
    by_name = {name: by_id[next(iter(ids))] for name, ids in name_ids.items()}
    for old, new in aliases:
        if old != new:
            by_name[old] = new
    for old in list(by_name):
        seen, current = set(), old
        while current in by_name and by_name[current] != current:
            if current in seen:
                raise ValueError(f'Cyclic channel alias: {old}')
            seen.add(current)
            current = by_name[current]
        by_name[old] = current
    return by_name, by_id


def normalize(row, snap, filename, line, root, names, ids):
    api = snap['kind'] == 'telegram_api'
    original = username(row.get('chat_username') if api else row.get('channel'))
    cid = channel_id(row.get('chat_id')) if api else None
    name = ids.get(cid) if api and cid in ids else names.get(original, original)
    if not name:
        if cid is None:
            raise ValueError(f'No channel identity: {snap["name"]}/{filename}:{line}')
        name = f'channelid_{cid}'
    fetched = row.get('fetched_at') or snap['manifest']['finished_utc']
    provenance = {'kind': snap['kind'], 'snapshot': snap['name'],
                  'path': str((snap['folder'] / filename).relative_to(root)), 'line': line,
                  'fetched_at': fetched, 'original_channel': original or None,
                  'fetched_at_inferred_from_manifest': not bool(row.get('fetched_at')),
                  'original_chat_id': row.get('chat_id'), 'original_url': row.get('url'),
                  'found_by': row.get('found_by')}
    p = dict(row)
    p.update(channel=name, msg_id=int(row['msg_id']),
             date=timestamp(row['date']).isoformat(), text=row.get('text'),
             views=row.get('views'), fetched_at=fetched,
             url=f'https://t.me/{name}/{row["msg_id"]}' if not name.startswith('channelid_') else None,
             source='api' if api else 'web', source_snapshot=snap['name'],
             post_key=f'{name}/{row["msg_id"]}')
    if api:
        fwd = row.get('fwd_from') or {}
        fwd_name = ids.get(channel_id(fwd.get('from_id')))
        # Documents cannot be classified as videos from the recorded media class
        # alone. Preserve the API class and use the conservative document label.
        media = {'MessageMediaPhoto': 'photo', 'MessageMediaDocument': 'document',
                 'MessageMediaWebPage': 'webpage'}.get(row.get('media'), row.get('media'))
        p.update(edited=bool(row.get('edit_date')), views_raw=None,
                 fwd_from_url=f'https://t.me/{fwd_name}/{fwd["channel_post"]}'
                     if fwd_name and fwd.get('channel_post') else None,
                 fwd_from_name=fwd.get('from_name'), author=row.get('post_author'),
                 links=row.get('urls') or [], media_api=row.get('media'),
                 media=[media] if media else [],
                 n_media=int(bool(row.get('media'))),
                 reactions=[[r.get('r'), r.get('n')] for r in row.get('reactions') or []],
                 reply_to_url=f'https://t.me/{name}/{row["reply_to_msg_id"]}'
                     if row.get('reply_to_msg_id') else None)
    p.setdefault('grouped_id', None)
    p.setdefault('fwd_from_url', None)
    return p, provenance


def project_albums(messages):
    albums, publications = defaultdict(list), []
    for p in messages:
        if p['source'] == 'api' and p.get('grouped_id') is not None:
            albums[(p['channel'], str(p['grouped_id']))].append(p)
        else:
            publications.append(dict(p))
    for members in albums.values():
        # Prefer the caption; longest caption is deterministic if several exist.
        caption = min(members, key=lambda p: (-len((p.get('text') or '').strip()), p['msg_id']))
        p = dict(caption)
        p.update(album_member_ids=sorted(m['msg_id'] for m in members),
                 album_message_count=len(members), album_caption_member_id=caption['msg_id'],
                 album_first_message_date=min(m['date'] for m in members),
                 album_last_message_date=max(m['date'] for m in members),
                 album_sources=[s for m in members for s in m['sources']])
        publications.append(p)
    stats = {'known_groups': len(albums), 'multi_message_groups': sum(len(v) > 1 for v in albums.values()),
             'known_group_messages': sum(map(len, albums.values())),
             'collapsed_messages': sum(len(v) - 1 for v in albums.values()),
             'groups_without_caption': sum(not any((p.get('text') or '').strip() for p in v) for v in albums.values()),
             'groups_with_multiple_captions': sum(sum(bool((p.get('text') or '').strip()) for p in v) > 1 for v in albums.values()),
             'web_unknown_groupid_messages': sum(p['source'] == 'web' and p.get('grouped_id') is None for p in messages),
             'policy': 'One known API grouped_id per channel; select longest caption, tie lowest msg_id; '
                       'keep its timestamp and views, never sum member views. Observed groups may be partial. '
                       'Unknown web grouped_id is not inferred.'}
    return sorted(publications, key=lambda p: (p['date'], p['channel'], p['msg_id'])), stats


def annotate(p, boiler):
    p['text_clean'] = '\n'.join(line for line in (p.get('text') or '').splitlines()
                                if prep.norm_line(line) not in boiler.get(p['channel'], set())).strip()
    p['t_kyiv'] = timestamp(p['date']).astimezone(prep.KYIV).isoformat()
    tc = p['text_clean']
    p['place'] = bool(prep.PLACE.search(prep.KYIV_PLACES.sub(' ', tc)))
    p['relevant'], p['relevance_reason_code'] = prep.relevance_decision(tc, p['t_kyiv'])
    p['lang'] = prep.lang_of(tc)
    p['fwd_channel'] = None
    match = re.match(r'https?://t\.me/([A-Za-z0-9_]+)/(\d+)', p.get('fwd_from_url') or '')
    if match:
        p['fwd_channel'], p['fwd_msg_id'] = match.group(1).lower(), int(match.group(2))


def build(root=ROOT, web_snapshots=WEB_SNAPSHOTS, output_name=OUTPUT):
    root = Path(root)
    start, end = timestamp(START), timestamp(END)
    snapshots = [read_snapshot(root, root / 'data/raw/telegram_web' / name, 'telegram_web')
                 for name in web_snapshots]
    skipped = []
    api_root = root / 'data/raw/telegram_api'
    for folder in sorted(api_root.iterdir()) if api_root.exists() else []:
        if not folder.is_dir():
            continue
        manifest = folder / 'manifest.json'
        if not manifest.exists() or not json.loads(manifest.read_text()).get('finished_utc'):
            skipped.append(folder.name)
            continue
        snapshots.append(read_snapshot(root, folder, 'telegram_api'))
    names, ids = identities(snapshots)
    observations, metadata, per_source, coverage = defaultdict(list), defaultdict(list), [], []
    excluded = 0
    for snap in snapshots:
        counts = Counter()
        source_keys = set()
        for filename in ('posts.jsonl', 'messages.jsonl', 'search.jsonl'):
            for line, row in snap['rows'].get(filename, []):
                counts['raw_message_observations'] += 1
                if not start <= timestamp(row['date']) < end:
                    counts['excluded_outside_window'] += 1
                    excluded += 1
                    continue
                p, source = normalize(row, snap, filename, line, root, names, ids)
                key = p['channel'], p['msg_id']
                rank = (p['source'] == 'api', timestamp(p['fetched_at']), snap['name'], filename, line)
                observations[key].append((rank, p, source))
                source_keys.add(key)
                counts['observations_in_window'] += 1
        for line, row in snap['rows'].get('channels.jsonl', []):
            api = snap['kind'] == 'telegram_api'
            name = username(row.get('username') if api else row.get('channel'))
            name = ids.get(channel_id(row.get('chat_id')), names.get(name, name)) if api else names.get(name, name)
            if name:
                metadata[name].append((api, row.get('fetched_at') or snap['manifest']['finished_utc'],
                                       snap['name'], row, str((snap['folder'] / 'channels.jsonl').relative_to(root)), line))
        per_source.append({'kind': snap['kind'], 'snapshot': snap['name'], **counts,
                           'unique_messages_in_window': len(source_keys)})
        m = snap['manifest']
        coverage.append({'kind': snap['kind'], 'snapshot': snap['name'], 'window': m.get('window'),
                         'channels': m.get('channels', {}),
                         'requested_channels': m.get('requested_channels', m.get('seeds', [])),
                         'unvisited': m.get('unvisited', []), 'unvisited_queue': m.get('unvisited_queue'),
                         'discovery': m.get('discovery', [])})
    messages = []
    for key, versions in observations.items():
        _, selected, _ = max(versions, key=lambda v: v[0])
        selected['sources'] = [source for _, _, source in sorted(versions, key=lambda v: v[0])]
        kinds = {source['kind'] for source in selected['sources']}
        selected['source_presence'] = 'overlap' if len(kinds) > 1 else ('api_only' if selected['source'] == 'api' else 'web_only')
        messages.append(selected)
    messages.sort(key=lambda p: (p['date'], p['channel'], p['msg_id']))
    publications, album_stats = project_albums(messages)
    by_ch = defaultdict(list)
    for p in publications:
        by_ch[p['channel']].append(p)
    boiler = prep.strip_boilerplate(by_ch)
    for p in messages + publications:
        annotate(p, boiler)
    features = []
    for name in sorted(set(metadata) | set(by_ch)):
        ps = by_ch[name]
        metas = sorted(metadata[name], key=lambda m: (m[0], timestamp(m[1]), m[2]), reverse=True)
        def field(web, api=None):
            for is_api, _, _, row, _, _ in metas:
                value = row.get(api if is_api and api else web)
                if value is not None:
                    return value
            return None
        langs = Counter(p['lang'] for p in ps)
        rel = [p for p in ps if p['relevant']]
        features.append({'channel': name, 'title': field('title'), 'subscribers': field('subscribers', 'participants'),
                         'depth': field('depth'), 'via': field('via'), 'posts_window': len(ps),
                         'posts_relevant': len(rel), 'views_relevant': sum(p.get('views') or 0 for p in rel),
                         'share_uk': round(langs['uk'] / max(1, langs['uk'] + langs['ru']), 3),
                         'first_relevant_kyiv': min((p['t_kyiv'] for p in rel), default=None),
                         'boilerplate_lines': len(boiler.get(name, ())),
                         'description': (field('description', 'about') or '')[:300].replace('\n', ' '),
                         'metadata_sources': json.dumps([{'kind': 'telegram_api' if a else 'telegram_web',
                             'fetched_at': b, 'snapshot': c, 'path': e, 'line': f} for a,b,c,_,e,f in metas])})
    features.sort(key=lambda r: (-r['views_relevant'], r['channel']))
    for counts in per_source:
        counts['selected_messages'] = sum(p['source_snapshot'] == counts['snapshot'] and
            ('telegram_api' if p['source'] == 'api' else 'telegram_web') == counts['kind'] for p in messages)
        counts['selected_publications'] = sum(p['source_snapshot'] == counts['snapshot'] and
            ('telegram_api' if p['source'] == 'api' else 'telegram_web') == counts['kind'] for p in publications)
    complete, requested = set(), set()
    for cov in coverage:
        if cov['kind'] != 'telegram_api':
            continue
        requested.update(names.get(username(n), username(n)) for n in cov['requested_channels'])
        window = cov['window'] or []
        covers_window = len(window) == 2 and timestamp(window[0]) <= start and timestamp(window[1]) >= end
        for n, status in cov['channels'].items():
            if status.get('status') == 'complete' and covers_window:
                complete.add(names.get(username(n), username(n)))
    presence = Counter(p['source_presence'] for p in messages)
    quality = {'created_at': datetime.now(timezone.utc).isoformat(),
               'window': [START, END], 'start_inclusive': True, 'end_exclusive': True,
               'selection_policy': 'API preferred; latest fetched_at within source type; canonical username + msg_id',
               'unit': 'posts_clean = publications; messages_clean = unique messages before album projection',
               'web_only_note': 'Retained public web observations; absence from API is not a deletion finding.',
               'raw_hashes': [r for s in snapshots for r in s['receipts']],
               'all_declared_hashes_verified': True, 'per_source': per_source, 'coverage': coverage,
               'skipped_unfinished_api_snapshots': skipped,
               'aliases': {k: v for k, v in sorted(names.items()) if k != v},
               'api_complete_channels': sorted(complete),
               'api_unresolved_or_unvisited': sorted(requested - complete),
               'excluded_outside_window': excluded,
               'observations_in_window': sum(len(v) for v in observations.values()),
               'unique_messages': len(messages), 'publications': len(publications),
               'duplicate_observations_removed': sum(len(v) - 1 for v in observations.values()),
               'overlap_messages': presence['overlap'], 'api_only_messages': presence['api_only'],
               'web_only_messages': presence['web_only'], 'channels_with_messages': len(by_ch),
               'relevant_publications': sum(p['relevant'] for p in publications),
               'albums': album_stats,
               'missing': {'messages_views': sum(p.get('views') is None for p in messages),
                           'messages_text': sum(not (p.get('text') or '').strip() for p in messages),
                           'publications_views': sum(p.get('views') is None for p in publications),
                           'publications_text': sum(not (p.get('text') or '').strip() for p in publications)}}
    out = root / 'data/processed' / output_name
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in (('messages_clean.jsonl', messages), ('posts_clean.jsonl', publications)):
        temp = out / (name + '.tmp')
        with temp.open('w', encoding='utf-8') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
        temp.replace(out / name)
    columns = ['channel', 'title', 'subscribers', 'depth', 'via', 'posts_window', 'posts_relevant',
               'views_relevant', 'share_uk', 'first_relevant_kyiv', 'boilerplate_lines', 'description', 'metadata_sources']
    temp = out / 'channels_features.csv.tmp'
    with temp.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(features)
    temp.replace(out / 'channels_features.csv')
    temp = out / 'data_quality.json.tmp'
    temp.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(out / 'data_quality.json')
    return quality


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--output', default=OUTPUT)
    args = parser.parse_args()
    result = build(args.root, output_name=args.output)
    print(json.dumps({key: result[key] for key in ('unique_messages', 'publications',
                     'relevant_publications', 'overlap_messages', 'api_only_messages',
                     'web_only_messages', 'skipped_unfinished_api_snapshots')}, ensure_ascii=False, indent=2))
