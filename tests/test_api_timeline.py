import asyncio
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))


class TimelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_window_excludes_other_dates_without_truncating_valid_posts(self):
        import tg_api_snapshot as api
        start = datetime(2026, 11, 2, tzinfo=timezone.utc)
        end = datetime(2026, 11, 5, tzinfo=timezone.utc)
        messages = [SimpleNamespace(id=i, date=datetime(2026, 11, day, tzinfo=timezone.utc))
                    for i, day in [(4, 5), (3, 4), (2, 2), (1, 1)]]
        class Client:
            async def iter_messages(self, *args, **kwargs):
                for m in messages:
                    yield m
        got, status = await api.harvest_timeline(Client(), None, start, end, 100)
        self.assertEqual([m.id for m in got], [3, 2])
        self.assertEqual(status, 'complete')

    async def test_cap_is_reported_as_incomplete(self):
        import tg_api_snapshot as api
        start = datetime(2026, 11, 2, tzinfo=timezone.utc)
        end = datetime(2026, 11, 5, tzinfo=timezone.utc)
        class Client:
            async def iter_messages(self, *args, **kwargs):
                for i in range(3):
                    yield SimpleNamespace(id=i, date=datetime(2026, 11, 4, tzinfo=timezone.utc))
        got, status = await api.harvest_timeline(Client(), None, start, end, 3)
        self.assertEqual(len(got), 3)
        self.assertEqual(status, 'cap_reached')

    async def test_rate_limit_preserves_already_received_messages(self):
        import tg_api_snapshot as api
        from telethon.errors import FloodWaitError
        start = datetime(2026, 11, 2, tzinfo=timezone.utc)
        end = datetime(2026, 11, 5, tzinfo=timezone.utc)
        class Client:
            async def iter_messages(self, *args, **kwargs):
                yield SimpleNamespace(id=1, date=datetime(2026, 11, 4, tzinfo=timezone.utc))
                raise FloodWaitError(request=None, capture=120)
        got, status = await api.harvest_timeline(Client(), None, start, end, 100)
        self.assertEqual([m.id for m in got], [1])
        self.assertEqual(status, 'FloodWaitError:120')


if __name__ == '__main__':
    unittest.main()
