"""Conditional randomization checks of S7 and timing of verified Telegram copies.

Exploratory, conditional on the collected corpus and existing text-candidate graph.
No attribution, bot classification, payment, or causal transmission inference.
Usage: python src/analysis/coordination_null_models.py --replicates 9999 --seed 20261106
Requires numpy. No input files are modified. All permutations are independent draws.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
KYIV = ZoneInfo('Europe/Kyiv')
COMMENTS = ROOT / 'data/raw/telegram_comments/20261106T071956Z/comments.jsonl'
COPIES = ROOT / 'reports/telegram-comments/20261106T071956Z/copypasta_groups.csv'
SIGNALS = ROOT / 'reports/telegram-comments/20261106T071956Z/signals.json'
EDGES = ROOT / 'reports/continuation_20261105/verified_similarity_edges.csv'
RELEVANT = ROOT / 'reports/continuation_20261105/relevant_posts.csv'
POSTS = ROOT / 'data/processed/continuation_20261105/posts_clean.jsonl'
THREADS = {'https://t.me/insiderukr/137695', 'https://t.me/truexanewsua/147862',
           'https://t.me/uaonlii/135660', 'https://t.me/uaonlii/135665'}


def norm(text):
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', (text or '').lower())).strip()


def s7_statistics(labels, threads, authors):
    """Max identical eligible thread signature; phrases in signatures of size >=10.

    labels=-1 denotes ineligible text. Eligibility and authors are recomputed in
    each draw. Max scans every observed thread set, rather than a selected pool.
    """
    by_phrase = defaultdict(list)
    for i in np.flatnonzero(labels >= 0):
        by_phrase[int(labels[i])].append(int(i))
    signatures = Counter()
    for indices in by_phrase.values():
        if len(indices) < 3 or len(set(authors[indices])) < 3:
            continue
        th = threads[indices]
        if len(th) == len(set(th)):
            signatures[tuple(sorted(th))] += 1
    return max(signatures.values(), default=0), sum(n for n in signatures.values() if n >= 10)


def permute_labels(labels, strata, rng):
    """Uniform independent permutation within each fixed disjoint stratum."""
    result = labels.copy()
    if isinstance(strata, tuple):
        destinations, groups = strata
        # A uniform global permutation gives independent uniform relative orders
        # within disjoint groups. Distinct integer priorities avoid float ties.
        order = np.lexsort((rng.permutation(len(destinations)), groups))
        result[destinations] = labels[destinations[order]]
    else:
        for ix in strata:
            result[ix] = labels[rng.permutation(ix)]
    return result


def rotate_times(blocks, n_nodes, rng):
    """Independent uniform cyclic content rotations on fixed channel time slots.

    Each content label in a block moves by the same rank offset. Real time slots,
    channel and block membership remain fixed; cyclic rank order is retained.
    """
    result = np.empty(n_nodes, dtype=float)
    for block in blocks:
        shift = int(rng.integers(len(block['times'])))
        result[block['nodes']] = block['times'][(block['positions'] + shift) % len(block['times'])]
    return result


def empirical_summary(observed, draws):
    values = np.asarray(draws, dtype=float)
    if not len(values):
        raise ValueError('At least one randomization is required')
    k = int(np.sum(values >= observed))
    mean = float(np.mean(values))
    return {'observed': int(observed), 'null_mean': mean,
            'null_q025': float(np.quantile(values, .025)),
            'null_q975': float(np.quantile(values, .975)),
            'null_min': int(np.min(values)), 'null_max': int(np.max(values)),
            'excess': float(observed - mean), 'ratio': observed / mean if mean else None,
            'tail_count': k, 'B': len(values), 'p_raw': (k + 1) / (len(values) + 1)}


def holm_adjust(pvalues):
    """Holm FWER control, valid with arbitrary dependence between tests."""
    order = sorted(range(len(pvalues)), key=lambda i: pvalues[i])
    result = [0.] * len(order)
    running = 0.
    for rank, i in enumerate(order):
        running = max(running, (len(order) - rank) * pvalues[i])
        result[i] = min(1., running)
    return result


def csv_write(path, rows):
    if not rows:
        return
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def grouped_indices(keys):
    groups = defaultdict(list)
    for i, key in enumerate(keys):
        groups[key].append(i)
    return [np.array(ix, dtype=int) for ix in groups.values()]


def comment_analysis(B, seed, out):
    raw = [json.loads(line) for line in COMMENTS.open()]
    cs = [c for c in raw if c.get('sender')]
    text = [norm(c.get('text')) for c in cs]
    phrase_ids = {t: i for i, t in enumerate(sorted(set(text)))}
    full = np.array([phrase_ids[t] for t in text], dtype=int)
    frequencies = np.bincount(full)
    eligible = np.array([i if len(t) >= 20 and frequencies[i] >= 3 else -1
                         for t, i in sorted(phrase_ids.items(), key=lambda x: x[1])], dtype=int)
    labels = eligible[full]
    thread_ids = {t: i for i, t in enumerate(sorted({c['post_url'] for c in cs}))}
    author_ids = {a: i for i, a in enumerate(sorted({c['sender'] for c in cs}))}
    threads = np.array([thread_ids[c['post_url']] for c in cs])
    authors = np.array([author_ids[c['sender']] for c in cs])
    activity = Counter(c['sender'] for c in cs)
    days = [datetime.fromisoformat(c['date']).astimezone(KYIV).date().isoformat() for c in cs]
    scripts = [('uk_marker' if re.search('[іїєґ]', t) else 'other_cyrillic' if re.search('[а-я]', t) else 'other') for t in text]
    bins = [0 if activity[c['sender']] == 1 else 1 if activity[c['sender']] <= 4
            else 2 if activity[c['sender']] <= 9 else 3 for c in cs]
    nulls = {'day_activity_script': grouped_indices(zip(days, bins, scripts)),
             'author_day': grouped_indices(zip(authors, days))}
    by_phrase = defaultdict(list)
    for i, t in enumerate(text):
        if len(t) >= 20:
            by_phrase[t].append(i)
    copies = {t: ix for t, ix in by_phrase.items() if len(set(authors[ix])) >= 3}
    campaign = {t: ix for t, ix in copies.items()
                if len(set(threads[ix])) >= 3 and {cs[i]['post_url'] for i in ix} <= THREADS}
    signatures = Counter(tuple(sorted(set(threads[ix]))) for ix in copies.values()
                         if len(ix) == len(set(threads[ix])) and len(ix) >= 3)
    ledger = []
    pool_indices = []
    for t, ix in sorted(campaign.items()):
        counts = Counter(cs[i]['post_url'] for i in ix)
        size = signatures.get(tuple(sorted(set(threads[ix]))), 0)
        passes = max(counts.values()) == 1 and size >= 10
        if passes:
            pool_indices.extend(ix)
        ledger.append({'phrase': t, 'comments': len(ix), 'accounts': len(set(authors[ix])),
                       'threads': ';'.join(sorted(counts)), 'thread_signature_phrases': size,
                       'S7': int(passes), 'reason': 'included' if passes else 'signature_pool_below_10',
                       'max_occurrences_per_thread': max(counts.values())})
    csv_write(out / 'phrase_reconciliation.csv', ledger)
    observed = s7_statistics(labels, threads, authors)
    expected = json.loads(SIGNALS.read_text())['S7_template_pools']
    assert observed == (max(p['phrases'] for p in expected), sum(p['phrases'] for p in expected))
    assert (len(campaign), sum(len(ix) for ix in campaign.values()),
            len({int(authors[i]) for ix in campaign.values() for i in ix})) == (126, 438, 367)
    results, controls, rows = [], {}, []
    for model_index, (model, strata) in enumerate(nulls.items()):
        rng = np.random.default_rng(np.random.SeedSequence([seed, 1, model_index]))
        draws = np.empty((B, 2), dtype=int)
        movable = [ix for ix in strata if len(ix) > 1 and len(set(full[ix])) > 1]
        destinations = np.concatenate(movable) if movable else np.array([], dtype=int)
        group_codes = np.repeat(np.arange(len(movable)), [len(ix) for ix in movable])
        prepared_strata = (destinations, group_codes)
        moved = []
        for b in range(B):
            shuffled = permute_labels(full, prepared_strata, rng)
            # Exact phrase frequencies checked in EVERY draw, including short and
            # singleton texts. Authors, threads, times never leave their slots.
            assert np.array_equal(np.bincount(shuffled, minlength=len(frequencies)), frequencies)
            if b < 25:
                for ix in strata:
                    assert np.array_equal(np.sort(full[ix]), np.sort(shuffled[ix]))
            candidate_labels = eligible[shuffled]
            draws[b] = s7_statistics(candidate_labels, threads, authors)
            moved.append(float(np.mean(candidate_labels[labels >= 0] != labels[labels >= 0])))
            rows.append({'model': model, 'replicate': b + 1,
                         'max_pool_phrases': int(draws[b, 0]), 'pooled_phrases': int(draws[b, 1])})
        fixed = np.zeros(len(cs), dtype=bool)
        for ix in strata:
            if len(set(full[ix])) == 1:
                fixed[ix] = True
        controls[model] = {'strata': len(strata), 'singleton_slot_strata': sum(len(ix) == 1 for ix in strata),
                           'fixed_candidate_comment_slots': int(sum(fixed & (labels >= 0))),
                           'all_candidate_comment_slots': int(sum(labels >= 0)),
                           'candidate_slot_changed_fraction_mean': float(np.mean(moved)),
                           'exact_phrase_frequency_checks': B,
                           'within_stratum_full_dictionary_checks': min(B, 25),
                           'fixed_author_thread_time_slots': len(cs)}
        for j, statistic in enumerate(['max_pool_phrases', 'pooled_phrases']):
            results.append({'family': 'S7', 'model': model, 'statistic': statistic,
                            **empirical_summary(observed[j], draws[:, j])})
    csv_write(out / 's7_null_draws.csv', rows)
    campaign_ix = [i for ix in campaign.values() for i in ix]
    return results, {'raw_comments': len(raw), 'known_author_comments': len(cs),
                     'threads': len(thread_ids), 'authors': len(author_ids),
                     'candidate_phrases_frequency_ge3_length_ge20': int(sum(eligible >= 0)),
                     'all_copypasta_phrases_ge3_authors': len(copies),
                     'all_copypasta_comments': sum(len(ix) for ix in copies.values()),
                     'campaign_phrases': len(campaign), 'campaign_comments': len(campaign_ix),
                     'campaign_authors': len(set(authors[campaign_ix])),
                     'S7_phrases': observed[1], 'S7_comments': len(pool_indices),
                     'S7_authors': len(set(authors[pool_indices])),
                     'excluded_phrases': sum(not r['S7'] for r in ledger),
                     'excluded_comments': sum(r['comments'] for r in ledger if not r['S7']),
                     'null_controls': controls}


def time_blocks(posts, nodes, hours):
    """Build fixed local-day/hour timelines from ALL collected posts, not copies."""
    timelines = defaultdict(list)
    for p in posts:
        date = datetime.fromisoformat(p['date']).astimezone(KYIV)
        key = (p['channel'], date.date().isoformat(), date.hour // hours)
        timelines[key].append((date.timestamp(), p['url']))
    blocks = []
    for key, timeline in sorted(timelines.items()):
        timeline.sort()
        positions = [(j, nodes[url]) for j, (_, url) in enumerate(timeline) if url in nodes]
        if positions:
            blocks.append({'times': np.array([t for t, _ in timeline]),
                           'positions': np.array([j for j, _ in positions]),
                           'nodes': np.array([i for _, i in positions]), 'key': key})
    assert sorted(int(i) for b in blocks for i in b['nodes']) == list(range(len(nodes)))
    return blocks


def temporal_analysis(B, seed, out):
    edges = list(csv.DictReader(EDGES.open()))
    relevant = {r['url'] for r in csv.DictReader(RELEVANT.open())}
    posts = [json.loads(line) for line in POSTS.open()]
    urls = sorted({e[key] for e in edges for key in ['earlier_url', 'later_url']})
    nodes = {url: i for i, url in enumerate(urls)}
    assert len({p['url'] for p in posts}) == len(posts), 'Duplicate timeline URL'
    lookup = {p['url']: p for p in posts}
    assert set(nodes) <= relevant
    original = np.array([datetime.fromisoformat(lookup[url]['date']).timestamp() for url in urls])
    pairs = np.array([[nodes[e['earlier_url']], nodes[e['later_url']]] for e in edges])
    scores = np.array([float(e['jaccard']) for e in edges])
    assert all(lookup[e['earlier_url']]['channel'] != lookup[e['later_url']]['channel'] for e in edges)
    assert all(abs((original[pairs[i, 1]] - original[pairs[i, 0]]) / 60 - float(e['lag_minutes'])) < 1e-7
               for i, e in enumerate(edges))
    settings = [(threshold, window) for threshold in [.6, .75, .9] for window in [10, 30]]
    masks = [scores >= threshold for threshold, _ in settings]
    def statistics(times):
        deltas = np.abs(times[pairs[:, 1]] - times[pairs[:, 0]]) / 60
        return np.array([sum((deltas <= window) & mask) for (_, window), mask in zip(settings, masks)])
    observed = statistics(original)
    results, controls, rows, block_rows = [], {}, [], []
    for model_index, hours in enumerate([24, 2]):
        model = f'channel_{hours}h_slot_rotation'
        rng = np.random.default_rng(np.random.SeedSequence([seed, 2, model_index]))
        blocks = time_blocks(posts, nodes, hours)
        draws = np.empty((B, len(settings)), dtype=int)
        moved = []
        for b in range(B):
            times = rotate_times(blocks, len(nodes), rng)
            draws[b] = statistics(times)
            moved.append(float(np.mean(times != original)))
            if b < 25:
                # Rotated nodes occupy distinct real slots in their own block;
                # all pairwise cyclic rank offsets remain invariant.
                for block in blocks:
                    vals = times[block['nodes']]
                    assert all(t in block['times'] for t in vals)
                    # Equal timestamps can correspond to distinct Telegram posts.
                    offsets = (np.searchsorted(block['times'], vals) - block['positions']) % len(block['times'])
                    if len(set(block['times'])) == len(block['times']):
                        assert len(set(offsets)) == 1
            rows.append({'model': model, 'replicate': b + 1,
                         **{f'j{threshold}_w{window}': int(draws[b, j])
                            for j, (threshold, window) in enumerate(settings)}})
        for block in blocks:
            block_rows.append({'model': model, 'channel': block['key'][0], 'kyiv_day': block['key'][1],
                               'block': block['key'][2], 'timeline_slots': len(block['times']),
                               'copy_nodes': len(block['nodes']),
                               'first_utc': datetime.fromtimestamp(float(min(block['times'])), ZoneInfo('UTC')).isoformat(),
                               'last_utc': datetime.fromtimestamp(float(max(block['times'])), ZoneInfo('UTC')).isoformat()})
        controls[model] = {'blocks_with_copy_nodes': len(blocks),
                          'timeline_slots_in_these_blocks': sum(len(b['times']) for b in blocks),
                          'one_slot_blocks': sum(len(b['times']) == 1 for b in blocks),
                          'fixed_nodes_one_slot_blocks': sum(len(b['nodes']) for b in blocks if len(b['times']) == 1),
                          'node_time_changed_fraction_mean': float(np.mean(moved)),
                          'rank_order_and_membership_check_replicates': min(B, 25)}
        for j, (threshold, window) in enumerate(settings):
            n = int(sum(masks[j]))
            summary = empirical_summary(observed[j], draws[:, j])
            results.append({'family': 'copy_timing', 'model': model,
                            'statistic': f'pairs_jaccard_ge{threshold}_lag_le{window}min',
                            'edge_denominator': n, 'observed_share': observed[j] / n,
                            'null_share': summary['null_mean'] / n, **summary})
    csv_write(out / 'temporal_null_draws.csv', rows)
    csv_write(out / 'temporal_blocks.csv', block_rows)
    return results, {'timeline_posts': len(posts), 'relevant_posts': len(relevant),
                     'verified_edges': len(edges), 'edge_nodes': len(nodes),
                     'edge_channels': len({lookup[u]['channel'] for u in nodes}), 'null_controls': controls}


def main():
    global EDGES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--replicates', type=int, default=9999)
    parser.add_argument('--seed', type=int, default=20261106)
    default_out = ROOT / 'reports/coordination-null-models-2026-11-06'
    parser.add_argument('--out', type=Path, default=default_out)
    parser.add_argument('--edges', type=Path, default=EDGES,
                        help='copy-edge CSV; alternate graphs require an explicit --out directory')
    args = parser.parse_args()
    if args.replicates < 1:
        parser.error('--replicates must be positive')
    if args.edges.resolve() != EDGES.resolve() and args.out.resolve() == default_out.resolve():
        parser.error('--out is required when --edges selects an alternate graph')
    args.out.mkdir(parents=True, exist_ok=True)
    original_edges = EDGES
    EDGES = args.edges
    try:
        s7, comment_controls = comment_analysis(args.replicates, args.seed, args.out)
        temporal, temporal_controls = temporal_analysis(args.replicates, args.seed, args.out)
        tests = s7 + temporal
        for row, adjusted in zip(tests, holm_adjust([r['p_raw'] for r in tests])):
            row['p_holm_16'] = adjusted
        files = [COMMENTS, COPIES, SIGNALS, EDGES, RELEVANT, POSTS]
        result = {'seed': args.seed, 'B': args.replicates, 'python': platform.python_version(),
                  'numpy': np.__version__, 'rng': 'numpy PCG64; independent SeedSequence [seed,family,model]',
                  'exploratory': True, 'test_family_size': len(tests),
                  'tail': 'upper inclusive; p=(k+1)/(B+1)', 'comments': comment_controls,
                  'copies': temporal_controls, 'tests': tests,
                  'inputs_sha256': {str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p):
                                    hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                  'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        (args.out / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
        fields = sorted({key for r in tests for key in r})
        csv_write(args.out / 'test_summary.csv', [{key: r.get(key) for key in fields} for r in tests])
        print(json.dumps({'output': str(args.out), 'B': args.replicates, 'tests': tests}, ensure_ascii=False, indent=2))
    finally:
        EDGES = original_edges


if __name__ == '__main__':
    main()
