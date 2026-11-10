from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.analysis.video_archive_adapter import build_video_index_manifest


def archive_row(path: Path, *, status: str = "downloaded_verified", sha256: str | None = None,
                size: int | None = None, mime: str = "video/mp4", message_id: int = 123) -> dict:
    data = path.read_bytes() if path.exists() else b""
    return {
        "platform": "telegram",
        "channel": "public_channel",
        "message_id": message_id,
        "post_url": f"https://t.me/public_channel/{message_id}",
        "published_at": "2026-11-02T10:00:00+00:00",
        "retrieved_at_utc": "2026-11-07T19:00:00+00:00",
        "status": status,
        "actual_mime_type": mime,
        "local_file": str(path),
        "file_size_bytes": len(data) if size is None else size,
        "sha256": hashlib.sha256(data).hexdigest() if sha256 is None else sha256,
        "caption": "must not be copied into the technical video index",
    }


def write_manifest(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_adapts_verified_video_and_omits_caption(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"sample video bytes")
    source = tmp_path / "archive.jsonl"
    output = tmp_path / "index-input.jsonl"
    receipt = tmp_path / "index-input.receipt.json"
    write_manifest(source, [archive_row(video)])

    result = build_video_index_manifest(source, output, receipt)
    row = json.loads(output.read_text(encoding="utf-8"))
    provenance = json.loads(receipt.read_text(encoding="utf-8"))

    assert result["summary"]["included_records"] == 1
    assert row["local_path"] == str(video.resolve())
    assert row["source_locator"] == "https://t.me/public_channel/123"
    assert row["sha256"] == hashlib.sha256(video.read_bytes()).hexdigest()
    assert "caption" not in row
    assert "source" not in row
    assert provenance["input_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert provenance["summary"]["unique_media_count"] == 1


def test_excludes_non_downloaded_missing_mismatched_and_nonvideo_rows(tmp_path: Path):
    valid = tmp_path / "valid.mp4"
    valid.write_bytes(b"valid bytes")
    mismatch = tmp_path / "mismatch.mp4"
    mismatch.write_bytes(b"other bytes")
    absent = tmp_path / "absent.mp4"
    source = tmp_path / "archive.jsonl"
    output = tmp_path / "index-input.jsonl"
    receipt = tmp_path / "index-input.receipt.json"
    rows = [
        archive_row(valid, status="metadata_ok", message_id=1),
        archive_row(absent, message_id=2),
        archive_row(mismatch, size=999, message_id=3),
        archive_row(valid, mime="image/jpeg", message_id=4),
        archive_row(valid, message_id=5),
    ]
    write_manifest(source, rows)

    result = build_video_index_manifest(source, output, receipt)
    assert result["summary"]["included_records"] == 1
    assert result["summary"]["excluded_reasons"] == {
        "archive_status_metadata_ok": 1,
        "file_missing": 1,
        "file_size_mismatch": 1,
        "not_video_media_type": 1,
    }


def test_refuses_aliases_and_overwrite_without_explicit_flag(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"sample video bytes")
    source = tmp_path / "archive.jsonl"
    write_manifest(source, [archive_row(video)])

    with pytest.raises(ValueError, match="different"):
        build_video_index_manifest(source, source, tmp_path / "receipt.json")
    with pytest.raises(ValueError, match="source media"):
        build_video_index_manifest(source, video, tmp_path / "receipt.json")
    output = tmp_path / "index-input.jsonl"
    receipt = tmp_path / "index-input.receipt.json"
    build_video_index_manifest(source, output, receipt)
    with pytest.raises(FileExistsError):
        build_video_index_manifest(source, output, receipt)


def test_rejects_invalid_jsonl_without_overwriting_existing_output(tmp_path: Path):
    source = tmp_path / "archive.jsonl"
    output = tmp_path / "index-input.jsonl"
    receipt = tmp_path / "receipt.json"
    source.write_text("not json\n", encoding="utf-8")
    output.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(ValueError, match="line 1"):
        build_video_index_manifest(source, output, receipt, overwrite=True)
    assert output.read_text(encoding="utf-8") == "preserve\n"


def test_malformed_source_url_is_excluded_without_aborting_batch(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"sample video bytes")
    good = archive_row(video, message_id=1)
    malformed = {**archive_row(video, message_id=2), "post_url": "https://[bad-host/t.me/x"}
    source = tmp_path / "archive.jsonl"
    output = tmp_path / "index-input.jsonl"
    receipt = tmp_path / "receipt.json"
    write_manifest(source, [good, malformed])

    result = build_video_index_manifest(source, output, receipt)
    assert result["summary"]["included_records"] == 1
    assert result["summary"]["excluded_reasons"] == {"invalid_source_locator": 1}
