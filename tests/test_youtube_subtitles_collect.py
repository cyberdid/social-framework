import importlib.util
from pathlib import Path
import unittest

P = Path(__file__).resolve().parents[1] / 'src/youtube_subtitles_collect.py'

class SubtitleTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('collector', P)
        self.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.m)

    def test_vtt_strips_timing_tags_and_rolling_repeats(self):
        vtt='WEBVTT\n\n00:00:01.000 --> 00:00:03.000 align:start\n<c>Це &amp; тест</c>\n\n00:00:03.000 --> 00:00:05.000\nЦе &amp; тест\nдруга фраза\n'
        self.assertEqual(self.m.parse_vtt(vtt), [{'start':1.0,'end':3.0,'text':'Це & тест'}, {'start':3.0,'end':5.0,'text':'друга фраза'}])

    def test_blockage_not_mislabelled_as_missing_subtitles(self):
        self.assertEqual(self.m.failure_reason('HTTP Error 429: Too Many Requests'), 'rate_limited')
        self.assertEqual(self.m.failure_reason("Sign in to confirm you’re not a bot"), 'bot_blocked')
        self.assertEqual(self.m.failure_reason('Video unavailable'), 'unavailable')

    def test_top_and_strata_are_unique_and_cover_missing_day(self):
        records=[{'id':'a','view_count':100,'upload_date':'20261102','duration':50,'title':'ТЦК'}, {'id':'b','view_count':90,'upload_date':'20261102','duration':400,'title':'ТЦК'}, {'id':'c','view_count':1,'upload_date':'20261103','duration':50,'title':'Поліція'}]
        selection=self.m.select_videos(records,2)
        self.assertEqual([r['video_id'] for r in selection], ['a','b','c'])
        self.assertEqual(selection[2]['selection_reason'], 'supplement:day:20261103;day_format:20261103:short_le180;marker:police_investigation')

if __name__=='__main__': unittest.main()
