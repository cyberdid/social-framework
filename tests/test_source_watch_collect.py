"""Offline, behavior-level tests for the public source watcher."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import source_watch_collect as watch


class FakeResponse:
    def __init__(self, *, redirect=None, body=b'example'):
        self.status_code = 302 if redirect else 200
        self.is_redirect = bool(redirect)
        self.headers = {'Location': redirect} if redirect else {'Content-Type': 'text/html'}
        self.body = body
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def iter_content(self, size): yield self.body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}
        self.cookies = type('Cookies', (), {'clear': lambda self: None})()
        self.trust_env = True
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class SourceWatchTests(unittest.TestCase):
    def extract(self, html, kind='known_article', url='https://mvs.gov.ua/news/example'):
        return watch.extract_content(html.encode(), url, kind)

    def test_article_ignores_dynamic_chrome_but_detects_material_edit(self):
        text = 'Офіційний текст повідомлення про перебіг перевірки. ' * 4
        a = self.extract(f'<title>Новина</title><nav>123</nav><article><h1>Перевірка</h1><p>{text}</p></article><script>token=1</script>')
        b = self.extract(f'<title>Новина</title><nav>456</nav><article><h1>Перевірка</h1><p>{text}</p></article><script>token=2</script>')
        c = self.extract(f'<article><h1>Перевірка</h1><p>{text} Уточнення від установи.</p></article>')
        self.assertEqual(a['content_sha256'], b['content_sha256'])
        self.assertNotEqual(a['content_sha256'], c['content_sha256'])
        self.assertEqual(a['mode'], 'article_text')

    def test_article_excludes_hidden_sources_and_similar_materials(self):
        body = 'Офіційний основний текст повідомлення. ' * 5
        a = self.extract(
            '<article class="article"><h1>Повідомлення</h1><p>' + body + '</p>'
            '<div class="sources"><div>ДЖЕРЕЛА УНІАН</div></div>'
            '<div class="similar-materials">Схожі матеріали старий текст</div></article>'
        )
        b = self.extract(
            '<article class="article"><h1>Повідомлення</h1><p>' + body + '</p>'
            '<div class="sources"><div>ДЖЕРЕЛА інший медіа-ресурс</div></div>'
            '<div class="similar-materials">Схожі матеріали інша рекомендація</div></article>'
        )
        self.assertEqual(a['filtered_text'], b['filtered_text'])
        self.assertNotIn('ДЖЕРЕЛА', a['filtered_text'])
        self.assertNotIn('Схожі матеріали', a['filtered_text'])
        self.assertEqual(a['extractor_version'], 2)

    def test_listing_compares_available_news_links_not_calendar_or_tokens(self):
        a = self.extract('<nav>Сьогодні 1</nav><a href="/news/first?utm_source=x">Повідомлення</a><script>nonce=1</script>', 'news_listing')
        b = self.extract('<nav>Сьогодні 2</nav><a href="/news/first?utm_source=y">Повідомлення</a><script>nonce=2</script>', 'news_listing')
        c = self.extract('<a href="/news/second">Нове повідомлення</a>', 'news_listing')
        self.assertEqual(a['content_sha256'], b['content_sha256'])
        self.assertNotEqual(a['content_sha256'], c['content_sha256'])
        self.assertEqual(a['news_links'][0]['url'], 'https://mvs.gov.ua/news/first')

    def test_unsupported_html_only_promises_possible_change(self):
        a = self.extract('<div>A tiny dynamic page</div>')
        self.assertEqual(a['mode'], 'fallback_text')
        row = {'url': 'https://mvs.gov.ua/news/x', 'status': 200, 'sha256': 'new', 'extracted': a}
        before = {'url': row['url'], 'status': 200, 'sha256': 'old', 'extracted': a}
        self.assertEqual(watch.compare_row(row, before)['event'], 'possible_change')

    def test_status_and_challenge_errors_never_become_news(self):
        for row in [
            {'status': 403, 'error': 'HTTP 403'},
            {'status': None, 'error': 'TLS failure'},
            {'status': 200, 'error': 'access_challenge'},
        ]:
            self.assertEqual(watch.compare_row({'url': 'https://mvs.gov.ua/', **row}, None)['event'], 'error')

    def test_selects_corrected_known_urls_and_four_listings(self):
        plan = watch.build_plan(watch.DEFAULT_AUDIT)
        listings = [p for p in plan if p['kind'] == 'news_listing']
        self.assertEqual(len(listings), 4)
        self.assertIn('https://mvs.gov.ua/press-center/news', {p['url'] for p in listings})
        self.assertEqual(len(plan), 12)
        self.assertTrue(any('provadzhennya-' in p['url'] for p in plan))
        self.assertFalse(any('sluzhbove-' in p['url'] for p in plan))

    def test_fetch_caps_body_and_uses_finite_timeouts_without_implicit_auth(self):
        session = FakeSession([FakeResponse(body=b'x' * 100)])
        with patch.object(watch.requests, 'Session', return_value=session):
            row = watch.fetch_public('https://mvs.gov.ua/news/x', max_bytes=20)
        self.assertTrue(row['truncated'])
        self.assertEqual(len(row['body']), 20)
        self.assertFalse(session.trust_env)
        self.assertLessEqual(session.calls[0][1]['timeout'][0], 5)
        self.assertLessEqual(session.calls[0][1]['timeout'][1], 10)
        self.assertFalse(session.calls[0][1]['allow_redirects'])

    def test_fetch_never_follows_cross_source_or_private_redirects(self):
        for location in ['https://127.0.0.1/admin', 'https://hromadske.ua/']:
            session = FakeSession([FakeResponse(redirect=location)])
            with patch.object(watch.requests, 'Session', return_value=session):
                with self.assertRaises(ValueError):
                    watch.fetch_public('https://mvs.gov.ua/news/x')
            self.assertEqual(len(session.calls), 1)
        for url in ['http://mvs.gov.ua/', 'https://user:password@mvs.gov.ua/', 'https://example.org/']:
            with self.assertRaises(ValueError):
                watch.validate_public_url(url)

    def test_headline_change_and_extractor_version_change_are_visible(self):
        body = '<div class="l-article__content">' + 'Основний текст статті. ' * 6 + '</div>'
        a = self.extract('<h1>Перший заголовок</h1>' + body)
        b = self.extract('<h1>Уточнений заголовок</h1>' + body)
        self.assertNotEqual(a['content_sha256'], b['content_sha256'])
        row = {'url': 'https://mvs.gov.ua/news/x', 'status': 200, 'sha256': 'same', 'extracted': a}
        before = {**row, 'extracted': {**a, 'extractor_version': 0}}
        self.assertEqual(watch.compare_row(row, before)['event'], 'new_baseline')

    def test_dry_run_never_fetches_or_creates_raw_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / 'raw'
            with patch.object(watch, 'fetch_public', side_effect=AssertionError('Network forbidden')):
                result = watch.run(watch.build_plan(watch.DEFAULT_AUDIT), raw, Path(tmp) / 'reports', dry_run=True)
            self.assertFalse(raw.exists())
            self.assertEqual(len(result['targets']), 12)

    def test_immutable_snapshots_and_rebuild_and_last_success_after_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / 'raw'
            reports = Path(tmp) / 'reports'
            plan = [{'url': 'https://mvs.gov.ua/news/x', 'kind': 'known_article', 'source': 'fixture'}]
            body = ('<article><p>' + 'Перевірений зміст повідомлення. ' * 6 + '</p></article>').encode()
            response = {'status': 200, 'final_url': plan[0]['url'], 'content_type': 'text/html; charset=utf-8', 'body': body}
            with patch.object(watch, 'fetch_public', return_value=response):
                first = watch.run(plan, raw, reports, pause=0)
            previous = {p: p.read_bytes() for p in raw.rglob('*') if p.is_file()}
            with patch.object(watch, 'fetch_public', return_value={'status': None, 'final_url': plan[0]['url'], 'error': 'TLS failure'}):
                failure = watch.run(plan, raw, reports, pause=0)
            with patch.object(watch, 'fetch_public', return_value=response):
                third = watch.run(plan, raw, reports, pause=0)
            self.assertEqual(first['rows'][0]['event'], 'new_page')
            self.assertEqual(failure['rows'][0]['event'], 'error')
            self.assertEqual(third['rows'][0]['event'], 'unchanged')
            for p, contents in previous.items():
                self.assertEqual(p.read_bytes(), contents)
            for manifest in raw.glob('*/manifest.json'):
                data = json.loads(manifest.read_text())
                for row in data['rows']:
                    if row.get('body_file'):
                        self.assertEqual(hashlib.sha256((manifest.parent / row['body_file']).read_bytes()).hexdigest(), row['sha256'])
            before = (reports / 'latest.json').read_bytes()
            watch.rebuild_report(Path(third['manifest']), reports)
            self.assertEqual(before, (reports / 'latest.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
