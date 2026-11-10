"""Build a provenance-preserving technical-index input from a video archive JSONL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from src.media_pipeline import sha256_file


_VIDEO_SUFFIXES = {".3g2", ".3gp", ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
                   ".mpeg", ".mpg", ".mts", ".ogv", ".ts", ".webm", ".wmv"}
_SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _read_snapshot(path: Path) -> tuple[bytes, list[tuple[int, dict]]]:
    payload = path.read_bytes()
    rows = []
    for line_number, line in enumerate(payload.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid archive JSONL at line {line_number}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"archive JSONL line {line_number} must contain an object")
        rows.append((line_number, row))
    return payload, rows


def _stable_record_id(row: dict, digest: str) -> str:
    identity = [row.get("platform"), row.get("post_url"), row.get("channel"), row.get("message_id"), digest]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":"))
    return "archive_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def build_video_index_manifest(input_path: str | os.PathLike[str], output_path: str | os.PathLike[str],
                               receipt_path: str | os.PathLike[str], *, overwrite: bool = False) -> dict:
    """Filter archive rows to verified local video files and write an auditable input manifest.

    The adapter is intentionally conservative: it does not copy captions or infer that a
    downloaded clip depicts the incident. Its receipt records every excluded row reason.
    """
    input_file = Path(input_path).expanduser().resolve(strict=True)
    raw_output_file = Path(output_path).expanduser()
    raw_receipt_file = Path(receipt_path).expanduser()
    if raw_output_file.is_symlink() or raw_receipt_file.is_symlink():
        raise ValueError("adapter outputs must not be symlinks")
    output_file = raw_output_file.resolve()
    receipt_file = raw_receipt_file.resolve()
    if len({input_file, output_file, receipt_file}) != 3:
        raise ValueError("input, output, and receipt must be different files")
    payload, rows = _read_snapshot(input_file)
    payload_sha = hashlib.sha256(payload).hexdigest()
    included = []
    exclusions = []
    exclusion_counts: Counter[str] = Counter()
    unique_assets: dict[str, int] = {}

    for line_number, row in rows:
        def exclude(reason: str) -> None:
            exclusions.append({"input_line": line_number, "reason": reason,
                               "source_locator": row.get("post_url") if isinstance(row.get("post_url"), str) else None})
            exclusion_counts[reason] += 1

        status = row.get("status")
        if status != "downloaded_verified":
            exclude(f"archive_status_{status}" if isinstance(status, str) and status else "archive_status_missing")
            continue
        locator = row.get("post_url")
        try:
            valid_locator = (isinstance(locator, str) and bool(locator.strip())
                             and urlsplit(locator).scheme in {"http", "https"})
        except ValueError:
            valid_locator = False
        if not valid_locator:
            exclude("invalid_source_locator")
            continue
        local_value = row.get("local_file")
        if not isinstance(local_value, str) or not local_value.strip():
            exclude("missing_local_path")
            continue
        local_path = Path(local_value).expanduser()
        try:
            resolved_path = local_path.resolve(strict=True)
        except (OSError, RuntimeError):
            exclude("file_missing")
            continue
        if not resolved_path.is_file():
            exclude("not_regular_file")
            continue
        mime = row.get("actual_mime_type")
        is_video = (mime.lower().startswith("video/") if isinstance(mime, str) and mime.strip()
                    else resolved_path.suffix.lower() in _VIDEO_SUFFIXES)
        if not is_video:
            exclude("not_video_media_type")
            continue
        expected_sha = row.get("sha256")
        if not isinstance(expected_sha, str) or not _SHA256.fullmatch(expected_sha):
            exclude("invalid_expected_sha256")
            continue
        expected_size = row.get("file_size_bytes")
        if type(expected_size) is not int or expected_size < 0:
            exclude("invalid_expected_size")
            continue
        try:
            actual_size = resolved_path.stat().st_size
        except OSError:
            exclude("file_unreadable")
            continue
        if actual_size != expected_size:
            exclude("file_size_mismatch")
            continue
        platform = row.get("platform") if isinstance(row.get("platform"), str) else "unknown"
        digest = expected_sha
        archive_record_id = _stable_record_id(row, digest)
        included.append({
            "archive_record_id": archive_record_id,
            "local_path": str(resolved_path),
            "source_locator": locator,
            "platform": platform,
            "publication_time_claim": row.get("published_at"),
            "fetched_at": row.get("retrieved_at_utc") or row.get("downloaded_at_utc"),
            "sha256": digest,
            "size_bytes_claim": expected_size,
            "external_post_id": str(row.get("message_id")) if row.get("message_id") is not None else None,
            "channel": row.get("channel") if isinstance(row.get("channel"), str) else None,
            "archive_status": status,
        })
        unique_assets.setdefault(digest, expected_size)

    for _, row in rows:
        local_value = row.get("local_file")
        if not isinstance(local_value, str) or not local_value.strip():
            continue
        local_path = Path(local_value).expanduser()
        try:
            source_path = local_path.resolve(strict=False)
        except (OSError, RuntimeError):
            continue
        if source_path in {output_file, receipt_file}:
            raise ValueError("adapter outputs must not alias a source media file")
    if output_file.exists() or receipt_file.exists():
        if not overwrite:
            raise FileExistsError("refusing to overwrite adapter outputs; pass overwrite=True explicitly")
        if any(path.is_symlink() or not path.is_file() for path in (output_file, receipt_file) if path.exists()):
            raise FileExistsError("existing adapter outputs must be regular non-symlink files")

    serialized = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                        for row in included)
    summary = {
        "archive_rows": len(rows),
        "included_records": len(included),
        "unique_media_count": len(unique_assets),
        "unique_media_bytes_claimed": sum(unique_assets.values()),
        "excluded_records": len(exclusions),
        "excluded_reasons": dict(sorted(exclusion_counts.items())),
    }
    receipt = {
        "schema_version": "1.0",
        "producer": "social.analysis.video_archive_adapter@1.0",
        "input_path": str(input_file),
        "input_sha256": payload_sha,
        "output_path": str(output_file),
        "output_sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
        "summary": summary,
        "included_record_ids": [row["archive_record_id"] for row in included],
        "excluded_records": exclusions,
        "limitations": [
            "Only archive rows already marked downloaded_verified are candidates for local indexing.",
            "The archive SHA-256 is carried forward as a claim and is independently checked by the media indexer.",
            "This technical adapter excludes captions and does not verify event relevance, recording time, location, or claim truth.",
        ],
    }
    _atomic_write(output_file, serialized)
    _atomic_write(receipt_file, json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return {"summary": summary, "receipt": receipt}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_archive_jsonl", type=Path)
    parser.add_argument("output_index_input_jsonl", type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    result = build_video_index_manifest(args.input_archive_jsonl, args.output_index_input_jsonl,
                                        args.receipt, overwrite=args.overwrite)
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
