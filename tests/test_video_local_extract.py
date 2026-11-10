from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.analysis.video_local_extract import extract_manifest


def make_index_row(path: Path, *, digest: str | None = None) -> dict:
    import hashlib
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "record_id": "record-1",
        "source": {
            "local_path": str(path),
            "source_locator": "https://example.org/video",
            "platform": "local_user_provided",
            "sha256": digest or actual,
        },
        "media_index": {
            "status": "ok",
            "verified_media_facts": {
                "sha256": actual,
                "size_bytes": path.stat().st_size,
                "ffprobe": {
                    "format": {"duration": "10.0", "start_time": "0.000000"},
                    "streams": [
                        {"codec_type": "video", "index": 0, "start_time": "0.000000"},
                        {"codec_type": "audio", "index": 1, "start_time": "0.200000", "sample_rate": "44100", "channels": 2},
                    ],
                },
            },
        },
    }


def fake_ffmpeg(path: Path, log: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(log)!r}).write_text(' '.join(sys.argv[1:]), encoding='utf-8')\n"
        "args=sys.argv[1:]\n"
        "if args == ['-version']:\n"
        " print('ffmpeg fake 1.0'); sys.exit(0)\n"
        "out=pathlib.Path(args[-1])\n"
        "if out.suffix == '.wav': out.write_bytes(b'WAVE-fake-audio')\n"
        "else:\n"
        " pattern=out.name\n"
        " for n in (1,2): out.with_name(pattern.replace('%06d', f'{n:06d}')).write_bytes(b'jpeg-frame-'+bytes([n]))\n"
        " print('[Parsed_showinfo_1] n: 0 pts: 0 pts_time:0.000000 fmt:yuv420p')\n"
        " print('[Parsed_showinfo_1] n: 1 pts: 5 pts_time:5.000000 fmt:yuv420p')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_extracts_frames_and_audio_with_offsets_and_provenance(tmp_path: Path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source bytes")
    original = media.read_bytes()
    index = tmp_path / "index.jsonl"
    index.write_text(json.dumps(make_index_row(media)) + "\n", encoding="utf-8")
    fake = fake_ffmpeg(tmp_path / "ffmpeg", tmp_path / "args.log")

    result = extract_manifest(index, tmp_path / "extraction.jsonl", media_dir=tmp_path / "derived", ffmpeg=fake,
                              interval_ms=5000, max_frames=4)
    row = result["records"][0]["extraction"]
    assert row["status"] == "complete"
    assert [frame["media_offset_ms"] for frame in row["frames"]] == [0, 5000]
    assert row["audio"]["status"] == "extracted"
    assert row["audio"]["original_start_offset_ms"] == 200
    assert row["asr"]["status"] == "not_configured"
    assert row["ocr"]["status"] == "not_configured"
    assert media.read_bytes() == original
    assert "-protocol_whitelist file" in (tmp_path / "args.log").read_text(encoding="utf-8")
    assert (tmp_path / "extraction.jsonl").exists()


def test_duplicate_rows_share_one_asset_extraction(tmp_path: Path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source bytes")
    row = make_index_row(media)
    duplicate = {**row, "record_id": "record-2", "source": {**row["source"], "source_locator": "https://example.org/copy"}}
    index = tmp_path / "index.jsonl"
    index.write_text("\n".join(json.dumps(x) for x in (row, duplicate)) + "\n", encoding="utf-8")
    fake = fake_ffmpeg(tmp_path / "ffmpeg", tmp_path / "args.log")

    result = extract_manifest(index, tmp_path / "extraction.jsonl", media_dir=tmp_path / "derived", ffmpeg=fake)
    first, second = [item["extraction"] for item in result["records"]]
    assert first["asset_sha256"] == second["asset_sha256"]
    assert first["asset_path"] == second["asset_path"]
    assert result["summary"]["unique_assets_processed"] == 1
    assert result["summary"]["manifest_record_count"] == 2


def test_rejects_hash_mismatch_and_does_not_write_extraction(tmp_path: Path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source bytes")
    row = make_index_row(media, digest="0" * 64)
    index = tmp_path / "index.jsonl"
    index.write_text(json.dumps(row) + "\n", encoding="utf-8")
    fake = fake_ffmpeg(tmp_path / "ffmpeg", tmp_path / "args.log")
    result = extract_manifest(index, tmp_path / "extraction.jsonl", media_dir=tmp_path / "derived", ffmpeg=fake)
    assert result["records"][0]["extraction"]["status"] == "failed"
    assert result["records"][0]["extraction"]["error_code"] == "source_hash_mismatch"
    assert not list((tmp_path / "derived").glob("*"))


def test_refuses_output_manifest_aliasing_input_or_media(tmp_path: Path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source bytes")
    index = tmp_path / "index.jsonl"
    index.write_text(json.dumps(make_index_row(media)) + "\n", encoding="utf-8")
    fake = fake_ffmpeg(tmp_path / "ffmpeg", tmp_path / "args.log")
    with pytest.raises(ValueError, match="different file"):
        extract_manifest(index, index, media_dir=tmp_path / "derived", ffmpeg=fake)
    with pytest.raises(ValueError, match="source media"):
        extract_manifest(index, media, media_dir=tmp_path / "derived", ffmpeg=fake)


def test_rejects_output_manifest_symlink_without_replacing_target(tmp_path: Path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source bytes")
    index = tmp_path / "index.jsonl"
    index.write_text(json.dumps(make_index_row(media)) + "\n", encoding="utf-8")
    fake = fake_ffmpeg(tmp_path / "ffmpeg", tmp_path / "args.log")
    target = tmp_path / "valuable.txt"
    target.write_text("keep me\n", encoding="utf-8")
    output = tmp_path / "extraction.jsonl"
    output.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        extract_manifest(index, output, media_dir=tmp_path / "derived", ffmpeg=fake, overwrite=True)
    assert target.read_text(encoding="utf-8") == "keep me\n"


def test_missing_stream_offset_is_preserved_as_unknown(tmp_path: Path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source bytes")
    row = make_index_row(media)
    row["media_index"]["verified_media_facts"]["ffprobe"]["streams"][1]["start_time"] = None
    row["media_index"]["verified_media_facts"]["ffprobe"]["format"]["start_time"] = None
    index = tmp_path / "index.jsonl"
    index.write_text(json.dumps(row) + "\n", encoding="utf-8")
    fake = fake_ffmpeg(tmp_path / "ffmpeg", tmp_path / "args.log")

    result = extract_manifest(index, tmp_path / "extraction.jsonl", media_dir=tmp_path / "derived", ffmpeg=fake)
    assert result["records"][0]["extraction"]["audio"]["original_start_offset_ms"] is None


@pytest.mark.parametrize("bad_field,bad_value", [("source", []), ("media_index", "bad"),
                                                   ("verified_media_facts", []), ("ffprobe", "bad"),
                                                   ("streams", "bad")])
def test_malformed_nested_index_row_is_a_row_failure(tmp_path: Path, bad_field: str, bad_value):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source bytes")
    row = make_index_row(media)
    if bad_field in {"source", "media_index"}:
        row[bad_field] = bad_value
    elif bad_field == "verified_media_facts":
        row["media_index"][bad_field] = bad_value
    elif bad_field == "ffprobe":
        row["media_index"]["verified_media_facts"][bad_field] = bad_value
    else:
        row["media_index"]["verified_media_facts"]["ffprobe"][bad_field] = bad_value
    index = tmp_path / "index.jsonl"
    index.write_text(json.dumps(row) + "\n", encoding="utf-8")
    fake = fake_ffmpeg(tmp_path / "ffmpeg", tmp_path / "args.log")

    result = extract_manifest(index, tmp_path / "extraction.jsonl", media_dir=tmp_path / "derived", ffmpeg=fake)
    extraction = result["records"][0]["extraction"]
    assert extraction["status"] == "failed"
    assert extraction["error_code"] == "invalid_or_changed_source"


def test_cache_reuse_keeps_row_identity_and_validates_derivative_hashes(tmp_path: Path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source bytes")
    first = make_index_row(media)
    index1 = tmp_path / "index1.jsonl"
    index1.write_text(json.dumps(first) + "\n", encoding="utf-8")
    fake = fake_ffmpeg(tmp_path / "ffmpeg", tmp_path / "args.log")
    derived = tmp_path / "derived"
    output1 = tmp_path / "extraction1.jsonl"
    extract_manifest(index1, output1, media_dir=derived, ffmpeg=fake)

    second = {**make_index_row(media), "record_id": "different-row", "source": {
        **first["source"], "source_locator": "https://example.org/different-post"}}
    index2 = tmp_path / "index2.jsonl"
    index2.write_text(json.dumps(second) + "\n", encoding="utf-8")
    output2 = tmp_path / "extraction2.jsonl"
    second_result = extract_manifest(index2, output2, media_dir=derived, ffmpeg=fake)
    second_row = second_result["records"][0]
    assert second_row["record_id"] == "different-row"
    assert second_row["source_locator"] == "https://example.org/different-post"
    assert second_row["extraction"]["reused_existing_asset_extraction"] is True
    assert "source_locator" not in second_row["extraction"]

    frame = Path(second_row["extraction"]["frames"][0]["path"])
    frame.unlink()
    output3 = tmp_path / "extraction3.jsonl"
    failed = extract_manifest(index2, output3, media_dir=derived, ffmpeg=fake)
    assert failed["records"][0]["extraction"]["status"] == "failed"
    assert failed["records"][0]["extraction"]["error_code"] == "existing_asset_output_conflict"


def test_identical_rerun_keeps_extraction_manifest_identity_stable(tmp_path: Path):
    media = tmp_path / "source.mp4"
    media.write_bytes(b"source bytes")
    index = tmp_path / "index.jsonl"
    index.write_text(json.dumps(make_index_row(media)) + "\n", encoding="utf-8")
    fake = fake_ffmpeg(tmp_path / "ffmpeg", tmp_path / "args.log")
    media_dir = tmp_path / "derived"
    first_output = tmp_path / "first.jsonl"
    second_output = tmp_path / "second.jsonl"

    first = extract_manifest(index, first_output, media_dir=media_dir, ffmpeg=fake)
    second = extract_manifest(index, second_output, media_dir=media_dir, ffmpeg=fake)

    assert first["records"][0]["extraction"]["reused_existing_asset_extraction"] is False
    assert second["records"][0]["extraction"]["reused_existing_asset_extraction"] is True
    assert first_output.read_bytes() == second_output.read_bytes()
    assert first["receipt"]["output_sha256"] == second["receipt"]["output_sha256"]
    assert first["summary"]["reused_existing_asset_extraction_count"] == 0
    assert second["summary"]["reused_existing_asset_extraction_count"] == 1
