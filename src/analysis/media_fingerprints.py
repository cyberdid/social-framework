"""CPU-only candidate shared-video matching. No network, OCR or geolocation.

fingerprint VIDEO [--output RECEIPT.json]
compare VIDEO_OR_RECEIPT VIDEO_OR_RECEIPT [--output COMPARISON.json]

Perceptual thresholds are exploratory, not calibrated probabilities. Byte
identity and similar visual sequences cannot establish location, date or truth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageOps
from scipy.fft import dctn

ROOT = Path(__file__).resolve().parents[2]
ALGORITHM = 'sampled-phash63-dhash64-crops-mirror-h-v2'


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def display_path(path):
    path = Path(path).resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def image_signature(image):
    gray = image.convert('L')
    small = np.asarray(gray.resize((32, 32), Image.Resampling.LANCZOS), dtype=float)
    coefficients = dctn(small, type=2, norm='ortho')[:8, :8].flatten()[1:]
    phash = sum(int(bit) << i for i, bit in enumerate(coefficients > np.median(coefficients)))
    dh = np.asarray(gray.resize((9, 8), Image.Resampling.LANCZOS), dtype=float)
    dhash = sum(int(bit) << i for i, bit in enumerate((dh[:, 1:] > dh[:, :-1]).flatten()))
    stddev = float(small.std())
    edge = float((np.abs(np.diff(small, axis=0)).mean() + np.abs(np.diff(small, axis=1)).mean()) / 2)
    return {'phash': f'{phash:016x}', 'dhash': f'{dhash:016x}',
            'gray_stddev': round(stddev, 4), 'mean_edge_difference': round(edge, 4),
            'informative': stddev >= 5 and edge >= 0.5}


def frame_variants(image):
    """Black-border crop is geometric; it does not locate the actual scene."""
    width, height = image.size
    gray = np.asarray(image.convert('L'))
    bright = gray > 12
    rows, cols = np.where(bright.mean(axis=1) > 0.025)[0], np.where(bright.mean(axis=0) > 0.025)[0]
    box = (int(cols[0]), int(rows[0]), int(cols[-1] + 1), int(rows[-1] + 1)) if len(rows) and len(cols) else (0, 0, width, height)
    if box[2] - box[0] < 16 or box[3] - box[1] < 16:
        box = (0, 0, width, height)
    x0, y0, x1, y1 = box
    dx, dy = max(0, (x1-x0)//10), max(0, (y1-y0)//10)
    boxes = {'full': (0, 0, width, height), 'border_crop': box,
             'center_80': (x0+dx, y0+dy, x1-dx, y1-dy)}
    variants = {}
    for name, bbox in boxes.items():
        crop = image.crop(bbox)
        variants[name] = {'bbox_xyxy': list(bbox), **image_signature(crop)}
        variants[f'{name}_mirror_h'] = {'bbox_xyxy': list(bbox),
                                        **image_signature(ImageOps.mirror(crop))}
    return variants


def fingerprint(path, interval_seconds=1.0, max_frames=180, trim_start_seconds=0.0,
                trim_end_seconds=0.0, max_seconds=600.0):
    path = Path(path)
    if interval_seconds <= 0 or max_frames < 1 or max_seconds <= 0 or min(trim_start_seconds, trim_end_seconds) < 0:
        raise ValueError('Positive sampling limits and nonnegative trims required')
    before = path.stat()
    digest = sha256(path)
    frames, decoded = [], 0
    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                raise ValueError(f'No video stream: {display_path(path)}')
            stream = container.streams.video[0]
            rate = float(stream.average_rate) if stream.average_rate else 25.0
            duration = (float(stream.duration * stream.time_base) if stream.duration is not None else
                        container.duration / av.time_base if container.duration is not None else None)
            if trim_end_seconds and duration is None:
                raise ValueError('Cannot trim end without a known video duration')
            end = min(duration - trim_end_seconds, trim_start_seconds + max_seconds) if duration is not None else trim_start_seconds + max_seconds
            if end <= trim_start_seconds:
                raise ValueError('Trimming leaves no video sampling window')
            step = max(interval_seconds, (end - trim_start_seconds) / max_frames)
            target, origin = trim_start_seconds, None
            width, height = stream.codec_context.width, stream.codec_context.height
            codec = stream.codec_context.name
            for index, frame in enumerate(container.decode(stream)):
                decoded += 1
                actual = frame.time
                if origin is None:
                    origin = actual if actual is not None else 0.0
                t = float(actual - origin) if actual is not None else index / rate
                if t >= end:
                    break
                if t + 1e-8 < target or len(frames) >= max_frames:
                    continue
                frames.append({'index': index, 'time_seconds': round(t, 6),
                               'presentation_seconds': float(actual) if actual is not None else None,
                               'pts': frame.pts, 'time_base': str(frame.time_base),
                               'timestamp_source': 'presentation_timestamp' if actual is not None else 'frame_index_over_rate',
                               'variants': frame_variants(frame.to_image())})
                target = trim_start_seconds + (math.floor((t - trim_start_seconds) / step) + 1) * step
    except av.error.FFmpegError as exc:
        raise ValueError(f'Cannot open/decode video {display_path(path)}: {exc}') from exc
    if not frames:
        raise ValueError(f'No decoded video frames inside sampling window: {display_path(path)}')
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError('Input changed while fingerprinting')
    return {'schema_version': 1, 'kind': 'video_fingerprint', 'algorithm': ALGORITHM,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'source': {'path': display_path(path), 'sha256': digest, 'bytes': before.st_size},
            'video': {'duration_seconds': duration, 'width': width, 'height': height,
                      'codec': codec, 'average_rate': rate, 'decoded_frames': decoded},
            'sampling': {'requested_interval_seconds': interval_seconds, 'effective_interval_seconds': step,
                         'max_frames': max_frames, 'trim_start_seconds': trim_start_seconds,
                         'trim_end_seconds': trim_end_seconds, 'max_seconds': max_seconds,
                         'window_start_seconds': trim_start_seconds, 'window_end_seconds': end,
                         'analysis_capped': duration is None or end < duration - trim_end_seconds,
                         'sampled_frames': len(frames),
                         'policy': 'Sequential decoding; first frame at/after each grid target. '
                                   'No semantic outro detection; manual trims are recorded.'},
            'hashes': {'phash_bits': 63, 'dhash_bits': 64,
                       'phash': '32x32 grayscale DCT-II, 8x8 low-frequency block excluding DC, median bits',
                       'dhash': '9x8 grayscale horizontal adjacent-pixel comparisons',
                       'variants': ['full', 'border_crop', 'center_80',
                                    'full_mirror_h', 'border_crop_mirror_h', 'center_80_mirror_h'],
                       'black_border_threshold': 12, 'bright_fraction_for_row_column': 0.025,
                       'minimum_gray_stddev': 5, 'minimum_mean_edge_difference': 0.5},
            'frames': frames,
            'limitations': 'Candidate visual evidence only. No OCR, audio match, location, date, '
                           'authenticity assessment or calibrated probability. Sampling can miss brief matches.'}


def nearest_direction(a, b, phash_threshold, dhash_threshold):
    eligible_a = [f for f in a['frames'] if any(v['informative'] for v in f['variants'].values())]
    eligible_b = [f for f in b['frames'] if any(v['informative'] for v in f['variants'].values())]
    pairs = []
    for frame_a in eligible_a:
        best = None
        for frame_b in eligible_b:
            for va, ha in frame_a['variants'].items():
                if not ha['informative']:
                    continue
                for vb, hb in frame_b['variants'].items():
                    if not hb['informative']:
                        continue
                    dp = (int(ha['phash'], 16) ^ int(hb['phash'], 16)).bit_count()
                    dd = (int(ha['dhash'], 16) ^ int(hb['dhash'], 16)).bit_count()
                    score = max(dp / phash_threshold, dd / dhash_threshold)
                    key = (score, dp + dd, abs(frame_a['time_seconds'] - frame_b['time_seconds']), va, vb)
                    if best is None or key < best[0]:
                        best = (key, {'index_a': frame_a['index'], 'index_b': frame_b['index'],
                                      'time_a_seconds': frame_a['time_seconds'], 'time_b_seconds': frame_b['time_seconds'],
                                      'variant_a': va, 'variant_b': vb, 'phash_distance': dp, 'dhash_distance': dd,
                                      'match': dp <= phash_threshold and dd <= dhash_threshold})
        if best is not None:
            pairs.append(best[1])
    matched = [p for p in pairs if p['match']]
    offsets = [p['time_b_seconds'] - p['time_a_seconds'] for p in matched]
    offset = statistics.median(offsets) if offsets else None
    tolerance = max(1.0, a['sampling']['effective_interval_seconds'] * 1.5,
                    b['sampling']['effective_interval_seconds'] * 1.5)
    consistent = [p for p in matched if abs(p['time_b_seconds'] - p['time_a_seconds'] - offset) <= tolerance]
    # Require distinct source AND target frames; repeated common stills are weak evidence.
    matched_b = {p['index_b'] for p in consistent}
    by_index = {frame['index']: frame for frame in a['frames']}
    distinct_visual = {by_index[p['index_a']]['variants'][p['variant_a']]['phash'] for p in consistent}
    times = [p['time_a_seconds'] for p in consistent]
    return {'informative_frames': len(eligible_a), 'target_informative_frames': len(eligible_b),
            'matched_frames': len(matched), 'matching_coverage': len(matched) / len(eligible_a) if eligible_a else 0.0,
            'temporally_consistent_frames': len(consistent),
            'temporal_coverage': len(consistent) / len(eligible_a) if eligible_a else 0.0,
            'distinct_target_frames': len(matched_b), 'distinct_matched_visual_hashes': len(distinct_visual),
            'matched_span_seconds': max(times) - min(times) if times else 0.0,
            'median_offset_seconds_target_minus_source': offset,
            'offset_mad_seconds': statistics.median(abs(v-offset) for v in offsets) if offsets else None,
            'offset_tolerance_seconds': tolerance, 'nearest_frames': pairs}


def compare_fingerprints(a, b, phash_threshold=10, dhash_threshold=12,
                         minimum_coverage=0.6, minimum_matching_frames=3,
                         minimum_span_seconds=2.0):
    if a.get('algorithm') != ALGORITHM or b.get('algorithm') != ALGORITHM:
        raise ValueError('Incompatible fingerprint algorithm/version')
    if not (0 < phash_threshold <= 63 and 0 < dhash_threshold <= 64 and 0 < minimum_coverage <= 1
            and minimum_matching_frames >= 1 and minimum_span_seconds >= 0):
        raise ValueError('Invalid comparison thresholds')
    ab, ba = nearest_direction(a, b, phash_threshold, dhash_threshold), nearest_direction(b, a, phash_threshold, dhash_threshold)
    duration_a = a['sampling']['window_end_seconds'] - a['sampling']['window_start_seconds']
    duration_b = b['sampling']['window_end_seconds'] - b['sampling']['window_start_seconds']
    shorter = 'a_to_b' if duration_a <= duration_b else 'b_to_a'
    evidence = ab if shorter == 'a_to_b' else ba
    candidate = (evidence['temporal_coverage'] >= minimum_coverage
                 and evidence['temporally_consistent_frames'] >= minimum_matching_frames
                 and evidence['distinct_target_frames'] >= minimum_matching_frames
                 and evidence['distinct_matched_visual_hashes'] >= minimum_matching_frames
                 and evidence['matched_span_seconds'] >= minimum_span_seconds)
    ab.update(source_label='a', target_label='b')
    ba.update(source_label='b', target_label='a')
    # Pair keys consistently refer to the global A/B inputs, even in B→A.
    for pair in ba['nearest_frames']:
        for left, right in (('index_a', 'index_b'), ('time_a_seconds', 'time_b_seconds'),
                            ('variant_a', 'variant_b')):
            pair[left], pair[right] = pair[right], pair[left]
    exact = a['source']['sha256'] == b['source']['sha256']
    return {'schema_version': 1, 'kind': 'video_comparison', 'algorithm': ALGORITHM,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'sources': {'a': a['source'], 'b': b['source']},
            'sampling': {'a': a['sampling'], 'b': b['sampling']},
            'exact_file_match': exact, 'perceptual_candidate': candidate,
            'candidate_media_match': exact or candidate,
            'decision': 'identical_file_bytes' if exact else 'candidate_shared_visual_sequence' if candidate else 'no_candidate_at_these_parameters',
            'thresholds': {'status': 'exploratory_uncalibrated', 'phash_distance_max': phash_threshold,
                           'dhash_distance_max': dhash_threshold, 'minimum_coverage': minimum_coverage,
                           'minimum_matching_frames': minimum_matching_frames, 'minimum_span_seconds': minimum_span_seconds,
                           'coverage_rule': 'Temporally consistent coverage of the shorter analysed window; both directions reported.',
                           'candidate_direction': shorter},
            'directions': {'a_to_b': ab, 'b_to_a': ba},
            'limitations': 'Distances and coverage are descriptive, not probability. Crops and nearest-neighbour '
                           'search can create false positives; similar overlays/outros can match. Require manual '
                           'frame review and independent contextual evidence. No match does not establish different '
                           'events; trimming, fast edits and omitted frames can cause false negatives. '
                           'Nearest-frame offsets describe alignment, not upload/copy direction.'}


def load_input(path, parameters):
    path = Path(path)
    if path.suffix.lower() == '.json':
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('kind') != 'video_fingerprint':
            raise ValueError(f'Not a video fingerprint receipt: {path}')
        record['receipt'] = {'path': display_path(path), 'sha256': sha256(path),
                             'note': 'Recorded video hash not reverified when loading a receipt.'}
        return record
    return fingerprint(path, **parameters)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('fingerprint', 'compare'):
        p = sub.add_parser(command)
        p.add_argument('first', type=Path)
        if command == 'compare':
            p.add_argument('second', type=Path)
            p.add_argument('--phash-threshold', type=int, default=10)
            p.add_argument('--dhash-threshold', type=int, default=12)
            p.add_argument('--minimum-coverage', type=float, default=0.6)
        p.add_argument('--interval', type=float, default=1.0)
        p.add_argument('--max-frames', type=int, default=180)
        p.add_argument('--trim-start', type=float, default=0.0)
        p.add_argument('--trim-end', type=float, default=0.0)
        p.add_argument('--max-seconds', type=float, default=600.0)
        p.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        parameters = {'interval_seconds': args.interval, 'max_frames': args.max_frames,
                      'trim_start_seconds': args.trim_start, 'trim_end_seconds': args.trim_end,
                      'max_seconds': args.max_seconds}
        first = load_input(args.first, parameters)
        if args.command == 'fingerprint':
            result = first
        else:
            second = load_input(args.second, parameters)
            result = compare_fingerprints(first, second, args.phash_threshold, args.dhash_threshold,
                                          args.minimum_coverage)
            result['input_receipts'] = {k: v.get('receipt') for k,v in [('a', first), ('b', second)]}
        payload = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            if args.output.resolve().is_relative_to((ROOT / 'data/raw').resolve()):
                raise ValueError('Receipts must not be written into immutable data/raw')
            if args.output.resolve() in {args.first.resolve(), getattr(args, 'second', args.first).resolve()}:
                raise ValueError('Output must not overwrite input')
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding='utf-8')
            print(f'{result["kind"]}: {display_path(args.output)}')
        else:
            print(payload, end='')
    except (ValueError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
