import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/analysis'))
import analyze


class CandidateIndex:
    def __init__(self, **kwargs):
        self.ids = []
    def insert(self, key, value):
        self.ids.append(key)
    def query(self, value):
        # Deliberately return false-positive candidates, which LSH may do.
        return self.ids


class CopySimilarityTests(unittest.TestCase):
    def post(self, channel, text):
        return {'channel': channel, 'text_clean': text,
                'dt': datetime(2026, 11, 2, tzinfo=timezone.utc)}

    def test_lsh_false_positive_does_not_become_a_copy(self):
        posts = [self.post('a', 'абвгд ' * 30), self.post('b', 'яюєжз ' * 30)]
        with patch.object(analyze, 'MinHashLSH', CandidateIndex):
            self.assertEqual(analyze.copy_clusters(posts, thr=0.6), [])

    def test_identical_text_remains_a_copy_after_verification(self):
        posts = [self.post('a', 'довгий однаковий текст ' * 10),
                 self.post('b', 'довгий однаковий текст ' * 10)]
        self.assertEqual(len(analyze.copy_clusters(posts, thr=0.9)), 1)


if __name__ == '__main__':
    unittest.main()
