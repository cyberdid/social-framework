"""Conservatively import existing web captures into the evidence ledger.

This module only reads local capture manifests and body files. It never fetches
sources, creates claim records, or derives text from an archived body. A passage
is created only from a collector-provided ``article_text`` extraction whose
referenced body has passed the collector's byte-count and SHA-256 checks.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

try:  # Support both ``src.analysis`` and ``analysis`` import styles.
    from src.evidence_ledger import EvidenceLedger
except ModuleNotFoundError:  # pragma: no cover - exercised by src-on-PYTHONPATH users
    from evidence_ledger import EvidenceLedger


PRODUCER = "source_ingest_bridge"
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def ingest_source_watch_manifest(
    manifest_path: str | Path,
    ledger: EvidenceLedger,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Import body receipts and collector-extracted article passages.

    The returned summary deliberately excludes source text, URLs, local paths,
    hashes, and exception messages. Details needed to locate the capture live in
    the ledger's source, artifact, and passage records.
    """
    return _ingest_json_manifest(manifest_path, ledger, root=root)


def ingest_archive_index(
    index_path: str | Path,
    ledger: EvidenceLedger,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Import legacy ``archive_sources`` JSONL receipts without text extraction.

    The archive index only records compressed raw HTML and has no trustworthy
    passage extraction, so successful rows produce Source and Artifact records
    only. Redirected rows are skipped because the current ledger schema has no
    safe place to preserve a capture URL when no Passage locator exists.
    """
    root_path = _root(root)
    try:
        index_file = Path(index_path).resolve(strict=True)
        index_bytes = index_file.read_bytes()
    except (OSError, RuntimeError, ValueError):
        return _summary([_item(0, "manifest_unavailable")])

    if not _inside(index_file, root_path):
        return _summary([_item(0, "unsupported_path")])

    items: list[dict[str, Any]] = []
    try:
        lines = index_bytes.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return _summary([_item(0, "unsupported_manifest")])

    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            items.append(_item(index, "unsupported_manifest_row"))
            continue
        if not isinstance(row, dict):
            items.append(_item(index, "unsupported_manifest_row"))
            continue
        result = _ingest_archive_row(
            row, index=index, index_file=index_file, ledger=ledger, root=root_path,
        )
        items.append(result)
    return _summary(items)


def _ingest_json_manifest(
    manifest_path: str | Path,
    ledger: EvidenceLedger,
    *,
    root: str | Path | None,
) -> dict[str, Any]:
    root_path = _root(root)
    try:
        manifest_file = Path(manifest_path).resolve(strict=True)
        manifest_bytes = manifest_file.read_bytes()
    except (OSError, RuntimeError, ValueError):
        return _summary([_item(0, "manifest_unavailable")])

    if not _inside(manifest_file, root_path):
        return _summary([_item(0, "unsupported_path")])
    try:
        manifest = json.loads(manifest_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _summary([_item(0, "unsupported_manifest")])
    if not isinstance(manifest, dict) or not isinstance(manifest.get("rows"), list):
        return _summary([_item(0, "unsupported_manifest")])

    manifest_rel = manifest_file.relative_to(root_path).as_posix()
    manifest_hash = _sha256(manifest_bytes)
    items = []
    for index, row in enumerate(manifest["rows"]):
        if not isinstance(row, dict):
            items.append(_item(index, "unsupported_manifest_row"))
            continue
        items.append(_ingest_watch_row(
            row, index=index, manifest_file=manifest_file, manifest_rel=manifest_rel,
            manifest_hash=manifest_hash, ledger=ledger, root=root_path,
        ))
    return _summary(items)


def _ingest_watch_row(
    row: Mapping[str, Any], *, index: int, manifest_file: Path,
    manifest_rel: str, manifest_hash: str, ledger: EvidenceLedger, root: Path,
) -> dict[str, Any]:
    source_url = row.get("url")
    if not _valid_url(source_url):
        return _item(index, "missing_url" if not source_url else "unsupported_url")
    capture_url = row.get("final_url")
    if not _valid_url(capture_url):
        return _item(index, "missing_capture_url" if not capture_url else "unsupported_capture_url")

    body_file = row.get("body_file")
    if not isinstance(body_file, str) or not body_file.strip():
        return _item(index, "missing_body")
    body_path = _safe_relative_file(manifest_file.parent, body_file)
    if body_path is None:
        return _item(index, "unsupported_path")
    if not body_path.is_file():
        return _item(index, "missing_body")
    try:
        body = body_path.read_bytes()
    except OSError:
        return _item(index, "missing_body")

    integrity = _verify_body(row, body)
    if integrity is not None:
        return _item(index, integrity)
    captured_at = row.get("requested_at")
    normalized_at = _normalize_timestamp(captured_at)
    if normalized_at is None:
        return _item(index, "unsupported_capture_timestamp")
    body_rel = _relative_to_root(body_path, root)
    if body_rel is None:
        return _item(index, "unsupported_path")

    content_type = row.get("content_type")
    media_type = _media_type(content_type)
    limitations: list[str] = []
    if not media_type:
        media_type = "application/octet-stream"
        limitations.append("Collector content type was absent or unusable.")
    if row.get("truncated") is True:
        limitations.append("Collector marked the archived body as truncated.")
    run_id = _capture_run_id(source_url, capture_url, normalized_at, row["sha256"], body_rel)
    passage_text, locator, passage_state = _watch_passage(
        row, index=index, manifest_rel=manifest_rel, manifest_hash=manifest_hash,
        body_rel=body_rel, body_hash=row["sha256"].lower(), source_url=source_url,
        capture_url=capture_url, captured_at=captured_at,
    )

    artifact_only_state = passage_state
    if row.get("truncated") is True:
        passage_text, locator = None, None
        artifact_only_state = "truncated_capture"
    elif row.get("error"):
        passage_text, locator = None, None
        error = row.get("error")
        artifact_only_state = (
            "unsupported_content_type" if error == "unsupported_content_type"
            else "access_challenge" if error == "access_challenge"
            else "unsupported_capture_state"
        )
    elif not _is_success(row.get("status")):
        passage_text, locator = None, None
        artifact_only_state = "unsupported_http_status"
    elif media_type not in {"text/html", "application/xhtml+xml"}:
        passage_text, locator = None, None
        artifact_only_state = "unsupported_content_type"

    if passage_text is None and source_url != capture_url:
        return _item(index, "unsupported_capture_url")

    result = _write_records(
        ledger, source_url=source_url, capture_url=capture_url,
        captured_at=normalized_at, body_hash=row["sha256"].lower(),
        artifact_path=body_rel, media_type=media_type, run_id=run_id,
        artifact_limitations=limitations, passage_text=passage_text,
        passage_locator=locator,
    )
    if result is None:
        return _item(index, "record_conflict")
    if passage_text is None:
        return _item(index, artifact_only_state)
    return _item(index, "already_present" if result == 0 else "ingested", result)


def _watch_passage(
    row: Mapping[str, Any], *, index: int, manifest_rel: str, manifest_hash: str,
    body_rel: str, body_hash: str, source_url: str, capture_url: str,
    captured_at: Any,
) -> tuple[str | None, dict[str, Any] | None, str]:
    extracted = row.get("extracted")
    if not isinstance(extracted, dict):
        return None, None, "unsupported_extraction"
    if extracted.get("mode") != "article_text":
        return None, None, "unsupported_extraction"
    text = extracted.get("filtered_text")
    if not isinstance(text, str) or not text.strip():
        return None, None, "unsupported_extraction"
    version = extracted.get("extractor_version")
    if type(version) is not int or version < 1:
        return None, None, "unsupported_extraction"
    locator = {
        "kind": "source_watch_filtered_article_text",
        "manifest_path": manifest_rel,
        "manifest_sha256": manifest_hash,
        "row_index": index,
        "body_path": body_rel,
        "body_sha256": body_hash,
        "source_url": source_url,
        "capture_url": capture_url,
        "capture_timestamp": captured_at,
        "extractor_version": version,
        "extraction_mode": "article_text",
        "text_sha256": _sha256(text.encode("utf-8")),
    }
    return text, locator, "unsupported_extraction"


def _ingest_archive_row(
    row: Mapping[str, Any], *, index: int, index_file: Path,
    ledger: EvidenceLedger, root: Path,
) -> dict[str, Any]:
    source_url = row.get("url")
    if not _valid_url(source_url):
        return _item(index, "missing_url" if not source_url else "unsupported_url")
    capture_url = row.get("final_url")
    if not _valid_url(capture_url):
        return _item(index, "missing_capture_url" if not capture_url else "unsupported_capture_url")

    file_name = row.get("file")
    if not isinstance(file_name, str) or not file_name.strip():
        return _item(index, "missing_body")
    if Path(file_name).suffix.lower() != ".gz":
        return _item(index, "unsupported_archive_format")
    body_path = _safe_relative_file(index_file.parent, file_name)
    if body_path is None:
        return _item(index, "unsupported_path")
    if not body_path.is_file():
        return _item(index, "missing_body")
    try:
        stored_bytes = body_path.read_bytes()
        body = gzip.decompress(stored_bytes)
    except (OSError, EOFError, gzip.BadGzipFile, zlib.error):
        return _item(index, "unsupported_archive_format")

    integrity = _verify_body(row, body)
    if integrity is not None:
        return _item(index, integrity)
    normalized_at = _normalize_timestamp(row.get("fetched_at"))
    if normalized_at is None:
        return _item(index, "unsupported_capture_timestamp")
    body_rel = _relative_to_root(body_path, root)
    if body_rel is None:
        return _item(index, "unsupported_path")

    # Without a Passage locator, preserving two distinct URLs would require
    # overloading Source.title or Artifact.path, which would corrupt their meaning.
    if source_url != capture_url:
        return _item(index, "unsupported_capture_url")
    limitations = [
        "Stored artifact is gzip-compressed; sha256 and byte count were verified against decompressed capture bytes.",
        "Legacy archive index contains no collector-provided passage extraction.",
    ]
    run_id = _capture_run_id(source_url, capture_url, normalized_at, row["sha256"], body_rel)
    result = _write_records(
        ledger, source_url=source_url, capture_url=capture_url,
        captured_at=normalized_at, body_hash=row["sha256"].lower(),
        artifact_path=body_rel, media_type="text/html", run_id=run_id,
        artifact_limitations=limitations, passage_text=None, passage_locator=None,
    )
    if result is None:
        return _item(index, "record_conflict")
    return _item(index, "unsupported_no_passage", result)


def _write_records(
    ledger: EvidenceLedger, *, source_url: str, capture_url: str,
    captured_at: str, body_hash: str, artifact_path: str, media_type: str,
    run_id: str, artifact_limitations: list[str], passage_text: str | None,
    passage_locator: dict[str, Any] | None,
) -> int | None:
    source_id = _record_id("source", source_url)
    artifact_id = _record_id(
        "artifact", source_id, source_url, capture_url, captured_at, body_hash,
        artifact_path,
    )
    passage_id = (_record_id("passage", artifact_id, _sha256(passage_text.encode("utf-8")), passage_locator)
                  if passage_text is not None and passage_locator is not None else None)
    run_created_at = captured_at
    existing_source = ledger.get_record(source_id)
    source_created_at = existing_source["created_at"] if existing_source else run_created_at
    expected_source = {
        "record_type": "Source", "schema_version": 1, "record_id": source_id,
        "created_at": source_created_at, "uri": source_url, "title": source_url,
    }
    expected_artifact = {
        "record_type": "Artifact", "schema_version": 1, "record_id": artifact_id,
        "created_at": captured_at, "source_id": source_id, "path": artifact_path,
        "sha256": body_hash, "media_type": media_type, "captured_at": captured_at,
        "producer": PRODUCER, "run_id": run_id, "limitations": artifact_limitations,
    }
    expected_passage = None
    if passage_id is not None:
        expected_passage = {
            "record_type": "Passage", "schema_version": 1, "record_id": passage_id,
            "created_at": captured_at, "artifact_id": artifact_id, "text": passage_text,
            "locator": passage_locator, "producer": PRODUCER, "run_id": run_id,
            "limitations": [
                "Passage text is the collector-provided filtered extraction; original-HTML byte offsets are unavailable."
            ],
        }

    existing_artifact = ledger.get_record(artifact_id)
    existing_passage = ledger.get_record(passage_id) if passage_id is not None else None
    if (existing_source is not None and existing_source != expected_source
            or existing_artifact is not None and existing_artifact != expected_artifact
            or expected_passage is not None and existing_passage is not None and existing_passage != expected_passage):
        return None

    added = 0
    if existing_source is None:
        ledger.add_source(
            source_id, uri=source_url, title=source_url, created_at=source_created_at,
        )
        added += 1
    if existing_artifact is None:
        ledger.add_artifact(
            artifact_id, source_id=source_id, path=artifact_path, sha256=body_hash,
            media_type=media_type, captured_at=captured_at, producer=PRODUCER,
            run_id=run_id, limitations=artifact_limitations, created_at=captured_at,
        )
        added += 1
    if expected_passage is not None and existing_passage is None:
        ledger.add_passage(
            passage_id, artifact_id=artifact_id, text=passage_text,
            locator=passage_locator, producer=PRODUCER, run_id=run_id,
            limitations=expected_passage["limitations"], created_at=captured_at,
        )
        added += 1
    return added


def _verify_body(row: Mapping[str, Any], body: bytes) -> str | None:
    expected_hash = row.get("sha256")
    if not isinstance(expected_hash, str) or not _SHA256.fullmatch(expected_hash):
        return "missing_hash"
    expected_bytes = row.get("bytes")
    if type(expected_bytes) is not int or expected_bytes < 0:
        return "missing_byte_count"
    if expected_bytes != len(body) or expected_hash.lower() != _sha256(body):
        return "integrity_mismatch"
    return None


def _safe_relative_file(base: Path, name: str) -> Path | None:
    candidate_name = Path(name)
    if candidate_name.is_absolute():
        return None
    try:
        candidate = (base / candidate_name).resolve(strict=False)
        base_resolved = base.resolve(strict=True)
        candidate.relative_to(base_resolved)
    except (OSError, RuntimeError, ValueError):
        return None
    return candidate


def _relative_to_root(path: Path, root: Path) -> str | None:
    try:
        return path.resolve(strict=True).relative_to(root).as_posix()
    except (OSError, RuntimeError, ValueError):
        return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _root(root: str | Path | None) -> Path:
    return Path(root).resolve() if root is not None else _DEFAULT_ROOT


def _valid_url(value: Any) -> bool:
    if not isinstance(value, str) or not value or value != value.strip() or any(ch.isspace() for ch in value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme in {"http", "https"} and bool(parts.hostname)
                and parts.username is None and parts.password is None and parts.port in (None, 80, 443))
    except ValueError:
        return False


def _normalize_timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _media_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type or None


def _is_success(value: Any) -> bool:
    return type(value) is int and 200 <= value < 300


def _capture_run_id(source_url: str, capture_url: str, captured_at: str,
                    body_hash: str, artifact_path: str) -> str:
    value = json.dumps(
        [source_url, capture_url, captured_at, body_hash.lower(), artifact_path],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return "capture_" + _sha256(value)[:24]


def _record_id(kind: str, *parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"{kind}_" + _sha256(encoded)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _item(index: int, status: str, records_added: int = 0) -> dict[str, Any]:
    return {"index": index, "status": status, "records_added": records_added}


def _summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    added = 0
    for item in items:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
        added += item["records_added"]
    return {"items": items, "counts": counts, "records_added": added}
