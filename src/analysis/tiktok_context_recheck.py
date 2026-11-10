"""Retrospective candidate recheck and explicit clock ledger for the user batch."""
import csv
import json
import statistics
from pathlib import Path
from collections import Counter
from datetime import datetime, timezone
from src.analysis.contextual_candidates import scan

ROOT = Path(__file__).resolve().parents[2]


def main():
    old = json.loads((ROOT / 'data/processed/tiktok/manual_batch_20261106T0905Z/contextual_candidates_with_context.summary.json').read_text())
    inputs = [ROOT / x['path'] for x in old['inputs']]
    ledger, cases = [], {}
    with (ROOT / 'reports/tiktok/manual-batch-20261106-posts.csv').open() as f:
        posts = list(csv.DictReader(f))
    for p in posts:
        meta_path = ROOT / f"data/raw/tiktok_metadata/20261106T0848Z/{p['post_id']}.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        caption = meta.get('description') or meta.get('title') or ''
        cases[p['url']] = {
            'context_text': caption,
            'review_scope': 'user_selected_case_batch; post_relevance_not_independently_validated',
            'parent_caption_source': str(meta_path.relative_to(ROOT)) if caption else None,
        }
        comment_times = []
        for path in inputs:
            if p['post_id'] in str(path):
                comment_times.extend(json.loads(line).get('fetched_at_utc') for line in path.open())
        comment_times = [x for x in comment_times if x]
        stamp = meta.get('timestamp')
        id_stamp = datetime.fromisoformat(p['id_time_estimate_kyiv'])
        platform_stamp = datetime.fromtimestamp(stamp, timezone.utc) if stamp else None
        ledger.append({
            'url': p['url'], 'post_id': p['post_id'], 'type': p['type'],
            'id_publication_time_estimate_kyiv': p['id_time_estimate_kyiv'],
            'platform_publication_timestamp_utc': datetime.fromtimestamp(stamp, timezone.utc).isoformat() if stamp else None,
            'platform_time_source': str(meta_path.relative_to(ROOT)) if stamp else None,
            'metadata_minus_id_seconds': (platform_stamp - id_stamp).total_seconds() if platform_stamp else None,
            'timestamp_extractor_mapping': 'yt-dlp TikTok create_time/createTime; metadata, not filming time' if stamp else None,
            'event_time_verified': None, 'filming_time_verified': None,
            'comments_collected_first_utc': min(comment_times) if comment_times else None,
            'comments_collected_last_utc': max(comment_times) if comment_times else None,
            'clock_note': 'Publication and ID time do not establish event/filming time. No media geolocation performed.',
        })
    out = ROOT / 'data/processed/tiktok/context_recheck_20261106'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'known_cases.json').write_text(json.dumps(cases, ensure_ascii=False, indent=2))
    summary = scan(inputs, out / 'candidates', cases)
    with (ROOT / 'reports/tiktok/publication-event-filming-times-20261106.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(ledger[0])); w.writeheader(); w.writerows(ledger)
    candidates = [json.loads(x) for x in (out / 'candidates.jsonl').open()]
    conceal = [c for c in candidates if any(h['rule'] == 'evidence_concealment' for h in c['rules'])]
    result = {
        'version': summary['version'], 'input_comments': summary['unique_input_records'],
        'old_candidates_with_context': old['candidates'], 'new_candidates': summary['candidates'],
        'rule_counts': summary['rule_counts'], 'concealment_candidates': len(conceal),
        'concealment_posts': len({c['video_url'] for c in conceal}),
        'clock_ledger_posts': len(ledger),
        'platform_publication_times': sum(bool(x['platform_publication_timestamp_utc']) for x in ledger),
        'verified_filming_times': 0,
        'limitations': ['Retrospective rules; not prevalence or validated recall.',
                        'User selection enables review; not verified parent-post relevance.',
                        'Photo captions unavailable; no event/filming timestamps established.'],
    }
    deltas = [x['metadata_minus_id_seconds'] for x in ledger if x['metadata_minus_id_seconds'] is not None]
    start = datetime.fromisoformat('2026-11-02T00:00:00+02:00')
    end = datetime.fromisoformat('2026-11-05T12:10:13+02:00')
    result['publication_time_comparison'] = {
        'compared': len(deltas), 'exact_matches': sum(x == 0 for x in deltas),
        'min_seconds': min(deltas), 'median_seconds': statistics.median(deltas), 'max_seconds': max(deltas),
        'over_one_minute': sum(abs(x) > 60 for x in deltas),
        'outliers': [{k: x[k] for k in ['url', 'id_publication_time_estimate_kyiv',
                                      'platform_publication_timestamp_utc', 'metadata_minus_id_seconds']}
                     for x in ledger if x['metadata_minus_id_seconds'] is not None and abs(x['metadata_minus_id_seconds']) > 60],
        'frozen_window_disagreements': sum(
            (start <= datetime.fromisoformat(x['id_publication_time_estimate_kyiv']) < end) !=
            (start <= datetime.fromisoformat(x['platform_publication_timestamp_utc']) < end)
            for x in ledger if x['platform_publication_timestamp_utc']),
    }
    (ROOT / 'reports/tiktok/context-recheck-20261106.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print('CONCEALMENT CANDIDATES FOR HUMAN/ANALYST REVIEW:')
    for c in conceal:
        print(json.dumps({'video_url': c['video_url'], 'text': c['text']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
