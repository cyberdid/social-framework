import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src/analysis"))
import coordination_lsh_audit


class CoordinationLshAuditTests(unittest.TestCase):
    def post(self, channel, url, text, date):
        return {"channel": channel, "url": url, "text_clean": text, "date": date}

    def test_exhaustive_edges_count_all_shared_shingles_without_small_integer_overflow(self):
        shared = " ".join(f"token{i:04d}" for i in range(180))
        posts = [
            self.post("a", "https://t.me/a/1", shared,
                      "2026-11-02T10:00:00+02:00"),
            self.post("b", "https://t.me/b/1", shared,
                      "2026-11-02T10:05:00+02:00"),
            self.post("a", "https://t.me/a/2", shared,
                      "2026-11-02T10:06:00+02:00"),
        ]

        edges = coordination_lsh_audit.exact_cross_channel_edges(posts, minimum=0.6)

        self.assertEqual(len(edges), 2)
        self.assertEqual([row["jaccard"] for row in edges], [1.0, 1.0])
        first = next(row for row in edges if row["earlier_url"] == "https://t.me/a/1")
        self.assertEqual(first["later_url"], "https://t.me/b/1")
        self.assertEqual(first["lag_minutes"], 5.0)

    def test_candidate_recall_is_reported_by_threshold_and_counts_misses(self):
        exact = [
            {"earlier_url": "a", "later_url": "b", "jaccard": 0.9},
            {"earlier_url": "a", "later_url": "c", "jaccard": 0.7},
            {"earlier_url": "b", "later_url": "c", "jaccard": 0.6},
        ]
        candidate_pairs = {("a", "b"), ("a", "c"), ("x", "y")}

        results = coordination_lsh_audit.summarize_candidate_recall(
            exact, candidate_pairs, thresholds=(0.6, 0.75, 0.9)
        )

        self.assertEqual(results[0]["exact_pairs_n"], 3)
        self.assertEqual(results[0]["returned_pairs_n"], 2)
        self.assertEqual(results[0]["missed_pairs_n"], 1)
        self.assertEqual(results[0]["candidate_pairs_not_in_exact_universe_n"], 1)
        self.assertAlmostEqual(results[0]["recall"], 2 / 3)
        self.assertEqual(results[1]["exact_pairs_n"], 1)
        self.assertEqual(results[1]["returned_pairs_n"], 1)
        self.assertEqual(results[1]["recall"], 1.0)


if __name__ == "__main__":
    unittest.main()
