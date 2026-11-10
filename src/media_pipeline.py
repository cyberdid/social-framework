"""Read-only local media indexing for JSONL manifests.

The indexer never downloads media. Input rows identify an existing local file
with ``local_path`` and retain ``source_locator``, ``fetched_at`` and
``provenance`` under ``source`` in the output. Verified file/probe facts live
under ``media_index.verified_media_facts``; caller-supplied observations remain
inside the unchanged ``source`` row.

CLI: ``python src/media_pipeline.py INPUT.jsonl OUTPUT.jsonl [--strict-decode]``
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import urlsplit

CHUNK_SIZE = 1024 * 1024
PROBE_TIMEOUT_SECONDS = 120

_EXTENSION_TYPES = {
    '.3gp': 'video/3gpp', '.3g2': 'video/3gpp2', '.avi': 'video/x-msvideo',
    '.m2ts': 'video/mp2t', '.m4v': 'video/mp4', '.mkv': 'video/x-matroska',
    '.mov': 'video/quicktime', '.mp4': 'video/mp4', '.mpeg': 'video/mpeg',
    '.mpg': 'video/mpeg', '.mts': 'video/mp2t', '.ogv': 'video/ogg',
    '.ts': 'video/mp2t', '.webm': 'video/webm', '.wmv': 'video/x-ms-wmv',
    '.aac': 'audio/aac', '.flac': 'audio/flac', '.m4a': 'audio/mp4',
    '.mp3': 'audio/mpeg', '.oga': 'audio/ogg', '.ogg': 'audio/ogg',
    '.opus': 'audio/opus', '.wav': 'audio/wav', '.wma': 'audio/x-ms-wma',
    '.gif': 'image/gif', '.jpeg': 'image/jpeg', '.jpg': 'image/jpeg',
    '.png': 'image/png', '.webp': 'image/webp', '.bmp': 'image/bmp',
}


def sha256_file(path: str | os.PathLike[str]) -> str:
    """Return a streaming SHA-256 digest without changing the input file."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(CHUNK_SIZE), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _command(command: str | os.PathLike[str] | Sequence[str], args: Sequence[str]) -> list[str]:
    if isinstance(command, (str, os.PathLike)):
        return [os.fspath(command), *args]
    return [*(os.fspath(part) for part in command), *args]


def _is_local_path(path: str | os.PathLike[str]) -> bool:
    # ffmpeg accepts URLs itself, so reject schemes before passing a path to it.
    if '\x00' in os.fspath(path):
        return False
    try:
        parsed = urlsplit(os.fspath(path))
    except ValueError:
        return False
    return not parsed.scheme and not parsed.netloc


def _resolved_local_path(path: str | os.PathLike[str]) -> str | None:
    try:
        return str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError, ValueError):
        return None


def probe_media(path: str | os.PathLike[str], ffprobe: str | os.PathLike[str] | Sequence[str] = 'ffprobe',
                timeout: float | None = PROBE_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Run ffprobe locally and return its parsed JSON plus an explicit status."""
    if not _is_local_path(path):
        return {'status': 'invalid_input', 'error': 'Only local file paths may be probed.'}
    local_path = _resolved_local_path(path)
    if local_path is None:
        return {'status': 'invalid_input', 'error': 'The local file path cannot be resolved.'}
    command = _command(ffprobe, ['-v', 'error', '-protocol_whitelist', 'file',
                                 '-show_format', '-show_streams', '-of', 'json', local_path])
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return {'status': 'unavailable', 'error': str(exc)}
    except subprocess.TimeoutExpired:
        return {'status': 'failed', 'error': f'ffprobe timed out after {timeout} seconds'}
    except OSError as exc:
        return {'status': 'failed', 'error': str(exc)}
    if completed.returncode:
        error = completed.stderr.strip() or f'ffprobe exited with status {completed.returncode}'
        return {'status': 'failed', 'error': error[:4000]}
    try:
        value = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        return {'status': 'invalid_json', 'error': f'ffprobe returned invalid JSON: {exc}'}
    if not isinstance(value, dict):
        return {'status': 'invalid_json', 'error': 'ffprobe JSON root must be an object.'}
    return {'status': 'ok', 'data': value}


def decode_media(path: str | os.PathLike[str], ffmpeg: str | os.PathLike[str] | Sequence[str] = 'ffmpeg',
                 timeout: float | None = None) -> dict[str, Any]:
    """Decode the complete local input to a null sink using FFmpeg -xerror."""
    if not _is_local_path(path):
        return {'status': 'invalid_input', 'error': 'Only local file paths may be decoded.'}
    local_path = _resolved_local_path(path)
    if local_path is None:
        return {'status': 'invalid_input', 'error': 'The local file path cannot be resolved.'}
    command = _command(ffmpeg, ['-v', 'error', '-xerror', '-protocol_whitelist', 'file',
                                '-i', local_path, '-map', '0:v?', '-map', '0:a?',
                                '-f', 'null', '-'])
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        return {'status': 'unavailable', 'error': str(exc)}
    except subprocess.TimeoutExpired:
        return {'status': 'failed', 'error': 'ffmpeg full decode timed out'}
    except OSError as exc:
        return {'status': 'failed', 'error': str(exc)}
    if completed.returncode:
        error = completed.stderr.strip() or f'ffmpeg exited with status {completed.returncode}'
        return {'status': 'failed', 'error': error[:4000]}
    return {'status': 'passed'}


def _media_type(path: Path) -> tuple[str, str]:
    """Use a small deterministic magic set, then a stable suffix table."""
    try:
        with path.open('rb') as handle:
            head = handle.read(64)
    except OSError:
        head = b''
    if len(head) >= 12 and head[4:8] == b'ftyp':
        brand = head[8:12]
        return ('video/quicktime' if brand == b'qt  ' else 'video/mp4', 'magic')
    if head.startswith(b'\x1aE\xdf\xa3'):
        return ('video/webm' if path.suffix.lower() == '.webm' else 'video/x-matroska', 'magic')
    if head.startswith(b'RIFF') and head[8:12] == b'AVI ':
        return 'video/x-msvideo', 'magic'
    if head.startswith(b'RIFF') and head[8:12] == b'WAVE':
        return 'audio/wav', 'magic'
    if head.startswith(b'ID3') or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0):
        return 'audio/mpeg', 'magic'
    if head.startswith(b'fLaC'):
        return 'audio/flac', 'magic'
    if head.startswith(b'OggS'):
        return ('video/ogg' if path.suffix.lower() == '.ogv' else 'audio/ogg', 'magic')
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png', 'magic'
    if head.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg', 'magic'
    if head.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif', 'magic'
    suffix = path.suffix.lower()
    if suffix in _EXTENSION_TYPES:
        return _EXTENSION_TYPES[suffix], 'extension'
    return 'application/octet-stream', 'fallback'


def _input_identity(line_number: int, source: Any) -> str:
    payload = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(f'{line_number}\n{payload}'.encode('utf-8')).hexdigest()


def _read_existing(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    payload = path.read_bytes()
    if payload and not payload.endswith(b'\n'):
        last_start = payload.rfind(b'\n') + 1
        try:
            json.loads(payload[last_start:].decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # The final checkpoint may have been interrupted between writes.
            payload = payload[:last_start]
            path.write_bytes(payload)
        else:
            payload += b'\n'
            path.write_bytes(payload)
    for line_number, line in enumerate(payload.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f'Invalid existing output JSONL at line {line_number}: {exc}') from exc
        if isinstance(row, dict) and row.get('record_id'):
            latest[row['record_id']] = row
    return latest


def _same_path_or_file(left: Path, right: Path) -> bool:
    try:
        if left.resolve(strict=False) == right.resolve(strict=False):
            return True
    except (OSError, RuntimeError):
        pass
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


def _load_input_rows(path: Path) -> list[tuple[int, Any, str | None, str]]:
    rows: list[tuple[int, Any, str | None, str]] = []
    with path.open('r', encoding='utf-8') as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            try:
                source = json.loads(raw_line)
                input_error = None
            except json.JSONDecodeError as exc:
                source = {'raw_line': raw_line.rstrip('\r\n')}
                input_error = f'Invalid input JSON at line {line_number}: {exc}'
            rows.append((line_number, source, input_error, _input_identity(line_number, source)))
    return rows


def _validate_output_destination(input_path: Path, output_path: Path,
                                 input_rows: Sequence[tuple[int, Any, str | None, str]]) -> None:
    if _same_path_or_file(input_path, output_path):
        raise ValueError('Input and output manifests must be different files.')
    for _, source, input_error, _ in input_rows:
        if input_error is not None or not isinstance(source, dict):
            continue
        local = source.get('local_path')
        if not isinstance(local, str) or not local.strip() or not _is_local_path(local):
            continue
        media_path = Path(local).expanduser()
        if not media_path.is_absolute():
            media_path = input_path.parent / media_path
        if _same_path_or_file(output_path, media_path):
            raise ValueError('output manifest must not alias a source media file.')


def _atomic_write_jsonl(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _new_index_record(source: Any, record_id: str, line_number: int,
                      input_error: str | None = None) -> dict[str, Any]:
    facts = {'sha256': None, 'size_bytes': None, 'media_type': None,
             'media_type_detection': None, 'ffprobe': None}
    return {'record_id': record_id, 'input_line': line_number, 'source': source,
            'media_index': {'schema_version': 1, 'status': 'input_error' if input_error else 'pending',
                            'verified_media_facts': facts,
                            'probe': {'status': 'not_run'},
                            'decode_check': {'requested': False, 'status': 'not_requested'},
                            **({'error': input_error} if input_error else {})}}


def _hash_details(path: Path) -> tuple[str, int, os.stat_result]:
    before = path.stat()
    if not path.is_file():
        raise OSError(f'Not a regular file: {path}')
    digest = sha256_file(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError('Input changed while hashing')
    return digest, before.st_size, after


def _same_file_status(previous: dict[str, Any] | None, source: Any, digest: str, size: int,
                      strict_decode: bool) -> bool:
    if not previous or previous.get('source') != source:
        return False
    media_index = previous.get('media_index', {})
    facts = media_index.get('verified_media_facts', {})
    if facts.get('sha256') != digest or facts.get('size_bytes') != size or media_index.get('status') != 'ok':
        return False
    decode = media_index.get('decode_check', {})
    if bool(decode.get('requested')) != strict_decode:
        return False
    return not strict_decode or decode.get('status') == 'passed'


def _index_one(source: Any, record_id: str, line_number: int, *, input_error: str | None,
               input_root: Path, ffprobe: str | os.PathLike[str] | Sequence[str],
               ffmpeg: str | os.PathLike[str] | Sequence[str], strict_decode: bool,
               file_details: tuple[str, int, os.stat_result] | None = None) -> dict[str, Any]:
    row = _new_index_record(source, record_id, line_number, input_error)
    if input_error:
        return row
    if not isinstance(source, dict):
        row['media_index'].update(status='input_error', error='Each input JSONL value must be an object.')
        return row
    source_locator = source.get('source_locator')
    local_path = source.get('local_path')
    if not isinstance(source_locator, str) or not source_locator.strip():
        row['media_index'].update(status='input_error', error='source_locator must be a non-empty string.')
        return row
    if not isinstance(local_path, str) or not local_path.strip():
        row['media_index'].update(status='input_error', error='local_path must be a non-empty local file path.')
        return row
    if not _is_local_path(local_path):
        row['media_index'].update(status='input_error', error='local_path must be a local filesystem path.')
        return row
    path = Path(local_path).expanduser()
    if not path.is_absolute():
        path = input_root / path
    facts = row['media_index']['verified_media_facts']
    try:
        digest, size, before_probe = file_details or _hash_details(path)
    except FileNotFoundError as exc:
        row['media_index'].update(status='file_missing', error=str(exc))
        return row
    except PermissionError as exc:
        row['media_index'].update(status='file_unreadable', error=str(exc))
        return row
    except RuntimeError as exc:
        row['media_index'].update(status='file_changed', error=str(exc))
        return row
    except OSError as exc:
        status = 'file_missing' if not path.exists() else 'file_unreadable'
        row['media_index'].update(status=status, error=str(exc))
        return row
    media_type, detection = _media_type(path)
    facts.update(sha256=digest, size_bytes=size, media_type=media_type,
                 media_type_detection=detection)
    probe = probe_media(path, ffprobe=ffprobe)
    row['media_index']['probe'] = {key: value for key, value in probe.items() if key != 'data'}
    if probe.get('status') == 'ok':
        facts['ffprobe'] = probe['data']
    decode_check = {'requested': strict_decode, 'status': 'not_requested'}
    if strict_decode:
        decode_check = {'requested': True, **decode_media(path, ffmpeg=ffmpeg)}
    row['media_index']['decode_check'] = decode_check
    try:
        after_probe = path.stat()
        if (before_probe.st_size, before_probe.st_mtime_ns, before_probe.st_ino) != (
                after_probe.st_size, after_probe.st_mtime_ns, after_probe.st_ino):
            row['media_index'].update(status='file_changed', error='Input changed during media inspection.')
            return row
    except OSError as exc:
        row['media_index'].update(status='file_changed', error=f'Input disappeared during media inspection: {exc}')
        return row
    if strict_decode and decode_check['status'] == 'failed':
        status = 'decode_failed'
    elif strict_decode and decode_check['status'] == 'unavailable':
        status = 'decode_unavailable'
    elif probe.get('status') == 'failed':
        status = 'probe_failed'
    elif probe.get('status') == 'invalid_json':
        status = 'probe_invalid_json'
    elif probe.get('status') == 'invalid_input':
        status = 'input_error'
    elif probe.get('status') == 'unavailable':
        status = 'probe_unavailable'
    else:
        status = 'ok'
    row['media_index']['status'] = status
    return row


def index_manifest(input_path: str | os.PathLike[str], output_path: str | os.PathLike[str], *,
                   ffprobe: str | os.PathLike[str] | Sequence[str] = 'ffprobe',
                   ffmpeg: str | os.PathLike[str] | Sequence[str] = 'ffmpeg',
                   strict_decode: bool = False) -> dict[str, Any]:
    """Index local files named by input JSONL and atomically checkpoint output.

    Completed successful rows with matching input, checksum and decode options
    are reused on repeat runs. In-progress rows are appended as checkpoints so
    an interrupted run can resume; a successful run compacts the output to one
    row per current input line in original order.
    """
    source_path, target_path = Path(input_path), Path(output_path)
    input_rows = _load_input_rows(source_path)
    _validate_output_destination(source_path, target_path, input_rows)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_existing(target_path)
    current_rows: list[dict[str, Any]] = []
    for line_number, source, input_error, record_id in input_rows:
        previous = existing.get(record_id)
        row: dict[str, Any] | None = None
        file_details = None
        if previous and input_error is None and isinstance(source, dict):
            local = source.get('local_path')
            if isinstance(local, str) and local.strip() and _is_local_path(local):
                path = Path(local).expanduser()
                if not path.is_absolute():
                    path = source_path.parent / path
                try:
                    file_details = _hash_details(path)
                    digest, size, _ = file_details
                    if _same_file_status(previous, source, digest, size, strict_decode):
                        row = previous
                except (OSError, RuntimeError):
                    file_details = None
        if row is None:
            row = _index_one(source, record_id, line_number, input_error=input_error,
                             input_root=source_path.parent, ffprobe=ffprobe, ffmpeg=ffmpeg,
                             strict_decode=strict_decode, file_details=file_details)
            with target_path.open('a', encoding='utf-8') as journal:
                journal.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')) + '\n')
                journal.flush()
                os.fsync(journal.fileno())
            existing[record_id] = row
        current_rows.append(row)
    _atomic_write_jsonl(target_path, current_rows)
    status_counts = Counter(row['media_index']['status'] for row in current_rows)
    return {'input': str(source_path), 'output': str(target_path), 'records': len(current_rows),
            'status_counts': dict(sorted(status_counts.items()))}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='Input JSONL rows with local_path and source metadata')
    parser.add_argument('output', type=Path, help='Output JSONL media index')
    parser.add_argument('--ffprobe', default='ffprobe', help='ffprobe executable (default: ffprobe)')
    parser.add_argument('--ffmpeg', default='ffmpeg', help='ffmpeg executable (default: ffmpeg)')
    parser.add_argument('--strict-decode', action='store_true',
                        help='Decode the entire local media file with FFmpeg')
    args = parser.parse_args(argv)
    summary = index_manifest(args.input, args.output, ffprobe=args.ffprobe,
                              ffmpeg=args.ffmpeg, strict_decode=args.strict_decode)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
