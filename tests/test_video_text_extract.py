from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.analysis.video_text_extract import extract_text_manifest


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_extraction_record(root: Path, *, record_id: str = "row-1", locator: str = "https://t.me/x/1",
                           audio_offset_ms: int | None = 200) -> dict:
    asset_dir = root / "asset"
    asset_dir.mkdir(parents=True, exist_ok=True)
    frame = asset_dir / "frame_000001.jpg"
    frame.write_bytes(b"jpeg-frame")
    audio = asset_dir / "audio.wav"
    audio.write_bytes(b"wav-audio")
    return {
        "record_id": record_id,
        "source_locator": locator,
        "asset_sha256": "a" * 64,
        "extraction": {
            "status": "complete", "asset_sha256": "a" * 64, "source_sha256": "a" * 64,
            "asset_path": str(asset_dir),
            "frames": [{"frame_id": "frame-1", "path": str(frame), "sha256": _digest(frame),
                        "media_offset_ms": 5000, "time_basis": "ffmpeg_showinfo_pts_time"}],
            "audio": {"status": "extracted", "path": str(audio), "sha256": _digest(audio),
                      "original_start_offset_ms": audio_offset_ms},
        },
    }


def write_upstream_receipt(row: dict) -> None:
    extraction = {key: value for key, value in row["extraction"].items()
                  if key not in {"record_id", "reused_existing_asset_extraction"}}
    (Path(extraction["asset_path"]) / "extraction.json").write_text(
        json.dumps(extraction), encoding="utf-8")


def make_tesseract(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "if sys.argv == ['--version']:\n"
        " print('tesseract 5.3.4'); raise SystemExit(0)\n"
        "print('level\\tpage_num\\tblock_num\\tpar_num\\tline_num\\tword_num\\tleft\\ttop\\twidth\\theight\\tconf\\ttext')\n"
        "print('5\\t1\\t1\\t1\\t1\\t1\\t10\\t20\\t80\\t12\\t91.5\\tБорщагівка')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class FakeASR:
    def __init__(self):
        self.language = None

    def transcribe(self, audio, **kwargs):
        assert kwargs["word_timestamps"] is True
        assert kwargs["vad_filter"] is True
        self.language = kwargs.get("language")
        word = SimpleNamespace(word=" привіт", start=1.0, end=1.4, probability=0.8)
        segment = SimpleNamespace(text=" привіт", start=1.0, end=1.4, words=[word],
                                  avg_logprob=-0.2, no_speech_prob=0.01)
        return [segment], SimpleNamespace(language="uk", language_probability=0.9)


def test_extracts_candidate_asr_and_ocr_with_media_offsets(tmp_path: Path):
    source = tmp_path / "local-extraction.jsonl"
    row = make_extraction_record(tmp_path)
    write_upstream_receipt(row)
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    tesseract = make_tesseract(tmp_path / "tesseract")

    model = FakeASR()
    result = extract_text_manifest(source, tmp_path / "text.jsonl", asr_model_name="fake-small",
                                   asr_language="uk", asr_model=model, tesseract=tesseract)
    row = result["records"][0]
    assert row["asr"]["status"] == "complete"
    assert row["asr"]["language"] == "uk"
    assert model.language == "uk"
    assert row["asr"]["segments"][0]["start_ms"] == 1200
    assert row["asr"]["segments"][0]["end_ms"] == 1600
    assert row["ocr"]["spans"][0]["text"] == "Борщагівка"
    assert row["ocr"]["spans"][0]["media_offset_ms"] == 5000
    assert row["ocr"]["spans"][0]["engine_confidence"] == 91.5
    assert row["ocr"]["spans"][0]["confidence_is_calibrated"] is False
    assert row["vad"]["intervals"] == []
    assert row["source_publications"] == [{"record_id": "row-1", "source_locator": "https://t.me/x/1"}]


def test_groups_same_asset_and_preserves_all_publication_sources(tmp_path: Path):
    first = make_extraction_record(tmp_path / "one", record_id="row-1")
    second = make_extraction_record(tmp_path / "two", record_id="row-2", locator="https://t.me/y/2")
    second["extraction"]["asset_path"] = first["extraction"]["asset_path"]
    second["extraction"]["frames"] = first["extraction"]["frames"]
    second["extraction"]["audio"] = first["extraction"]["audio"]
    write_upstream_receipt(first)
    source = tmp_path / "local-extraction.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in (first, second)) + "\n", encoding="utf-8")

    result = extract_text_manifest(source, tmp_path / "text.jsonl")
    assert len(result["records"]) == 1
    assert [item["record_id"] for item in result["records"][0]["source_publications"]] == ["row-1", "row-2"]
    assert result["records"][0]["asr"]["status"] == "not_configured"
    assert result["records"][0]["ocr"]["status"] == "not_configured"


def test_reports_hash_mismatches_without_reading_untrusted_derivatives(tmp_path: Path):
    row = make_extraction_record(tmp_path)
    row["extraction"]["frames"][0]["sha256"] = "0" * 64
    write_upstream_receipt(row)
    source = tmp_path / "local-extraction.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    tesseract = make_tesseract(tmp_path / "tesseract")

    result = extract_text_manifest(source, tmp_path / "text.jsonl", tesseract=tesseract)
    assert result["records"][0]["ocr"]["status"] == "failed"
    assert "derivative_hash_mismatch" in result["records"][0]["ocr"]["frame_errors"]


def test_refuses_input_alias_and_nonfinite_timeout(tmp_path: Path):
    source = tmp_path / "local-extraction.jsonl"
    source.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="different"):
        extract_text_manifest(source, source)
    with pytest.raises(ValueError, match="finite positive"):
        extract_text_manifest(source, tmp_path / "text.jsonl", timeout=float("inf"))


def test_retranscribes_asset_after_receipt_loss_for_preloaded_model(tmp_path: Path):
    source = tmp_path / "local-extraction.jsonl"
    row = make_extraction_record(tmp_path)
    write_upstream_receipt(row)
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    output = tmp_path / "text.jsonl"
    tesseract = make_tesseract(tmp_path / "tesseract")
    extract_text_manifest(source, output, asr_model_name="fake-small", asr_model=FakeASR(), tesseract=tesseract)
    output.with_suffix(output.suffix + ".receipt.json").unlink()

    class CountingASR(FakeASR):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def transcribe(self, *args, **kwargs):
            self.calls += 1
            return super().transcribe(*args, **kwargs)

    replacement_model = CountingASR()
    resumed = extract_text_manifest(source, output, asr_model_name="fake-small", asr_model=replacement_model,
                                    tesseract=tesseract)
    assert len(resumed["records"]) == 1
    assert resumed["records"][0]["asr"]["segments"][0]["text"] == "привіт"
    assert replacement_model.calls == 1
    receipt = json.loads(output.with_suffix(output.suffix + ".receipt.json").read_text(encoding="utf-8"))
    assert receipt["input_sha256"] == _digest(source)
    assert receipt["output_sha256"] == _digest(output)


def test_rejects_audio_outside_asset_directory_even_when_digest_matches(tmp_path: Path):
    row = make_extraction_record(tmp_path)
    write_upstream_receipt(row)
    outside = tmp_path / "unrelated.wav"
    outside.write_bytes(b"unrelated audio")
    row["extraction"]["audio"]["path"] = str(outside)
    row["extraction"]["audio"]["sha256"] = _digest(outside)
    source = tmp_path / "local-extraction.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")

    result = extract_text_manifest(source, tmp_path / "text.jsonl", asr_model_name="fake-small",
                                   asr_model=FakeASR(), tesseract=make_tesseract(tmp_path / "tesseract"))
    assert result["records"][0]["asr"]["status"] == "failed"
    assert result["records"][0]["asr"]["error"] == "upstream_extraction_receipt_mismatch"


def test_failed_overwrite_preserves_previous_output_and_receipt(tmp_path: Path):
    row = make_extraction_record(tmp_path)
    write_upstream_receipt(row)
    source = tmp_path / "local-extraction.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    output = tmp_path / "text.jsonl"
    output.write_text("prior report\n", encoding="utf-8")
    receipt = output.with_suffix(output.suffix + ".receipt.json")
    receipt.write_text("prior receipt\n", encoding="utf-8")

    def fail_to_load(*args):
        raise RuntimeError("model load failed")

    with pytest.raises(RuntimeError, match="model load failed"):
        extract_text_manifest(source, output, asr_model_name="missing", overwrite=True,
                              asr_model_factory=fail_to_load)
    assert output.read_text(encoding="utf-8") == "prior report\n"
    assert receipt.read_text(encoding="utf-8") == "prior receipt\n"


def test_local_model_revision_cache_identity_tracks_model_bytes(tmp_path: Path):
    row = make_extraction_record(tmp_path)
    write_upstream_receipt(row)
    source = tmp_path / "local-extraction.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    model_dir = tmp_path / "pinned-model"
    model_dir.mkdir()
    weights = model_dir / "model.bin"
    weights.write_bytes(b"model-v1")
    output = tmp_path / "text.jsonl"

    class CountingASR(FakeASR):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def transcribe(self, audio, **kwargs):
            self.calls += 1
            return super().transcribe(audio, **kwargs)

    loaded_models = []

    def factory(model_path, device, compute_type):
        assert model_path == str(model_dir)
        model = CountingASR()
        loaded_models.append(model)
        return model

    first = extract_text_manifest(source, output, asr_model_name=str(model_dir),
                                  asr_model_revision="declared-revision", asr_model_factory=factory)
    first_fingerprint = first["receipt"]["parameters"]["asr_model_fingerprint"]
    assert first_fingerprint != "declared-revision"
    assert first_fingerprint == hashlib.sha256(
        json.dumps([("model.bin", _digest(weights))], separators=(",", ":")).encode()
    ).hexdigest()

    output.with_suffix(output.suffix + ".receipt.json").unlink()
    weights.write_bytes(b"model-v2")
    second = extract_text_manifest(source, output, asr_model_name=str(model_dir),
                                   asr_model_revision="declared-revision", asr_model_factory=factory)

    assert loaded_models[0].calls == 1
    assert loaded_models[1].calls == 1
    assert second["records"][0]["analysis_key"] != first["records"][0]["analysis_key"]
    assert second["receipt"]["parameters"]["asr_model_fingerprint"] != first_fingerprint


def test_remote_revision_is_rejected_before_model_factory_runs(tmp_path: Path):
    row = make_extraction_record(tmp_path)
    write_upstream_receipt(row)
    source = tmp_path / "local-extraction.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    output = tmp_path / "text.jsonl"

    def unexpected_factory(*args):
        raise AssertionError("remote revision must not load an unpinned model")

    with pytest.raises(ValueError, match="local model snapshot"):
        extract_text_manifest(source, output, asr_model_name="Systran/faster-whisper-small",
                              asr_model_revision="abcdef123456", asr_model_factory=unexpected_factory)
    assert not output.exists()


def test_duplicate_frame_pixels_preserve_each_occurrence_timestamp(tmp_path: Path):
    row = make_extraction_record(tmp_path)
    original = Path(row["extraction"]["frames"][0]["path"])
    duplicate = original.parent / "frame_000002.jpg"
    duplicate.write_bytes(original.read_bytes())
    row["extraction"]["frames"].append({"frame_id": "frame-2", "path": str(duplicate),
                                         "sha256": _digest(duplicate), "media_offset_ms": 10000,
                                         "time_basis": "ffmpeg_showinfo_pts_time"})
    write_upstream_receipt(row)
    source = tmp_path / "local-extraction.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = extract_text_manifest(source, tmp_path / "text.jsonl",
                                   tesseract=make_tesseract(tmp_path / "tesseract"))
    spans = result["records"][0]["ocr"]["spans"]
    assert [span["media_offset_ms"] for span in spans] == [5000, 10000]


def test_malformed_asset_path_is_a_per_asset_failure(tmp_path: Path):
    row = make_extraction_record(tmp_path)
    row["extraction"]["asset_path"] = ["invalid"]
    source = tmp_path / "local-extraction.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = extract_text_manifest(source, tmp_path / "text.jsonl", asr_model_name="fake-small",
                                   asr_model=FakeASR(), tesseract=make_tesseract(tmp_path / "tesseract"))
    assert result["records"][0]["asr"]["status"] == "failed"
    assert result["records"][0]["ocr"]["status"] == "failed"


def test_unknown_audio_stream_offset_keeps_asr_times_audio_relative(tmp_path: Path):
    row = make_extraction_record(tmp_path, audio_offset_ms=None)
    write_upstream_receipt(row)
    source = tmp_path / "local-extraction.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    result = extract_text_manifest(source, tmp_path / "text.jsonl", asr_model_name="fake-small",
                                   asr_model=FakeASR())
    segment = result["records"][0]["asr"]["segments"][0]
    assert segment["start_ms"] == 1000
    assert segment["time_basis"] == "audio_relative_extraction"


def test_no_asr_segments_are_not_claimed_as_proof_of_no_speech(tmp_path: Path):
    row = make_extraction_record(tmp_path)
    write_upstream_receipt(row)
    source = tmp_path / "local-extraction.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")

    class EmptyASR:
        def transcribe(self, *args, **kwargs):
            return [], SimpleNamespace(language="uk", language_probability=0.4)

    result = extract_text_manifest(source, tmp_path / "text.jsonl", asr_model_name="empty", asr_model=EmptyASR())
    assert result["records"][0]["asr"]["status"] == "no_segments_returned"


def test_output_symlink_is_rejected_before_overwrite(tmp_path: Path):
    row = make_extraction_record(tmp_path)
    write_upstream_receipt(row)
    source = tmp_path / "local-extraction.jsonl"
    source.write_text(json.dumps(row) + "\n", encoding="utf-8")
    target = tmp_path / "valuable.txt"
    target.write_text("keep me\n", encoding="utf-8")
    output = tmp_path / "text.jsonl"
    output.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        extract_text_manifest(source, output, overwrite=True)
    assert target.read_text(encoding="utf-8") == "keep me\n"
