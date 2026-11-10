"""Synthetic falsification and exact-margin tests for coordination null models."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/analysis'))
import coordination_null_models as nm


class CoordinationNullTests(unittest.TestCase):
    def test_s7_positive_and_negative(self):
        labels = np.repeat(np.arange(20), 3)
        threads = np.tile(np.arange(3), 20)
        authors = np.arange(60)
        self.assertEqual(nm.s7_statistics(labels, threads, authors), (20, 20))
        # Merely repeating a phrase within one thread cannot pass S7.
        self.assertEqual(nm.s7_statistics(labels, labels % 3, authors), (0, 0))
        rng = np.random.default_rng(7)
        null = [nm.s7_statistics(rng.permutation(labels), threads, authors)[0] for _ in range(499)]
        self.assertLess(nm.empirical_summary(20, null)['p_raw'], .01)
        # No shared thread signature: a negative control cannot be significant.
        self.assertEqual(nm.empirical_summary(0, null)['p_raw'], 1)

    def test_s7_recomputes_distinct_authors_and_pool_threshold(self):
        labels = np.repeat(np.arange(9), 3)
        threads = np.tile(np.arange(3), 9)
        authors = np.arange(27)
        self.assertEqual(nm.s7_statistics(labels, threads, authors), (9, 0))
        self.assertEqual(nm.s7_statistics(labels, threads, np.zeros(27, dtype=int)), (0, 0))

    def test_shuffle_exact_margins_and_author_dictionary(self):
        labels = np.array([1, 2, 3, 4, 1, 2])
        strata = [np.array([0, 1, 2]), np.array([3, 4, 5])]
        rng = np.random.default_rng(8)
        for _ in range(40):
            shuffled = nm.permute_labels(labels, strata, rng)
            self.assertEqual(sorted(labels), sorted(shuffled))
            for ix in strata:
                self.assertEqual(sorted(labels[ix]), sorted(shuffled[ix]))
        prepared = (np.concatenate(strata), np.repeat(np.arange(2), 3))
        for _ in range(40):
            shuffled = nm.permute_labels(labels, prepared, rng)
            for ix in strata:
                self.assertEqual(sorted(labels[ix]), sorted(shuffled[ix]))
        singleton = [np.array([i]) for i in range(len(labels))]
        self.assertTrue(np.array_equal(labels, nm.permute_labels(labels, singleton, rng)))

    def test_slot_rotation_preserves_exact_rhythm_and_cyclic_order(self):
        slots = np.array([0., 30., 100., 105.])
        block = {'times': slots, 'nodes': np.array([0, 1, 2, 3]), 'positions': np.arange(4)}
        rng = np.random.default_rng(11)
        for _ in range(25):
            result = nm.rotate_times([block], 4, rng)
            self.assertEqual(sorted(result), sorted(slots))
            ranks = np.argsort(np.argsort(result))
            self.assertTrue(np.all((np.diff(ranks) % 4) == 1))

    def test_temporal_positive_and_common_rhythm_negative(self):
        # Two densely publishing channels, one common copy at an identical instant.
        slots = np.arange(120) * 60.
        blocks = [{'times': slots, 'nodes': np.array([i]), 'positions': np.array([60])} for i in range(2)]
        rng = np.random.default_rng(12)
        draws = [int(abs(np.diff(nm.rotate_times(blocks, 2, rng))[0]) <= 1) for _ in range(999)]
        self.assertLess(nm.empirical_summary(1, draws)['p_raw'], .03)
        # If each channel only has one time slot, the same synchrony is inevitable.
        locked = [{'times': np.array([3600.]), 'nodes': np.array([i]), 'positions': np.array([0])} for i in range(2)]
        draws = [int(abs(np.diff(nm.rotate_times(locked, 2, rng))[0]) <= 1) for _ in range(99)]
        self.assertEqual(nm.empirical_summary(1, draws)['p_raw'], 1)

    def test_blocks_condition_on_kyiv_day_channel_and_local_two_hours(self):
        posts = [
            {'channel': 'a', 'url': 'a/1', 'date': '2026-11-02T21:55:00+00:00'},
            {'channel': 'a', 'url': 'a/2', 'date': '2026-11-02T22:05:00+00:00'},
            {'channel': 'b', 'url': 'b/1', 'date': '2026-11-02T22:05:00+00:00'},
            {'channel': 'a', 'url': 'a/3', 'date': '2026-11-03T00:05:00+00:00'},
        ]
        nodes = {p['url']: i for i, p in enumerate(posts)}
        blocks = nm.time_blocks(posts, nodes, 2)
        self.assertEqual(len(blocks), 4)
        self.assertEqual({b['key'] for b in blocks}, {
            ('a', '2026-11-02', 11), ('a', '2026-11-03', 0),
            ('b', '2026-11-03', 0), ('a', '2026-11-03', 1)})

    def test_null_generated_copy_timing_does_not_create_systematic_excess(self):
        # Independent channel phases on a highly nonuniform shared news wave.
        # The null keeps every burst slot: it must not infer fine coordination
        # simply because most publications lie in the same five-minute burst.
        slots = np.r_[np.arange(25) * 10., 3600. + np.arange(5) * 60.]
        blocks = [{'times': slots, 'nodes': np.array([i]), 'positions': np.array([0])}
                  for i in range(6)]
        pairs = np.array([(i, j) for i in range(6) for j in range(i + 1, 6)])
        def statistic(times):
            return int(sum(np.abs(times[pairs[:, 0]] - times[pairs[:, 1]]) <= 30))
        rng = np.random.default_rng(927)
        rejections = 0
        for _ in range(40):
            observed = statistic(nm.rotate_times(blocks, 6, rng))
            draws = [statistic(nm.rotate_times(blocks, 6, rng)) for _ in range(199)]
            rejections += nm.empirical_summary(observed, draws)['p_raw'] <= .05
        self.assertLessEqual(rejections, 5)

    def test_empirical_plus_one_ties_and_holm(self):
        result = nm.empirical_summary(5, [1, 2, 3, 4])
        self.assertEqual(result['p_raw'], .2)
        self.assertEqual(nm.empirical_summary(5, [5, 5])['p_raw'], 1.)
        self.assertEqual(nm.holm_adjust([.01, .04, .03]), [.03, .06, .06])

    def test_cli_accepts_alternate_edge_graph_without_overwriting_default_report(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            edge_path = temp / 'exact_edges.csv'
            edge_path.write_text('earlier_url,later_url\na,b\n', encoding='utf-8')
            out = temp / 'run'
            seen = {}

            def fake_temporal_analysis(B, seed, out):
                seen['edges'] = nm.EDGES
                return ([{'family': 'copy_timing', 'model': 'fake', 'statistic': 'x',
                          'observed': 1, 'null_mean': 0.0, 'null_q025': 0.0, 'null_q975': 0.0,
                          'null_min': 0, 'null_max': 0, 'excess': 1.0, 'ratio': None,
                          'tail_count': 0, 'B': B, 'p_raw': 1/(B+1)}], {'fixture': True})

            args = ['coordination_null_models.py', '--replicates', '9', '--seed', '12',
                    '--edges', str(edge_path), '--out', str(out)]
            fake_s7 = [
                {'family': 'S7', 'model': 'fake', 'statistic': str(index), 'p_raw': 0.2}
                for index in range(4)
            ]
            with patch.object(sys, 'argv', args), \
                    patch.object(nm, 'comment_analysis', return_value=(fake_s7, {'fixture': True})), \
                    patch.object(nm, 'temporal_analysis', side_effect=fake_temporal_analysis):
                nm.main()

            self.assertEqual(seen['edges'], edge_path)
            self.assertEqual(nm.EDGES, nm.ROOT / 'reports/continuation_20261105/verified_similarity_edges.csv')
            result = __import__('json').loads((out / 'results.json').read_text())
            self.assertEqual(result['test_family_size'], 5)
            self.assertTrue(result['copies']['fixture'])


if __name__ == '__main__':
    unittest.main()
