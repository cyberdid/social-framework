"""Reproduce metadata selection and timestamp-independent comment measurements.

This is a retrospective evidence audit, not a preregistered bot classifier.
Raw snapshots remain untouched. No per-comment author IDs are exported.
"""
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo
import csv
import hashlib
import json
import re

ROOT = Path(__file__).resolve().parents[2]
SNAP = ROOT / 'data/raw/youtube/20261106T072121Z'
OUT = ROOT / 'reports/youtube-comments/20261106T072121Z'
PATTERN = r'бор[щш]|borsh'


def read_rows(path):
    return [json.loads(line) for line in path.open(encoding='utf-8')]


def norm(text):
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', (text or '').lower())).strip()


def comments_summary(rows):
    counts = Counter(c['author'] for c in rows if c.get('author'))
    values = sorted(counts.values())
    n, total = len(values), sum(values)
    by_text = defaultdict(list)
    for c in rows:
        if c.get('author'):
            by_text[norm(c.get('text'))].append(c)
    exact = {t: cs for t, cs in by_text.items()
             if len(t) >= 20 and len({c['author'] for c in cs}) >= 3}
    signatures = Counter()
    for cs in exact.values():
        threads = Counter(c['video_id'] for c in cs)
        if len(threads) >= 3 and max(threads.values()) == 1:
            signatures[tuple(sorted(threads))] += 1
    per_author_text = defaultdict(lambda: defaultdict(set))
    for c in rows:
        if c.get('author') and len(norm(c.get('text'))) >= 20:
            per_author_text[c['author']][norm(c['text'])].add(c['video_id'])
    return {
        'comments': len(rows), 'unique_comment_ids': len({c['comment_id'] for c in rows}),
        'authors': n, 'videos': len({c['video_id'] for c in rows}),
        'top10_share': sum(sorted(values, reverse=True)[:10]) / total,
        'top1pct_share_floor': sum(sorted(values, reverse=True)[:max(1, n // 100)]) / total,
        'gini': 2 * sum((i + 1) * x for i, x in enumerate(values)) / (n * total) - (n + 1) / n,
        'max_comments_per_author': max(values),
        'exact_copy_groups_ge3_authors': len(exact),
        'exact_copy_comments': sum(map(len, exact.values())),
        'exact_copy_texts': sorted(exact),
        'template_pools_ge10_phrases': sum(v >= 10 for v in signatures.values()),
        'authors_same_text_ge3_videos': sum(any(len(s) >= 3 for s in ts.values()) for ts in per_author_text.values()),
        'time_signals': 'not_assessed_approximate_timestamps',
        'profile_signals': 'not_assessed_missing_fields',
        'composite_multisignal_bot_scores': 'not_assessed_invalid_time_and_profile_inputs',
    }


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    videos, comments = read_rows(SNAP / 'videos.jsonl'), read_rows(SNAP / 'comments.jsonl')
    manifest = json.loads((SNAP / 'manifest.json').read_text())
    hashes = {name: hashlib.sha256((SNAP / name).read_bytes()).hexdigest() == expected
              for name, expected in manifest['sha256'].items()}
    if not all(hashes.values()):
        raise ValueError('Raw snapshot hash mismatch')
    selected = [v for v in videos if v['in_window'] and
                re.search(PATTERN, (v.get('title') or '') + ' ' + (v.get('description') or ''), re.I)]
    ids = {v['id'] for v in selected}
    earliest = min((v for v in selected if v.get('timestamp')), key=lambda v: v['timestamp'])
    fetched = [c['fetched_at'] for c in comments]
    result = {
        'source': str(SNAP.relative_to(ROOT)), 'manifest_hash_checks': hashes,
        'selection': {'pattern': PATTERN, 'fields': ['title', 'description'],
                      'window': 'upload_date >= 20261102; upper bound is snapshot collection',
                      'status': 'lexical_metadata_selection_not_full_manual_content_validation',
                      'candidates': len(videos), 'in_window': sum(v['in_window'] for v in videos),
                      'matched': len(selected), 'channels_by_id': len({v['channel_id'] for v in selected}),
                      'views_snapshot_sum': sum(v.get('view_count') or 0 for v in selected),
                      'was_live': sum(bool(v.get('was_live')) for v in selected),
                      'by_upload_date': dict(sorted(Counter(v['upload_date'] for v in selected).items())),
                      'earliest_id': earliest['id'],
                      'earliest_timestamp_kyiv': datetime.fromtimestamp(earliest['timestamp'], ZoneInfo('Europe/Kyiv')).isoformat()},
        'comments_fetch_range_utc': [min(fetched), max(fetched)],
        'all_collected_comments': comments_summary(comments),
        'metadata_matched_comments': comments_summary([c for c in comments if c['video_id'] in ids]),
        'limitations': ['Selection can miss relevant videos without a place-name and include incidental mentions.',
                        'No unobserved platform-wide initiation or influence inference.',
                        'Collection cap and ranking prevent estimating all comments or bot prevalence.'],
    }
    tg = ROOT / 'data/raw/telegram_comments/20261106T071956Z/comments.jsonl'
    groups = ROOT / 'reports/telegram-comments/20261106T071956Z/copypasta_groups.csv'
    threads = {'https://t.me/insiderukr/137695', 'https://t.me/truexanewsua/147862',
               'https://t.me/uaonlii/135660', 'https://t.me/uaonlii/135665'}
    with groups.open(encoding='utf-8') as f:
        texts = {r['text'] for r in csv.DictReader(f) if int(r['threads']) >= 3 and
                 set(r['channels'].split(';')) <= {'insiderukr', 'truexanewsua', 'uaonlii'}}
    campaign = [c for c in read_rows(tg) if c['post_url'] in threads and norm(c.get('text')) in texts]
    authors = {c['sender']: c for c in campaign if c.get('sender')}
    result['telegram_campaign_denominators'] = {
        'source_comments': str(tg.relative_to(ROOT)), 'source_groups': str(groups.relative_to(ROOT)),
        'phrases': len(texts), 'comments': len(campaign), 'authors': len(authors),
        'no_username_authors': sum(not c['has_username'] for c in authors.values()),
        'no_username_comments': sum(not c['has_username'] for c in campaign),
    }
    (OUT / 'evidence_audit.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    with (OUT / 'metadata_selection.csv').open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['video_id', 'url', 'upload_date', 'in_window', 'place_name_match', 'selected', 'title'])
        for v in videos:
            matched = bool(re.search(PATTERN, (v.get('title') or '') + ' ' + (v.get('description') or ''), re.I))
            writer.writerow([v['id'], v['webpage_url'], v.get('upload_date'), v['in_window'], matched, v['id'] in ids, v.get('title')])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
