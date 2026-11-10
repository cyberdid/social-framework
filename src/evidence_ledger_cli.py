"""Local evidence ledger ingestion, export and schema migration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from src.analysis.source_ingest_bridge import ingest_source_watch_manifest
from src.evidence_ledger import EvidenceLedger, SCHEMA_VERSION


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, 'invalid arguments\n')


def _database(path: Path, *, must_exist: bool) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError('invalid database')
    if must_exist and not path.is_file():
        raise ValueError('missing database')
    if not path.parent.is_dir():
        raise ValueError('missing directory')


def main(argv=None) -> int:
    parser = SafeParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True, parser_class=SafeParser)
    for command in ('ingest', 'export-jsonl', 'migrate'):
        sub = commands.add_parser(command)
        sub.add_argument('--database', type=Path, required=True)
        if command == 'ingest':
            sub.add_argument('--manifest', type=Path, required=True)
            sub.add_argument('--root', type=Path, required=True)
        if command == 'export-jsonl':
            sub.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        _database(args.database, must_exist=args.command == 'export-jsonl')
        if args.command == 'ingest':
            if not args.manifest.is_file() or not args.root.is_dir():
                raise ValueError('invalid input')
            # Reject malformed or out-of-root manifests before opening the database.
            args.manifest.resolve(strict=True).relative_to(args.root.resolve(strict=True))
            manifest = json.loads(args.manifest.read_bytes())
            if not isinstance(manifest, dict) or not isinstance(manifest.get('rows'), list):
                raise ValueError('invalid manifest')
        if args.command == 'export-jsonl' and (args.output.is_symlink() or args.output.exists()):
            raise ValueError('output exists')
        with EvidenceLedger(args.database) as ledger:
            if args.command == 'ingest':
                result = ingest_source_watch_manifest(args.manifest, ledger, root=args.root)
                result = {'counts': result['counts'], 'records_added': result['records_added']}
            elif args.command == 'migrate':
                count = ledger._connection.execute('SELECT COUNT(*) FROM records').fetchone()[0]
                result = {'schema_version': SCHEMA_VERSION, 'records': count}
            else:
                data = ledger.to_jsonl().encode('utf-8')
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0)
                fd = os.open(args.output, flags, 0o600)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                except BaseException:
                    args.output.unlink(missing_ok=True)
                    raise
                result = {'sha256': hashlib.sha256(data).hexdigest(), 'records': len(data.splitlines())}
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception:
        print('{"error":"operation_failed"}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
