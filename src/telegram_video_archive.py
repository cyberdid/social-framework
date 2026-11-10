"""Resumable acquisition of public Telegram video posts already in a manifest.

The collector stores one manifest row per source post and one binary per Telegram
document ID. It never edits source posts or attempts to bypass access controls.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
import re
import tempfile
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable


COLLECTOR_VERSION = "1.1.0"
ACCESS_POLICY = (
    "Public broadcast channels only; authenticated existing session; "
    "no private-channel access or access-control bypass."
)
ASSET_STATUSES = {
    "downloaded", "downloaded_recovered", "downloaded_verified", "download_error",
    "download_interrupted", "existing_verified", "invalid_metadata", "metadata_changed",
}
SOURCE_ERROR_STATUSES = {"message_unavailable", "channel_or_metadata_error"}
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def file_matches(path: str | Path, expected_size: int | None,
                 expected_sha256: str | None = None) -> bool:
    candidate = Path(path)
    if not candidate.is_file() or expected_size is None or expected_size <= 0:
        return False
    if candidate.stat().st_size != expected_size:
        return False
    return not expected_sha256 or sha256_file(candidate).lower() == expected_sha256.lower()


async def download_atomic(
    writer: Callable[[Path], Awaitable[str | Path | None]],
    destination: str | Path,
    expected_size: int,
    expected_sha256: str | None = None,
) -> dict:
    """Download to a sibling temporary file and publish only a verified result.

    ``writer`` must write to the path it receives and return that path (or None).
    An existing destination is retained until the new file passes integrity
    checks, so interrupted downloads cannot replace a known good asset.
    """
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    if file_matches(target, expected_size, expected_sha256):
        return {"status": "existing_verified", "path": str(target),
                "size_bytes": target.stat().st_size, "sha256": sha256_file(target)}
    if expected_size <= 0:
        raise ValueError("expected media size must be positive")

    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".part",
                                     dir=target.parent)
    os.close(fd)
    temporary = Path(temp_name)
    temporary.unlink()
    try:
        result = await writer(temporary)
        written = Path(result) if result else temporary
        if written.resolve() != temporary.resolve():
            raise ValueError("downloader wrote outside the designated temporary path")
        if not written.is_file():
            raise ValueError("downloader returned without creating the temporary file")
        actual_size = written.stat().st_size
        if actual_size != expected_size:
            raise ValueError(f"download size mismatch: expected {expected_size}, got {actual_size}")
        digest = sha256_file(written)
        if expected_sha256 and digest.lower() != expected_sha256.lower():
            raise ValueError("download sha256 mismatch")
        os.replace(written, target)
        return {"status": "downloaded", "path": str(target),
                "size_bytes": actual_size, "sha256": digest}
    finally:
        temporary.unlink(missing_ok=True)


def group_manifest_by_file_id(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        file_id = row.get("telegram_file_id")
        if file_id is not None and str(file_id).strip():
            groups[str(file_id)].append(row)
    return dict(groups)


def read_manifest(path: str | Path) -> list[dict]:
    records = []
    with Path(path).open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"manifest row {line_number} must be an object")
            records.append(record)
    return records


def write_manifest_atomic(path: str | Path, rows: list[dict]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                     dir=target.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def write_json_atomic(path: str | Path, payload: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                     dir=target.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def load_channel_ids(path: str | Path | None) -> dict[str, int]:
    if not path or not Path(path).is_file():
        return {}
    result = {}
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            record = json.loads(line)
            username, chat_id = record.get("username"), record.get("chat_id")
            if username and chat_id:
                try:
                    result[str(username).lower().lstrip("@")] = int(chat_id)
                except (TypeError, ValueError):
                    continue
    return result


def _channel_username(value) -> str:
    return str(value or "").strip().lstrip("@").lower()


def validate_public_broadcast_entity(entity, expected_username: str,
                                     expected_channel_id: int | None = None):
    """Reject stale, private, group, or differently named Telegram entities."""
    expected = _channel_username(expected_username)
    actual = _channel_username(getattr(entity, "username", None))
    try:
        actual_id = int(getattr(entity, "id", 0))
    except (TypeError, ValueError):
        actual_id = 0
    expected_id_matches = True
    if expected_channel_id is not None:
        expected_id = int(expected_channel_id)
        expected_id_matches = expected_id in (actual_id, -(10**12 + actual_id))
    if (not expected or actual != expected or actual_id <= 0
            or getattr(entity, "broadcast", False) is not True
            or getattr(entity, "megagroup", False) is True
            or not expected_id_matches):
        raise ValueError("resolved entity is not the expected public broadcast channel")
    return entity


def normalize_manifest_row(row: dict) -> dict:
    """Upgrade legacy overloaded status rows to distinct source and asset states."""
    legacy_status = row.get("status")
    if not row.get("asset_status") and legacy_status in ASSET_STATUSES:
        row["asset_status"] = legacy_status
    if not row.get("source_status"):
        if legacy_status in ASSET_STATUSES:
            row["source_status"] = "metadata_ok" if row.get("telegram_file_id") else "unresolved"
        else:
            row["source_status"] = legacy_status or "pending"

    old_error = row.pop("error_type", None)
    if old_error:
        if row.get("source_status") in SOURCE_ERROR_STATUSES:
            row.setdefault("source_error_type", old_error)
        elif row.get("asset_status") in {"download_error", "download_interrupted",
                                          "invalid_metadata", "metadata_changed"}:
            row.setdefault("asset_error_type", old_error)
    if row.get("source_error_type"):
        row["error_type"] = row["source_error_type"]
    row["status"] = row["source_status"]
    return row


def _set_source_status(row: dict, status: str, error_type: str | None = None) -> None:
    row["source_status"] = status
    row["status"] = status  # Legacy status remains a per-post source-status alias.
    if error_type:
        row["source_error_type"] = str(error_type)
        row["error_type"] = str(error_type)
    else:
        row.pop("source_error_type", None)
        row.pop("error_type", None)


def _set_asset_status(row: dict, status: str, error_type: str | None = None) -> None:
    row["asset_status"] = status
    if error_type:
        row["asset_error_type"] = str(error_type)
    else:
        row.pop("asset_error_type", None)


def link_verified_asset(rows: list[dict], file_id: str, result: dict,
                        downloaded_at_utc: str | None = None) -> int:
    """Attach one verified binary record to every observation of its file ID."""
    linked = 0
    for row in rows:
        if str(row.get("telegram_file_id") or "") != str(file_id):
            continue
        _set_asset_status(row, "downloaded_verified")
        row.update(
            local_file=str(result["path"]),
            file_size_bytes=int(result["size_bytes"]),
            sha256=str(result["sha256"]),
            downloaded_at_utc=downloaded_at_utc or utc_now(),
        )
        linked += 1
    return linked


def quarantine_orphan_part_files(media_dir: str | Path,
                                 quarantine_dir: str | Path) -> int:
    """Move crash-left collector staging files aside without overwriting prior files."""
    staging = Path(media_dir)
    quarantine = Path(quarantine_dir)
    if not staging.is_dir():
        return 0
    quarantine.mkdir(parents=True, exist_ok=True)
    moved = 0
    for candidate in sorted(staging.glob(".tg_*.part")):
        destination = quarantine / candidate.name
        suffix = 1
        while destination.exists():
            destination = quarantine / f"{candidate.name}.{suffix}"
            suffix += 1
        os.replace(candidate, destination)
        moved += 1
    return moved


def channel_snapshot_provenance(path: str | Path | None) -> dict:
    if path is None:
        return {"source": "manifest_channel_values", "path": None, "sha256": None}
    candidate = Path(path)
    if candidate.is_file():
        return {"source": "local_jsonl_snapshot", "path": str(candidate),
                "sha256": sha256_file(candidate)}
    return {"source": "missing_local_snapshot", "path": str(candidate), "sha256": None}


def build_post_receipts(rows: list[dict], attempts_by_row: dict[int, dict]) -> list[dict]:
    receipts = []
    for row in rows:
        entry = attempts_by_row.get(id(row), {})
        receipts.append({
            "channel": _channel_username(row.get("channel")),
            "message_id": row.get("message_id"),
            "attempts": list(entry.get("attempts", [])),
            "source_status": row.get("source_status", row.get("status", "unknown")),
            "source_error_type": _safe_error_label(row.get("source_error_type")),
            "asset_status": row.get("asset_status"),
            "asset_error_type": _safe_error_label(row.get("asset_error_type")),
        })
    return sorted(receipts, key=lambda post: (post["channel"], str(post["message_id"])))


def build_run_receipt(run_id: str, started_at_utc: str, completed_at_utc: str,
                      parameters: dict, channel_snapshot: dict, posts: list[dict],
                      status: str, error_type: str | None = None,
                      orphan_part_files_quarantined: int = 0) -> dict:
    receipt = {
        "schema_version": "1.0",
        "run_id": run_id,
        "collector": {
            "name": "telegram_video_archive",
            "version": COLLECTOR_VERSION,
            "method": "public_channel_message_lookup_and_document_download",
        },
        "parameters": parameters,
        "started_at_utc": started_at_utc,
        "completed_at_utc": completed_at_utc,
        "status": status,
        "access_policy": ACCESS_POLICY,
        "channel_snapshot": channel_snapshot,
        "orphan_part_files_quarantined": orphan_part_files_quarantined,
        "posts": posts,
    }
    if error_type:
        receipt["error_type"] = _safe_error_label(error_type)
    return receipt


def _record_attempt(attempts_by_row: dict[int, dict], row: dict, stage: str,
                    status: str, error_type: str | None = None) -> None:
    entry = attempts_by_row.setdefault(id(row), {"attempts": []})
    attempt = {"stage": stage, "status": status}
    if error_type:
        attempt["error_type"] = _safe_error_label(error_type)
    entry["attempts"].append(attempt)


def _safe_error_type(exc: BaseException) -> str:
    """Do not persist exception strings, which can contain user/session data."""
    return type(exc).__name__


def _safe_error_label(value) -> str | None:
    if not value:
        return None
    label = str(value)
    return label if _ERROR_TYPE_RE.fullmatch(label) else "LegacyError"


def _video_document(message):
    from telethon.tl import types

    document = getattr(message, "document", None)
    if document is None:
        return None, None
    attrs = list(getattr(document, "attributes", []) or [])
    video_attr = next((a for a in attrs if isinstance(a, types.DocumentAttributeVideo)), None)
    mime = (getattr(document, "mime_type", None) or "").lower()
    return (document, video_attr) if mime.startswith("video/") or video_attr else (None, None)


def _media_suffix(document) -> str:
    mime = getattr(document, "mime_type", None) or "video/mp4"
    suffix = mimetypes.guess_extension(mime.split(";")[0].strip()) or ".mp4"
    if suffix == ".jpe":
        return ".jpg"
    if not re.fullmatch(r"\.[a-zA-Z0-9]{1,8}", suffix):
        return ".mp4"
    return suffix


def _canonical_path(media_dir: Path, document) -> Path:
    return media_dir / f"tg_{int(document.id)}{_media_suffix(document)}"


def _apply_metadata(row: dict, message, media_dir: Path) -> bool:
    document, video_attr = _video_document(message)
    if document is None:
        old_id = row.pop("telegram_file_id", None)
        old_asset = {key: row[key] for key in ("local_file", "sha256", "file_size_bytes") if row.get(key)}
        if old_id or old_asset:
            row["previous_media_observation"] = {"telegram_file_id": str(old_id) if old_id else None,
                                                 **old_asset}
        for key in ("local_file", "sha256", "file_size_bytes", "duration_seconds", "width",
                    "height", "is_round_video"):
            row.pop(key, None)
        _set_asset_status(row, "not_applicable")
        row.update(actual_mime_type=getattr(getattr(message, "document", None), "mime_type", None))
        _set_source_status(row, "not_video")
        return False
    previous_id = row.get("telegram_file_id")
    file_id = str(document.id)
    if previous_id and str(previous_id) != file_id:
        row["previous_telegram_file_id"] = str(previous_id)
        previous_asset = {key: row[key] for key in ("local_file", "sha256", "file_size_bytes")
                          if row.get(key)}
        if previous_asset:
            row["previous_media_observation"] = {
                "telegram_file_id": str(previous_id), **previous_asset,
            }
        for key in ("local_file", "sha256", "file_size_bytes", "downloaded_at_utc",
                    "asset_status", "asset_error_type"):
            row.pop(key, None)
    row.update(
        telegram_file_id=file_id,
        actual_mime_type=getattr(document, "mime_type", None),
        file_size_bytes=int(getattr(document, "size", 0) or 0),
        duration_seconds=getattr(video_attr, "duration", None),
        width=getattr(video_attr, "w", None), height=getattr(video_attr, "h", None),
        is_round_video=bool(getattr(video_attr, "round_message", False)),
        local_file=str(_canonical_path(media_dir, document)),
    )
    _set_source_status(row, "metadata_ok")
    return True


def build_summary(rows: list[dict], manifest_path: Path, media_dir: Path) -> dict:
    source_status_counts = Counter(row.get("source_status", row.get("status", "unknown"))
                                   for row in rows)
    asset_status_counts = Counter(row.get("asset_status", "unknown") for row in rows)
    groups = group_manifest_by_file_id(rows)
    verified = set()
    total_verified_bytes = 0
    for file_id, copies in groups.items():
        row = next((r for r in copies if r.get("asset_status") == "downloaded_verified"
                    and r.get("sha256") and r.get("local_file")), None)
        if row and file_matches(row["local_file"], row.get("file_size_bytes"), row.get("sha256")):
            verified.add(file_id)
            total_verified_bytes += Path(row["local_file"]).stat().st_size
    return {
        "schema_version": "1.0", "updated_at_utc": utc_now(),
        "manifest": str(manifest_path), "media_dir": str(media_dir),
        "source_posts": len(rows), "unique_telegram_file_ids": len(groups),
        "verified_unique_media": len(verified), "verified_unique_bytes": total_verified_bytes,
        "status_counts_by_post": dict(sorted(source_status_counts.items())),
        "source_status_counts_by_post": dict(sorted(source_status_counts.items())),
        "asset_status_counts_by_post": dict(sorted(asset_status_counts.items())),
        "policy": ACCESS_POLICY,
    }


async def collect(manifest_path: Path, media_dir: Path, channel_snapshot: Path | None,
                  concurrency: int = 3, checkpoint_every: int = 10, *,
                  client_factory: Callable | None = None,
                  env_loader: Callable | None = None) -> dict:
    """Resume downloads for a previously collected public-post manifest."""
    if client_factory is None or env_loader is None:
        from tg_collect import load_env, make_client
        client_factory = client_factory or make_client
        env_loader = env_loader or load_env

    rows = [normalize_manifest_row(row) for row in read_manifest(manifest_path)]
    channel_ids = load_channel_ids(channel_snapshot)
    media_dir.mkdir(parents=True, exist_ok=True)
    quarantine = media_dir.parent / "partial_downloads"
    orphan_parts = quarantine_orphan_part_files(media_dir, quarantine)
    attempts_by_row: dict[int, dict] = {}
    messages_by_row: dict[int, object] = {}
    run_id = uuid.uuid4().hex
    started_at_utc = utc_now()
    snapshot_info = channel_snapshot_provenance(channel_snapshot)
    parameters = {
        "manifest": str(manifest_path), "media_dir": str(media_dir),
        "channel_snapshot": str(channel_snapshot) if channel_snapshot else None,
        "concurrency": int(concurrency), "checkpoint_every": int(checkpoint_every),
    }
    receipt_path = manifest_path.parent / "collection_receipts" / f"{run_id}.json"
    run_status, run_error_type = "complete", None
    summary = None

    try:
        # Re-verify each binary by file ID, then link a verified record to all
        # duplicate observations without altering their source status.
        for file_id, copies in group_manifest_by_file_id(rows).items():
            verified_row = next((row for row in copies if row.get("local_file")
                                 and file_matches(row["local_file"], row.get("file_size_bytes"),
                                                  row.get("sha256"))), None)
            if verified_row:
                result = {
                    "path": verified_row["local_file"],
                    "size_bytes": int(verified_row.get("file_size_bytes")
                                      or Path(verified_row["local_file"]).stat().st_size),
                    "sha256": verified_row.get("sha256") or sha256_file(verified_row["local_file"]),
                }
                link_verified_asset(copies, file_id, result,
                                    verified_row.get("downloaded_at_utc"))
                if not verified_row.get("sha256"):
                    verified_row["sha256"] = result["sha256"]
            else:
                recorded_paths = {str(row.get("local_file")) for row in copies if row.get("local_file")}
                for recorded_path in recorded_paths:
                    candidate = Path(recorded_path)
                    if candidate.is_file():
                        destination = quarantine / candidate.name
                        suffix = 1
                        while destination.exists():
                            destination = quarantine / f"{candidate.name}.{suffix}"
                            suffix += 1
                        os.replace(candidate, destination)
                for row in copies:
                    had_asset_record = (row.get("local_file") or row.get("sha256")
                                        or row.get("asset_status") in ASSET_STATUSES)
                    if had_asset_record:
                        row["local_file"] = None
                        row.pop("sha256", None)
                        _set_asset_status(row, "download_interrupted")
                    else:
                        row["local_file"] = None

        write_manifest_atomic(manifest_path, rows)
        post_groups: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            channel = _channel_username(row.get("channel"))
            if channel and row.get("message_id") is not None:
                post_groups[channel].append(row)

        groups = group_manifest_by_file_id(rows)
        missing_file_ids = {
            file_id for file_id, copies in groups.items()
            if not any(row.get("asset_status") == "downloaded_verified" for row in copies)
        }
        fetch_by_channel: dict[str, dict[int, dict]] = defaultdict(dict)
        for channel, posts in post_groups.items():
            for row in posts:
                file_id = str(row.get("telegram_file_id") or "")
                source_status = row.get("source_status", "unresolved")
                unresolved_source = source_status in ({"pending", "unresolved"} | SOURCE_ERROR_STATUSES)
                if unresolved_source or not file_id or file_id in missing_file_ids:
                    fetch_by_channel[channel][int(row["message_id"])] = row

        client = client_factory(env_loader())
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("Telegram session is not authorized; no download attempted")

            semaphore = asyncio.Semaphore(max(1, concurrency))

            async def fetch_channel(channel: str, by_id: dict[int, dict]):
                channel_rows = list(by_id.values())
                for row in channel_rows:
                    _record_attempt(attempts_by_row, row, "channel_resolution", "started")
                try:
                    entity = None
                    cached_id = channel_ids.get(channel)
                    if cached_id is not None:
                        try:
                            cached_entity = await client.get_entity(cached_id)
                            entity = validate_public_broadcast_entity(cached_entity, channel, cached_id)
                        except Exception:
                            entity = None
                    if entity is None:
                        entity = await client.get_entity("@" + channel)
                        entity = validate_public_broadcast_entity(entity, channel)
                    input_entity = await client.get_input_entity(entity)
                except Exception as exc:
                    error_type = _safe_error_type(exc)
                    for row in channel_rows:
                        _set_source_status(row, "channel_or_metadata_error", error_type)
                        _record_attempt(attempts_by_row, row, "channel_resolution", "error", error_type)
                    return
                for row in channel_rows:
                    _record_attempt(attempts_by_row, row, "channel_resolution", "validated")

                message_ids = list(by_id)
                for offset in range(0, len(message_ids), 100):
                    batch_ids = message_ids[offset:offset + 100]
                    batch_rows = [by_id[message_id] for message_id in batch_ids]
                    for row in batch_rows:
                        _record_attempt(attempts_by_row, row, "source_fetch", "started")
                    try:
                        async with semaphore:
                            received = await client.get_messages(input_entity, ids=batch_ids)
                    except Exception as exc:
                        error_type = _safe_error_type(exc)
                        for row in batch_rows:
                            _set_source_status(row, "channel_or_metadata_error", error_type)
                            _record_attempt(attempts_by_row, row, "source_fetch", "error", error_type)
                        continue
                    received = received if isinstance(received, (list, tuple)) else [received]
                    by_message_id = {message.id: message for message in received if message is not None}
                    for message_id in batch_ids:
                        row = by_id[message_id]
                        message = by_message_id.get(message_id)
                        if message is None:
                            _set_source_status(row, "message_unavailable", "MessageUnavailable")
                            _record_attempt(attempts_by_row, row, "source_fetch", "unavailable",
                                            "MessageUnavailable")
                            continue
                        try:
                            messages_by_row[id(row)] = message
                            _apply_metadata(row, message, media_dir)
                            _record_attempt(attempts_by_row, row, "source_fetch", row["source_status"])
                        except Exception as exc:
                            error_type = _safe_error_type(exc)
                            _set_source_status(row, "channel_or_metadata_error", error_type)
                            _record_attempt(attempts_by_row, row, "source_fetch", "error", error_type)

            await asyncio.gather(*(fetch_channel(channel, posts)
                                   for channel, posts in fetch_by_channel.items()))
            write_manifest_atomic(manifest_path, rows)

            groups = group_manifest_by_file_id(rows)
            work = []
            for file_id, copies in groups.items():
                verified_row = next((row for row in copies if row.get("local_file")
                                     and file_matches(row["local_file"], row.get("file_size_bytes"),
                                                      row.get("sha256"))), None)
                if verified_row:
                    link_verified_asset(copies, file_id, {
                        "path": verified_row["local_file"],
                        "size_bytes": int(verified_row.get("file_size_bytes")
                                          or Path(verified_row["local_file"]).stat().st_size),
                        "sha256": verified_row.get("sha256") or sha256_file(verified_row["local_file"]),
                    }, verified_row.get("downloaded_at_utc"))
                    continue
                source_row = next((row for row in copies if id(row) in messages_by_row), None)
                if source_row:
                    work.append((file_id, copies, source_row, messages_by_row[id(source_row)]))

            completed = 0
            download_semaphore = asyncio.Semaphore(max(1, concurrency))

            async def download_one(file_id, copies, source_row, message):
                nonlocal completed
                document, _ = _video_document(message)
                if document is None or str(document.id) != file_id:
                    for row in copies:
                        _set_asset_status(row, "metadata_changed", "DocumentIdMismatch")
                        _record_attempt(attempts_by_row, row, "asset_download", "error",
                                        "DocumentIdMismatch")
                    return
                expected_size = int(getattr(document, "size", 0) or 0)
                target = _canonical_path(media_dir, document)
                if expected_size <= 0:
                    for row in copies:
                        _set_asset_status(row, "invalid_metadata", "MissingExpectedSize")
                        _record_attempt(attempts_by_row, row, "asset_download", "error",
                                        "MissingExpectedSize")
                    return
                for row in copies:
                    _record_attempt(attempts_by_row, row, "asset_download", "started")
                try:
                    async with download_semaphore:
                        result = await download_atomic(
                            lambda temporary: client.download_media(message, file=str(temporary)),
                            target, expected_size,
                        )
                    link_verified_asset(rows, file_id, result)
                    for row in copies:
                        _record_attempt(attempts_by_row, row, "asset_download", "downloaded_verified")
                except Exception as exc:
                    error_type = _safe_error_type(exc)
                    for row in copies:
                        _set_asset_status(row, "download_error", error_type)
                        _record_attempt(attempts_by_row, row, "asset_download", "error", error_type)
                finally:
                    completed += 1
                    if completed % max(1, checkpoint_every) == 0 or completed == len(work):
                        write_manifest_atomic(manifest_path, rows)
                        progress = build_summary(rows, manifest_path, media_dir)
                        write_json_atomic(manifest_path.with_name("collection_summary.json"), progress)
                        print(f"verified {progress['verified_unique_media']}/{progress['unique_telegram_file_ids']} unique media; "
                              f"download step {completed}/{len(work)}", flush=True)

            await asyncio.gather(*(download_one(*item) for item in work))
        finally:
            await client.disconnect()

        summary = build_summary(rows, manifest_path, media_dir)
        summary["completed_at_utc"] = utc_now()
        write_json_atomic(manifest_path.with_name("collection_summary.json"), summary)
        write_manifest_atomic(manifest_path, rows)
    except BaseException as exc:
        run_status = "error"
        run_error_type = _safe_error_type(exc)
        raise
    finally:
        receipt = build_run_receipt(
            run_id=run_id, started_at_utc=started_at_utc, completed_at_utc=utc_now(),
            parameters=parameters, channel_snapshot=snapshot_info,
            posts=build_post_receipts(rows, attempts_by_row), status=run_status,
            error_type=run_error_type,
            orphan_part_files_quarantined=orphan_parts,
        )
        write_json_atomic(receipt_path, receipt)

    summary["run_id"] = run_id
    summary["run_receipt"] = str(receipt_path)
    write_json_atomic(manifest_path.with_name("collection_summary.json"), summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True,
                        help="Existing per-post JSONL manifest to resume")
    parser.add_argument("--media-dir", type=Path, required=True,
                        help="Destination for immutable downloaded binaries")
    parser.add_argument("--channel-snapshot", type=Path,
                        help="Optional local Telegram channels.jsonl ID cache")
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--checkpoint-every", type=int, default=10)
    args = parser.parse_args(argv)
    summary = asyncio.run(collect(args.manifest, args.media_dir, args.channel_snapshot,
                                  args.concurrency, args.checkpoint_every))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
