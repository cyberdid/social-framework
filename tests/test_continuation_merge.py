"""Fixture-level contracts for immutable web/API continuation merging."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/analysis'))

BASE = '20261105T101013Z'
EXPANSION = '20261105T102914Z'
WHEN = '2026-11-02T12:00:00+00:00'
FETCHED = '2026-11-05T10:40:00+00:00'


class ContinuationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.snapshot('telegram_web', BASE, [])
        self.snapshot('telegram_web', EXPANSION, [])

    def snapshot(self, kind, name, posts, channels=(), statuses=None, unvisited=()):
        folder = self.root / 'data/raw' / kind / name
        folder.mkdir(parents=True, exist_ok=True)
        filename = 'posts.jsonl' if kind == 'telegram_web' else 'messages.jsonl'
        for filename_, rows in ((filename, posts), ('channels.jsonl', channels)):
            (folder / filename_).write_text(''.join(json.dumps(p) + '\n' for p in rows))
        manifest = {
            'snapshot_utc': name, 'finished_utc': FETCHED,
            'window': ['2026-11-02T00:00:00+02:00', '2026-11-05T10:10:13+00:00'],
            'channels': statuses or {}, 'unvisited': list(unvisited),
            'sha256': {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in folder.glob('*.jsonl')},
        }
        (folder / 'manifest.json').write_text(json.dumps(manifest))
        return folder

    def web(self, channel, msg_id, **changes):
        row = dict(channel=channel, msg_id=msg_id, date=WHEN, text='Борщагівка НГУ',
                   views=100, fetched_at=FETCHED, url=f'https://t.me/{channel}/{msg_id}')
        row.update(changes)
        return row

    def api(self, username, msg_id, **changes):
        row = dict(chat_username=username, chat_id=-1000000000010, msg_id=msg_id,
                   date=WHEN, text='Борщагівка НГУ', views=123, fetched_at=FETCHED,
                   grouped_id=None, fwd_from=None, media=None, reactions=None,
                   urls=[], found_by='full_timeline')
        row.update(changes)
        return row

    def build(self):
        import build_continuation
        quality = build_continuation.build(self.root)
        folder = self.root / 'data/processed/continuation_20261105'
        read = lambda name: [json.loads(s) for s in (folder / name).read_text().splitlines()]
        return quality, read('posts_clean.jsonl'), read('messages_clean.jsonl'), folder

    def test_alias_overlap_uses_latest_api_without_losing_web_only_posts(self):
        # Catches channel-level dedupe, ignored aliases, and latest-web-over-API priority.
        self.snapshot('telegram_web', BASE, [self.web('old_name', 1, views=999,
                      fetched_at='2026-11-05T11:30:00+00:00'), self.web('old_name', 2)])
        self.snapshot('telegram_web', EXPANSION, [self.web('old_name', 3)])
        meta = dict(chat_id=-1000000000010, username='new_name', requested_username='old_name',
                    participants=1500, fetched_at=FETCHED, title='Renamed')
        self.snapshot('telegram_api', 'api1', [self.api('new_name', 1, views=150)], [meta])
        long_text = 'Петропавлівська Борщагівка НГУ ' + 'повний текст ' * 60
        self.snapshot('telegram_api', 'api2', [self.api('new_name', 1, views=177,
                      text=long_text, fetched_at='2026-11-05T10:50:00+00:00')], [meta])
        quality, posts, messages, folder = self.build()
        self.assertEqual([(p['channel'], p['msg_id']) for p in posts],
                         [('new_name', 1), ('new_name', 2), ('new_name', 3)])
        self.assertEqual(posts[0]['views'], 177)
        self.assertEqual(posts[0]['text'], long_text)
        self.assertEqual(posts[0]['source_presence'], 'overlap')
        self.assertEqual(len(posts[0]['sources']), 3)
        self.assertEqual(posts[1]['source_presence'], 'web_only')
        self.assertNotIn('deleted', posts[1])
        self.assertEqual(quality['overlap_messages'], 1)
        self.assertEqual(quality['web_only_messages'], 2)
        import csv
        with (folder / 'channels_features.csv').open() as f:
            features = list(csv.DictReader(f))
        self.assertEqual(features[0]['subscribers'], '1500')

    def test_window_is_inclusive_start_exclusive_end_in_absolute_time(self):
        self.snapshot('telegram_web', BASE, [
            self.web('chan', 1) | {'date': '2026-11-01T21:59:59+00:00'},
            self.web('chan', 2) | {'date': '2026-11-02T00:00:00+02:00'},
            self.web('chan', 3) | {'date': '2026-11-05T10:10:12+00:00'},
            self.web('chan', 4) | {'date': '2026-11-05T12:10:13+02:00'},
        ])
        quality, posts, _, _ = self.build()
        self.assertEqual([p['msg_id'] for p in posts], [2, 3])
        self.assertEqual(quality['excluded_outside_window'], 2)

    def test_albums_project_caption_once_without_summing_views_or_guessing_web_groups(self):
        self.snapshot('telegram_web', BASE, [self.web('webchan', 20) | {'n_media': 3},
                                            self.web('webchan', 21) | {'n_media': 3}])
        self.snapshot('telegram_api', 'album', [
            self.api('chan', 10, grouped_id=9, text='', views=100),
            self.api('chan', 11, grouped_id=9, text='Борщагівка НГУ підвал', views=102),
            self.api('other', 12, chat_id=-1000000000020, grouped_id=9),
        ])
        quality, posts, messages, _ = self.build()
        self.assertEqual(len(messages), 5)
        self.assertEqual(len(posts), 4)
        album = next(p for p in posts if p['channel'] == 'chan')
        self.assertEqual(album['msg_id'], 11)
        self.assertEqual(album['album_member_ids'], [10, 11])
        self.assertEqual(album['views'], 102)
        self.assertTrue(album['relevant'])
        self.assertEqual(quality['albums']['collapsed_messages'], 1)
        self.assertEqual(quality['albums']['known_groups'], 2)

    def test_missing_username_resolves_by_channel_id_and_records_incomplete_coverage(self):
        statuses = {'alias': {'status': 'alias', 'canonical': 'renamed'},
                    'renamed': {'status': 'cap_reached', 'channel_id': 10,
                                'canonical_username': 'renamed'}}
        self.snapshot('telegram_web', BASE, [self.web('alias', 1)])
        self.snapshot('telegram_api', 'partial', [self.api(None, 1, text=None, views=None)],
                      [dict(chat_id=-1000000000010, username='renamed', fetched_at=FETCHED)],
                      statuses=statuses, unvisited=['not_visited'])
        quality, posts, _, _ = self.build()
        self.assertEqual([(p['channel'], p['msg_id']) for p in posts], [('renamed', 1)])
        self.assertIsNone(posts[0]['views'])
        self.assertEqual(posts[0]['text_clean'], '')
        self.assertEqual(quality['missing']['messages_views'], 1)
        self.assertEqual(quality['missing']['messages_text'], 1)
        cov = next(s for s in quality['coverage'] if s['kind'] == 'telegram_api')
        self.assertEqual(cov['unvisited'], ['not_visited'])
        self.assertEqual(cov['channels']['renamed']['status'], 'cap_reached')

    def test_api_forward_origin_and_media_are_normalized_without_losing_raw_fields(self):
        self.snapshot('telegram_api', 'forward', [self.api('chan', 1,
                      media='MessageMediaPhoto',
                      fwd_from={'from_id': -1000000000020, 'channel_post': 7,
                                'from_name': None, 'date': WHEN})],
                      [dict(chat_id=-1000000000020, username='origin',
                            requested_username='old_origin', fetched_at=FETCHED)])
        _, posts, _, _ = self.build()
        self.assertEqual(posts[0]['fwd_channel'], 'origin')
        self.assertEqual(posts[0]['fwd_msg_id'], 7)
        self.assertEqual(posts[0]['media'], ['photo'])
        self.assertEqual(posts[0]['media_api'], 'MessageMediaPhoto')
        self.assertEqual(posts[0]['fwd_from']['from_id'], -1000000000020)

    def test_unfinished_snapshot_is_not_read_and_hash_mismatch_stops_build(self):
        active = self.root / 'data/raw/telegram_api/still_running'
        active.mkdir(parents=True)
        (active / 'messages.jsonl').write_text('not complete json')
        quality, _, _, _ = self.build()
        self.assertIn('still_running', quality['skipped_unfinished_api_snapshots'])
        folder = self.snapshot('telegram_api', 'broken', [self.api('chan', 1)])
        with (folder / 'messages.jsonl').open('a') as f:
            f.write('{}\n')
        import build_continuation
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            build_continuation.build(self.root)


if __name__ == '__main__':
    unittest.main()
