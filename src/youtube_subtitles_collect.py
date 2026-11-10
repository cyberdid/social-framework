"""Public YouTube captions only; never media, cookies, login or challenge bypass.

Each invocation creates a NEW raw snapshot. --previous skips completed successes
and no-caption results from an older snapshot without modifying it. Failure receipts
are retryable in a later invocation; blocking ends this run. Selection is metadata
only: top views plus missing date, date/format and lexical marker strata.
"""
import argparse
import csv
import hashlib
import html
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
MARKERS = {
    'mobilization_tcc': r'тцк|мобіліз|мобилиз|военком',
    'detention_torture': r'катув|пыт|підвал|подвал|концтаб|концлаг|полон|плен|рабств|тортур',
    'police_investigation': r'поліці|полиц|розслід|расслед|слідств|следств|дбр',
    'intelligence_military': r'буданов|гур|розвід|развед|нацгвар|військов|военн',
    'conflicting_claims': r'фейк|спрост|опроверж|брех|ложь|правд|насправді|на самом деле',
}


def utc():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def format_group(r):
    return 'live_recording' if r.get('was_live') or r.get('is_live') else ('short_le180' if (r.get('duration') or 0) <= 180 else 'long_gt180')


def marker_groups(r):
    # Title only: avoids incidental boilerplate from descriptions. Not content coding.
    return [k for k, pattern in MARKERS.items() if re.search(pattern, r.get('title', '').lower())] or ['no_marker']


def select_videos(records, top_n=50):
    ranked = sorted(records, key=lambda r: (-(r.get('view_count') or 0), r['id']))
    reasons = {r['id']: ['top_views'] for r in ranked[:top_n]}
    strata = {}
    for r in ranked:
        day = r.get('upload_date') or 'unknown'
        keys = [f'day:{day}', f'day_format:{day}:{format_group(r)}'] + [f'marker:{m}' for m in marker_groups(r)]
        for key in keys:
            strata.setdefault(key, []).append(r['id'])
    for key, members in strata.items():
        if not any(vid in reasons for vid in members):
            reasons[members[0]] = ['supplement:' + key]
        elif members[0] in reasons and reasons[members[0]][0].startswith('supplement:'):
            reasons[members[0]].append(key)
    output = []
    for rank, r in enumerate(ranked, 1):
        if r['id'] not in reasons:
            continue
        output.append({'video_id': r['id'], 'views_rank': rank, 'view_count': r.get('view_count'),
                       'upload_date': r.get('upload_date'), 'format_group': format_group(r),
                       'title_markers': ';'.join(marker_groups(r)), 'selection_reason': ';'.join(reasons[r['id']]),
                       'channel_id': r.get('channel_id'), 'channel': r.get('channel'),
                       'url': 'https://www.youtube.com/watch?v=' + r['id'], 'title': r.get('title')})
    return output


def parse_vtt(text):
    cues = []
    previous = ''
    def seconds(value):
        parts = [float(p) for p in value.split(':')]
        return sum(p * 60 ** i for i, p in enumerate(reversed(parts)))
    for block in re.split(r'\n\s*\n', text.replace('\r', '')):
        lines = block.splitlines()
        timing = next((i for i, line in enumerate(lines) if ' --> ' in line), None)
        if timing is None:
            continue
        match = re.match(r'(\S+) --> (\S+)', lines[timing])
        if not match:
            continue
        value = html.unescape(re.sub(r'<[^>]*>', '', ' '.join(lines[timing + 1:])))
        value = re.sub(r'\s+', ' ', value).strip()
        original = value
        # YouTube rolling ASR repeats the preceding cue at the start of the next.
        old, new = previous.split(), value.split()
        overlap = next((n for n in range(min(len(old), len(new)), 0, -1) if old[-n:] == new[:n]), 0)
        value = ' '.join(new[overlap:])
        if value:
            cues.append({'start': seconds(match[1]), 'end': seconds(match[2]), 'text': value})
        previous = original
    return cues


def failure_reason(message):
    lowered = message.lower()
    if '429' in lowered or 'too many requests' in lowered:
        return 'rate_limited'
    if 'not a bot' in lowered or 'captcha' in lowered:
        return 'bot_blocked'
    if 'sign in' in lowered or 'login' in lowered:
        return 'login_required'
    if any(t in lowered for t in ['unavailable', 'private video', 'removed']):
        return 'unavailable'
    return 'extraction_error'


def choose_track(info):
    for kind, field in [('manual', 'subtitles'), ('automatic', 'automatic_captions')]:
        candidates = []
        for lang, tracks in (info.get(field) or {}).items():
            for track in tracks:
                if track.get('ext') != 'vtt' or not track.get('url'):
                    continue
                translated = 'tlang' in parse_qs(urlparse(track['url']).query)
                # Exclude machine translations: retain original language only.
                if translated:
                    continue
                base = lang.split('-')[0]
                preference = {'uk': 0, 'ru': 1, 'en': 2}.get(base, 3)
                candidates.append(((preference, lang), kind, lang, track))
        if candidates:
            _, kind, lang, track = min(candidates, key=lambda item: item[0])
            return kind, lang, track
    return None


class CaptureLogger:
    def __init__(self): self.messages = []
    def debug(self, message): pass
    def warning(self, message): self.messages.append(message)
    def error(self, message): self.messages.append(message)


def collect_one(video, out):
    import yt_dlp
    logger = CaptureLogger()
    receipt = {'video_id': video['video_id'], 'started_at': utc(), 'url': video['url']}
    opts = {'skip_download': True, 'quiet': True, 'no_warnings': False, 'noplaylist': True,
            'socket_timeout': 25, 'retries': 0, 'extractor_retries': 0, 'logger': logger,
            'ignore_no_formats_error': True, 'cachedir': False}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video['url'], download=False)
            chosen = choose_track(info or {})
            receipt['available_manual_languages'] = sorted((info or {}).get('subtitles') or {})
            receipt['available_automatic_languages'] = sorted((info or {}).get('automatic_captions') or {})
            if chosen:
                kind, language, track = chosen
                # Public caption endpoint from the extractor. No media URLs downloaded.
                payload = ydl.urlopen(track['url']).read()
                target = out / f"{video['video_id']}.{language}.vtt"
                with target.open('xb') as f: f.write(payload)
                receipt.update(status='ok', kind=kind, language=language, file=target.name,
                               bytes=len(payload), sha256=sha(target))
            else:
                receipt['status'] = 'no_original_caption_track'
    except Exception as exc:
        receipt.update(status=failure_reason(str(exc)), error=str(exc))
    receipt['diagnostics'] = logger.messages
    # An extractor may return an empty info object after logging a service block.
    for message in logger.messages:
        category = failure_reason(message)
        if category in {'rate_limited', 'bot_blocked', 'login_required'} and receipt.get('status') != 'ok':
            receipt['status'] = category
    receipt['finished_at'] = utc()
    return receipt


def main():
    import yt_dlp
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--videos', type=Path, default=ROOT / 'data/raw/youtube/20261106T072121Z/videos.jsonl')
    parser.add_argument('--metadata-selection', type=Path, default=ROOT / 'reports/youtube-comments/20261106T072121Z/metadata_selection.csv')
    parser.add_argument('--top', type=int, default=50)
    parser.add_argument('--pause', type=float, default=8)
    parser.add_argument('--previous', type=Path, help='Read prior receipts; never write into an earlier snapshot')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = ROOT / 'data/raw/youtube_subtitles' / stamp
    out.mkdir(parents=True, exist_ok=False)
    report = ROOT / 'reports/youtube-content' / stamp
    report.mkdir(parents=True, exist_ok=False)
    selected = {r['video_id'] for r in csv.DictReader(args.metadata_selection.open()) if r['selected'] == 'True'}
    records = [json.loads(line) for line in args.videos.open()]
    records = [r for r in records if r['id'] in selected]
    selection = select_videos(records, args.top)
    for target in [out / 'selection.csv', report / 'selection.csv']:
        with target.open('x') as f:
            writer = csv.DictWriter(f, fieldnames=list(selection[0]))
            writer.writeheader(); writer.writerows(selection)
    config = {'created_at': utc(), 'videos': str(args.videos.relative_to(ROOT)),
              'videos_sha256': sha(args.videos), 'metadata_selection': str(args.metadata_selection.relative_to(ROOT)),
              'metadata_selection_sha256': sha(args.metadata_selection), 'script_sha256': sha(Path(__file__)),
              'lexical_population': len(records), 'channels': len({r.get('channel_id') for r in records}),
              'selected': len(selection), 'top': args.top, 'pause_seconds': max(1, args.pause),
              'yt_dlp': yt_dlp.version.__version__, 'auth': 'none', 'media_downloaded': False,
              'selection_rule': 'top views + highest-view video in each uncovered date, date/format, title-marker stratum',
              'title_marker_regex': MARKERS, 'previous': str(args.previous) if args.previous else None}
    (out / 'config.json').write_text(json.dumps(config, ensure_ascii=False, indent=2))
    completed = set()
    if args.previous:
        for line in (args.previous / 'receipts.jsonl').open():
            r = json.loads(line)
            if r['status'] in {'ok', 'no_original_caption_track'}: completed.add(r['video_id'])
    receipts, consecutive_rate_limits, stop = [], 0, None
    with (out / 'receipts.jsonl').open('x') as f:
        try:
            for video in selection:
                if args.prepare_only: break
                if video['video_id'] in completed: continue
                rec = collect_one(video, out)
                f.write(json.dumps(rec, ensure_ascii=False) + '\n'); f.flush()
                receipts.append(rec)
                print(video['video_id'], rec['status'], flush=True)
                consecutive_rate_limits = consecutive_rate_limits + 1 if rec['status'] == 'rate_limited' else 0
                if rec['status'] in {'bot_blocked', 'login_required'} or consecutive_rate_limits >= 2:
                    stop = rec['status']; break
                time.sleep(max(1, args.pause) * (3 if consecutive_rate_limits else 1))
        except KeyboardInterrupt:
            stop = 'interrupted'
    manifest = config | {'finished_at': utc(), 'attempted': len(receipts), 'skipped_previous': len(completed),
                         'status_counts': dict(Counter(r['status'] for r in receipts)), 'stop_reason': stop,
                         'sha256': {p.name: sha(p) for p in out.iterdir() if p.is_file()}}
    with (out / 'manifest.json').open('x') as f: json.dump(manifest, f, ensure_ascii=False, indent=2)
    (report / 'collection_summary.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps({'raw': str(out), 'report': str(report), 'attempted': len(receipts), 'status_counts': manifest['status_counts'], 'stop_reason': stop}), flush=True)


if __name__ == '__main__': main()
