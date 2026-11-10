"""Fetch a fixed public source watchlist; save new immutable receipts and review queue.

No authentication, cookies retained between requests, browser challenges bypass,
legal inference, messaging, paid services, recursive crawl, or scheduling.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

SCRIPT_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
EXTRACTOR_VERSION = 2
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AUDIT = ROOT / 'reports/source-refresh-url-audit-2026-11-06.json'
RAW_ROOT = ROOT / 'data/raw/source_watch'
REPORT_DIR = ROOT / 'reports/source-watch'
LISTINGS = [
    ('https://mvs.gov.ua/press-center/news', 'Офіційна загальна стрічка МВС; /news є префіксом статей, не стрічкою.'),
    ('https://ngu.gov.ua/', 'Офіційна головна НГУ містить пагіновану стрічку новин.'),
    ('https://dbr.gov.ua/news', 'Офіційна стрічка повідомлень ДБР.'),
    ('https://ombudsman.gov.ua/news', 'Офіційна стрічка «Новини та події» Омбудсмана.'),
]
OFFICIAL = {'mvs.gov.ua', 'ngu.gov.ua', 'dbr.gov.ua', 'ombudsman.gov.ua'}
ALLOWED = OFFICIAL | {'suspilne.media', 'hromadske.ua'}
KEYWORDS = re.compile(r'бор[щш][а-яіїєґ]*г[іи]в|борщан|петропавл|borsh|bor[sc]h|petropav|ladik|ladyk|ladic', re.I)
TRACKING = {'fbclid', 'gclid', 'yclid'}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def normalize_url(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in TRACKING]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(query), ''))


def domain(url):
    return (urlsplit(url).hostname or '').removeprefix('www.').lower()


def validate_public_url(url):
    p = urlsplit(url)
    if p.scheme != 'https' or domain(url) not in ALLOWED or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Only credential-free HTTPS URLs on the fixed public source domains are allowed')


def build_plan(audit_path):
    """Successful corrected official URLs, last DBR spelling, two Ladika reports."""
    audit = json.loads(Path(audit_path).read_text())
    targets = {}
    dbr = []
    for row in audit['rows']:
        url = row['url']
        host = domain(url)
        if host == 'dbr.gov.ua' and '/news/' in url:
            dbr.append(url)
        ladika = ('suspilne.media/kyiv/1446145-' in url or 'hromadske.ua/kryminal/273906-' in url)
        if row.get('status') == 200 and (host in OFFICIAL or ladika):
            targets[url] = {'url': url, 'kind': 'known_article', 'source': str(audit_path)}
    # The later corrected DBR URL remains watched even when TLS validation fails.
    if dbr:
        targets[dbr[-1]] = {'url': dbr[-1], 'kind': 'known_article', 'source': str(audit_path)}
    for url, rationale in LISTINGS:
        targets[url] = {'url': url, 'kind': 'news_listing', 'source': 'fixed_official_listing', 'rationale': rationale}
    for url in targets:
        validate_public_url(url)
    return sorted(targets.values(), key=lambda row: (row['kind'], row['url']))


def extract_content(body, url, kind):
    """Conservative filtered fingerprint. Its changes still require human review."""
    soup = BeautifulSoup(body, 'html.parser')
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    for node in soup.select('script,style,noscript,iframe,svg,form,button,nav,footer,header,aside,[hidden],[aria-hidden="true"]'):
        node.decompose()
    for selector in ['[class*="cookie"]', '[class*="share"]', '[class*="social"]', '[class*="related"]',
                     '[class*="recommend"]', '[class*="similar"]', '[class*="sources"]',
                     '[class*="counter"]', '[class*="views"]', '.article-meta']:
        for node in soup.select(selector):
            node.decompose()
    if kind == 'news_listing':
        links = {}
        host = domain(url)
        candidates = soup.select('h2 a[href],h3 a[href],h4 a[href]') if host == 'ngu.gov.ua' else soup.select('a[href]')
        for anchor in candidates:
            target = normalize_url(urljoin(url, anchor['href']))
            parts = urlsplit(target)
            if parts.scheme != 'https' or domain(target) != host:
                continue
            path = parts.path
            if host in {'mvs.gov.ua', 'dbr.gov.ua'}:
                accepted = path.startswith('/news/') and len(path) > len('/news/')
            elif host == 'ombudsman.gov.ua':
                accepted = path.startswith('/news_details/')
            else:
                accepted = (path.count('/') <= 2 and len(path.strip('/')) > 12 and
                            not any(part in path for part in ['/category/', '/tag/', '/page/', '/author/', '/wp-']))
            label = ' '.join(anchor.get_text(' ', strip=True).split())
            if accepted and len(label) >= 8 and len(label) > len(links.get(target, '')):
                links[target] = label
        items = [{'url': link, 'title': label} for link, label in sorted(links.items())]
        text = '\n'.join(f"{item['url']}\t{item['title']}" for item in items)
        mode = 'news_links' if items else 'fallback_text'
    else:
        selectors = ['.news-details', '.l-article__content', 'article.post-body', '.article__text', '.article-text',
                     '[itemprop="articleBody"]', 'article', 'main', '[role="main"]']
        container = None
        for selector in selectors:
            matches = soup.select(selector)
            if matches:
                container = max(matches, key=lambda item: len(item.get_text(' ', strip=True)))
                break
        text = ' '.join((container or soup).get_text(' ', strip=True).split())
        mode = 'article_text' if container and len(text) >= 80 else 'fallback_text'
        heading = soup.find('h1')
        if mode == 'article_text' and heading and heading not in container.descendants:
            text = ' '.join(heading.get_text(' ', strip=True).split()) + '\n' + text
        items = []
    if mode == 'fallback_text':
        text = ' '.join(soup.get_text(' ', strip=True).split())
    return {'extractor_version': EXTRACTOR_VERSION, 'mode': mode, 'page_title': title, 'filtered_text': text,
            'content_sha256': sha(text.encode()), 'news_links': items,
            'keyword_match': bool(KEYWORDS.search(text + ' ' + url)),
            'warning': 'Filtered content or links are a review signal, not a verified new fact.'}


def fetch_public(url, timeout=20, max_bytes=2_000_000):
    """One bounded, TLS-verified, unauthenticated fetch; at most 4 redirects.

    Session ignores netrc/environment credentials and retains no state afterwards.
    Total budget is checked between redirects and received 64 KiB chunks; a single
    blocking socket read can exceed that deadline by at most its read timeout.
    """
    validate_public_url(url)
    start = time.monotonic()
    current = url
    with requests.Session() as session:
        session.trust_env = False  # no netrc auth, implicit proxy credentials, env CA overrides
        session.headers.update({'User-Agent': 'SocialSourceWatch/1.0 (public research; once daily)',
                                'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.1'})
        session.max_redirects = 4
        for _ in range(5):
            remaining = timeout - (time.monotonic() - start)
            if remaining <= 0:
                raise TimeoutError('Total URL budget exceeded')
            session.cookies.clear()
            with session.get(current, timeout=(min(5, remaining), min(10, remaining)),
                             stream=True, allow_redirects=False) as response:
                if response.is_redirect:
                    target = normalize_url(urljoin(current, response.headers['Location']))
                    validate_public_url(target)
                    if domain(target) != domain(url):
                        raise ValueError('Cross-source redirect requires manual review')
                    current = target
                    continue
                body = bytearray()
                truncated = False
                for chunk in response.iter_content(64 * 1024):
                    if time.monotonic() - start > timeout:
                        raise TimeoutError('Total URL budget exceeded while receiving body')
                    if len(body) + len(chunk) > max_bytes:
                        body.extend(chunk[:max_bytes - len(body)])
                        truncated = True
                        break
                    body.extend(chunk)
                return {'status': response.status_code, 'final_url': current, 'body': bytes(body),
                        'content_type': response.headers.get('Content-Type', ''), 'truncated': truncated,
                        'headers': {k: response.headers[k] for k in ['Content-Type', 'ETag', 'Last-Modified'] if k in response.headers}}
    raise ValueError('Redirect limit exceeded')


def compare_row(row, previous):
    result = dict(row)
    result['previous_snapshot'] = previous.get('_snapshot') if previous else None
    result['previous_sha256'] = previous.get('sha256') if previous else None
    result['new_news_links'] = []
    if row.get('error') or not 200 <= (row.get('status') or 0) < 300:
        result['event'] = 'error'
    elif not previous:
        result['event'] = 'new_page'
    elif row['extracted'].get('extractor_version') != previous['extracted'].get('extractor_version'):
        result['event'] = 'new_baseline'
        result['baseline_reason'] = 'extractor_version_changed'
        result['previous_sha256'] = None
    elif row['extracted']['mode'] != previous['extracted']['mode']:
        result['event'] = 'possible_change'
    elif row['extracted']['mode'] == 'fallback_text':
        result['event'] = 'possible_change' if row['sha256'] != previous['sha256'] else 'unchanged'
    elif row['extracted']['content_sha256'] != previous['extracted']['content_sha256']:
        result['event'] = 'filtered_change'
        if row['extracted']['mode'] == 'news_links':
            old = {item['url'] for item in previous['extracted']['news_links']}
            result['new_news_links'] = [item for item in row['extracted']['news_links'] if item['url'] not in old]
    else:
        result['event'] = 'unchanged'
    return result


def previous_successes(raw_root):
    result = {}
    for path in sorted(Path(raw_root).glob('*/manifest.json'), reverse=True):
        try:
            manifest = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # incomplete/unreadable files never become the baseline
        for row in manifest.get('rows', []):
            if row['url'] in result or row.get('error') or not 200 <= (row.get('status') or 0) < 300:
                continue
            if row.get('extracted'):
                result[row['url']] = {**row, '_snapshot': str(path.parent)}
    return result


def atomic_write(path, text):
    """Reports are replaceable; raw receipts only use exclusive creates below."""
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(path)


def rebuild_report(manifest_path, report_dir):
    manifest_path, report_dir = Path(manifest_path), Path(report_dir)
    data = json.loads(manifest_path.read_text())
    for row in data['rows']:
        if row.get('body_file'):
            if sha((manifest_path.parent / row['body_file']).read_bytes()) != row['sha256']:
                raise ValueError('Raw body SHA256 mismatch; report rebuild stopped')
    summary = {'scope': 'Manual verification queue; no automatic factual or legal conclusions.',
               'snapshot': data['snapshot'], 'manifest': str(manifest_path),
               'counts': dict(Counter(row['event'] for row in data['rows'])),
               'rows': data['rows']}
    report_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(report_dir / 'latest.json', json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    labels = {'new_page': 'Перше отримання / базова версія', 'new_baseline': 'Нова базова версія extractor', 'filtered_change': 'Зміна відфільтрованого вмісту',
              'possible_change': 'Можлива зміна / потрібен ручний перегляд', 'unchanged': 'Відфільтрований вміст без змін', 'error': 'Помилка доступу'}
    lines = ['# Щоденна черга перевірки джерел', '', f"Знімок UTC: `{data['snapshot']}`. Маніфест: `{manifest_path}`.", '',
             'Це черга ручної перевірки. Зміна HTML, тексту або посилань не є підтвердженням нової події чи юридичного висновку. '
             'Помилка доступу не доводить відсутності публікації. Перший знімок задає базову версію; новизну новини він не встановлює.', '',
             '| Стан | HTTP | Джерело | Підстава |', '|---|---:|---|---|']
    for row in data['rows']:
        note = row.get('error') or row.get('extracted', {}).get('mode', '')
        note = note.replace('|', '/').replace('\n', ' ')[:250]
        title = row.get('extracted', {}).get('page_title') or row['url']
        title = title.replace('|', '/').replace('[', '').replace(']', '')
        lines.append(f"| {labels[row['event']]} | {row.get('status') or '—'} | [{title}]({row['url']}) | {note} |")
    lines.extend(['', '## Нові доступні посилання після попереднього успішного отримання', ''])
    links = [(row, item) for row in data['rows'] for item in row.get('new_news_links', [])]
    if not links:
        lines.append('Не виявлено або ще немає попередньої базової версії. Це не гарантує відсутності новин.')
    for row, item in links:
        priority = ' — збіг ключових слів' if KEYWORDS.search(item['url'] + ' ' + item['title']) else ''
        title = item['title'].replace('[', '').replace(']', '')
        lines.append(f"- [{title}]({item['url']}){priority}")
    atomic_write(report_dir / 'latest.md', '\n'.join(lines) + '\n')
    return summary


def run(plan, raw_root=RAW_ROOT, report_dir=REPORT_DIR, *, dry_run=False, timeout=20, pause=1, max_bytes=2_000_000):
    if dry_run:
        return {'dry_run': True, 'network_requests': 0, 'targets': plan}
    previous = previous_successes(raw_root)
    snapshot = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    folder = Path(raw_root) / snapshot
    folder.mkdir(parents=True, exist_ok=False)
    rows = []
    for index, target in enumerate(plan):
        if index:
            time.sleep(pause)
        row = {**target, 'requested_at': datetime.now(timezone.utc).isoformat(),
               'request_timeout_seconds': timeout, 'status': None, 'final_url': target['url'], 'sha256': None}
        start = time.monotonic()
        try:
            receipt = dict(fetch_public(target['url'], timeout=timeout, max_bytes=max_bytes))
            body = receipt.pop('body', None)
            row.update(receipt)
            if body is not None:
                row['body_file'] = sha(target['url'].encode())[:20] + '.html'
                with (folder / row['body_file']).open('xb') as handle:
                    handle.write(body)
                row['bytes'] = len(body)
                row['sha256'] = sha(body)
                if row.get('truncated'):
                    row['error'] = 'body_size_limit'
                elif not 200 <= (row.get('status') or 0) < 300:
                    row['error'] = f"HTTP {row.get('status')}"
                elif 'html' not in row.get('content_type', '').lower():
                    row['error'] = 'unsupported_content_type'
                else:
                    extracted = extract_content(body, row['final_url'], target['kind'])
                    row['extracted'] = extracted
                    if re.search(r'attention required|just a moment|access denied|captcha', extracted['page_title'], re.I):
                        row['error'] = 'access_challenge'
        except (requests.RequestException, OSError, ValueError) as error:
            row['error'] = type(error).__name__ + ': ' + str(error)[:500]
        row['elapsed_seconds'] = round(time.monotonic() - start, 3)
        rows.append(compare_row(row, previous.get(target['url'])))
    manifest = {'schema_version': 1, 'snapshot': snapshot,
                'started_utc': snapshot, 'finished_utc': datetime.now(timezone.utc).isoformat(),
                'script_sha256': SCRIPT_SHA256, 'script_hash_captured': 'module_import_before_network',
                'targets': plan, 'rows': rows,
                'scope': 'Public source availability/content watch; manual review queue only.',
                'network_policy': {'timeout_seconds': timeout, 'pause_seconds': pause, 'max_bytes': max_bytes,
                                   'tls_verification': True, 'authentication': False, 'max_redirects': 4}}
    manifest_path = folder / 'manifest.json'
    with manifest_path.open('x', encoding='utf-8') as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)
        handle.write('\n')
    return rebuild_report(manifest_path, report_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit', type=Path, default=DEFAULT_AUDIT)
    parser.add_argument('--raw-root', type=Path, default=RAW_ROOT)
    parser.add_argument('--report-dir', type=Path, default=REPORT_DIR)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--rebuild', type=Path, help='Rebuild reports from an existing manifest without network')
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--pause', type=float, default=1)
    parser.add_argument('--max-bytes', type=int, default=2_000_000)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 60 or not 0 <= args.pause <= 60 or not 1024 <= args.max_bytes <= 10_000_000:
        parser.error('timeout: 1..60; pause: 0..60; max-bytes: 1024..10000000')
    if args.rebuild:
        if args.dry_run:
            parser.error('--rebuild and --dry-run are mutually exclusive')
        result = rebuild_report(args.rebuild, args.report_dir)
    else:
        result = run(build_plan(args.audit), args.raw_root, args.report_dir, dry_run=args.dry_run,
                     timeout=args.timeout, pause=args.pause, max_bytes=args.max_bytes)
    print(json.dumps(result if args.dry_run else {key: result[key] for key in ['snapshot', 'manifest', 'counts']},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
