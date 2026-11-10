"""Decode existing shortcode candidates as estimates; never platform timestamps."""
import csv
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_'


def main():
    source = ROOT / 'data/raw/instagram_browser/20261106T080549Z/hashtag_петропавловскаяборщаговка.txt'
    start = datetime.fromisoformat('2026-11-02T00:00:00+02:00')
    end = datetime.fromisoformat('2026-11-05T12:10:13+02:00')
    rows = []
    for code in dict.fromkeys(source.read_text().split()):
        value = 0
        for c in code:
            value = value * 64 + ALPHABET.index(c)
        stamp = datetime.fromtimestamp(((value >> 23) + 1314220021721) / 1000, timezone.utc).astimezone(ZoneInfo('Europe/Kyiv'))
        rows.append({'shortcode': code, 'url': f'https://www.instagram.com/p/{code}/',
                     'id_time_estimate_kyiv': stamp.isoformat(),
                     'inside_frozen_window': start <= stamp < end,
                     'platform_timestamp_verified': False})
    target = ROOT / 'reports/instagram-access-20261106/candidate-times.csv'
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    print({'candidates': len(rows), 'estimated_in_window': sum(r['inside_frozen_window'] for r in rows)})


if __name__ == '__main__':
    main()
