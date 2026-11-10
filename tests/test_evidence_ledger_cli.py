import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from src.evidence_ledger import EvidenceLedger, LedgerError


def cli(*args):
    return subprocess.run([sys.executable, '-m', 'src.evidence_ledger_cli', *map(str, args)], capture_output=True, text=True)


def test_migration_versions_and_preserves_legacy(tmp_path):
    db = tmp_path / 'legacy.db'
    with EvidenceLedger(db) as ledger:
        ledger.add_source('s', uri='https://example.org/', title='private')
        before = ledger.to_jsonl()
    with sqlite3.connect(db) as conn:
        conn.execute('PRAGMA user_version=0')
    for _ in range(2):
        result = cli('migrate', '--database', db)
        assert result.returncode == 0
        assert json.loads(result.stdout)['schema_version'] == 1
    with EvidenceLedger(db) as ledger:
        assert ledger.to_jsonl() == before
    with sqlite3.connect(db) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1


def test_rejects_future_and_invalid_without_modification(tmp_path):
    for future in (False, True):
        db = tmp_path / str(future)
        with sqlite3.connect(db) as conn:
            conn.execute('CREATE TABLE records (secret TEXT)')
            if future:
                conn.execute('PRAGMA user_version=99')
        before = db.read_bytes()
        result = cli('migrate', '--database', db)
        assert result.returncode == 1
        assert db.read_bytes() == before
        with pytest.raises(LedgerError):
            EvidenceLedger(db)


def test_ingest_export_and_safe_errors(tmp_path):
    root = tmp_path / 'capture'; root.mkdir()
    body = b'private body'; (root / 'body.html').write_bytes(body)
    manifest = root / 'manifest.json'
    row = {'url': 'https://example.org/secret', 'final_url': 'https://example.org/secret',
           'body_file': 'body.html', 'bytes': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
           'requested_at': '2026-01-01T00:00:00Z', 'content_type': 'text/html', 'status': 200}
    manifest.write_text(json.dumps({'rows': [row]}))
    db = tmp_path / 'ledger.db'
    for _ in range(2):
        result = cli('ingest', '--manifest', manifest, '--root', root, '--database', db)
        assert result.returncode == 0
        summary = json.loads(result.stdout)
        assert summary['counts']['unsupported_extraction'] == 1
        assert 'secret' not in result.stdout
    with EvidenceLedger(db) as ledger:
        expected = ledger.to_jsonl()
        assert len(expected.splitlines()) == 2
    out = tmp_path / 'export.jsonl'
    result = cli('export-jsonl', '--database', db, '--output', out)
    assert result.returncode == 0
    assert out.read_text() == expected
    assert json.loads(result.stdout)['sha256'] == hashlib.sha256(out.read_bytes()).hexdigest()
    assert os.stat(out).st_mode & 0o777 == 0o600
    with EvidenceLedger(tmp_path / 'copy.db') as ledger:
        assert ledger.import_jsonl(out.read_text()) == 2
        assert ledger.to_jsonl() == expected
    assert cli('export-jsonl', '--database', db, '--output', out).returncode == 1
    link = tmp_path / 'link'; link.symlink_to(db)
    assert cli('migrate', '--database', link).returncode == 1
    target = tmp_path / 'target'; target.symlink_to(out)
    assert cli('export-jsonl', '--database', db, '--output', target).returncode == 1
    result = cli('ingest', '--manifest', tmp_path / 'secret-missing', '--root', root, '--database', db)
    assert result.returncode == 1
    assert 'secret' not in result.stdout + result.stderr


def test_migrate_new_database_and_reject_extra_schema_objects(tmp_path):
    db = tmp_path / 'new.db'
    assert cli('migrate', '--database', db).returncode == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0] == 1
        conn.execute('CREATE TABLE unrelated (x TEXT)')
    before = db.read_bytes()
    assert cli('migrate', '--database', db).returncode == 1
    assert db.read_bytes() == before
