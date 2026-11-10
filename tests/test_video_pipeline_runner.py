from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.analysis.video_archive_adapter import build_video_index_manifest
import src.analysis.video_pipeline_runner as video_pipeline_runner
from src.analysis.video_pipeline_runner import run_video_pipeline


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def archive_manifest(tmp_path: Path) -> tuple[Path, Path]:
    video = tmp_path / "source.mp4"
    video.write_bytes(b"synthetic-video-bytes")
    row = {
        "platform": "telegram", "channel": "test", "message_id": 1,
        "post_url": "https://t.me/test/1", "status": "downloaded_verified",
        "actual_mime_type": "video/mp4", "local_file": str(video),
        "file_size_bytes": video.stat().st_size, "sha256": sha(video),
        "published_at": "2026-11-02T10:00:00Z",
    }
    manifest = tmp_path / "archive.jsonl"
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    return manifest, video


class FakeSteps:
    def __init__(self, *, fail_at: str | None = None, text_statuses: dict | None = None):
        self.calls: list[str] = []
        self.fail_at = fail_at
        self.text_statuses = text_statuses or {"asr:complete": 1, "ocr:complete": 1}
        self.text_overwrite_values: list[bool] = []

    def adapter(self, source, output, receipt, *, overwrite):
        self.calls.append("adapter")
        if self.fail_at == "adapter":
            raise RuntimeError("adapter unavailable")
        return build_video_index_manifest(source, output, receipt, overwrite=overwrite)

    def indexer(self, source, output, **kwargs):
        self.calls.append("index")
        if self.fail_at == "index":
            raise RuntimeError("ffprobe unavailable")
        source_rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
        indexed = [{"record_id": f"index-row-{number}", "source": row,
                    "media_index": {"status": "ok", "verified_media_facts": {
                        "sha256": sha(Path(row["local_path"]))}}}
                   for number, row in enumerate(source_rows, 1)]
        output.write_text("".join(json.dumps(row) + "\n" for row in indexed), encoding="utf-8")
        return {"records": len(indexed), "status_counts": {"ok": len(indexed)}}

    def extractor(self, source, output, **kwargs):
        self.calls.append("extract")
        if self.fail_at == "extract":
            raise RuntimeError("ffmpeg failed")
        output.write_text('{"record_id":"extract-row"}\n', encoding="utf-8")
        receipt_value = {"input_sha256": sha(source), "output_sha256": sha(output),
                         "summary": {"manifest_record_count": 1, "status_counts": {"complete": 1}}}
        output.with_suffix(output.suffix + ".receipt.json").write_text(json.dumps(receipt_value), encoding="utf-8")
        return {"summary": receipt_value["summary"], "receipt": receipt_value}

    def text_extractor(self, source, output, **kwargs):
        self.calls.append("text")
        self.text_overwrite_values.append(kwargs.get("overwrite", False))
        output.write_text('{"record_id":"text-row"}\n', encoding="utf-8")
        receipt_parameters = video_pipeline_runner._effective_text_parameters(
            asr_model_name=kwargs.get("asr_model_name"),
            asr_model_revision=kwargs.get("asr_model_revision"),
            asr_language=kwargs.get("asr_language"),
            asr_device=kwargs.get("asr_device", "cpu"),
            compute_type=kwargs.get("compute_type", "int8"),
            tesseract=kwargs.get("tesseract"),
            tesseract_languages=kwargs.get("tesseract_languages", "ukr+rus+eng"),
            tessdata_dir=kwargs.get("tessdata_dir"),
            timeout=kwargs.get("timeout", 1800),
            asr_model=kwargs.get("asr_model"),
        )
        receipt_value = {"input_sha256": sha(source), "output_sha256": sha(output),
                         "summary": {"unique_assets": 1, "status_counts": self.text_statuses},
                         "parameters": receipt_parameters}
        output.with_suffix(output.suffix + ".receipt.json").write_text(json.dumps(receipt_value), encoding="utf-8")
        return {"summary": receipt_value["summary"], "receipt": receipt_value}


def test_runs_stages_in_order_and_skips_optional_text_by_default(tmp_path: Path):
    manifest, _ = archive_manifest(tmp_path)
    steps = FakeSteps()
    result = run_video_pipeline(manifest, tmp_path / "run", tmp_path / "derived", steps=steps)

    assert steps.calls == ["adapter", "index", "extract"]
    assert result["status"] == "completed_bounded"
    assert result["stages"]["text"]["status"] == "skipped"
    assert result["stages"]["extraction"]["summary"]["status_counts"] == {"complete": 1}
    receipt = json.loads((tmp_path / "run" / "run_manifest.json").read_text(encoding="utf-8"))
    assert receipt["run_id"] == result["run_id"]
    assert receipt["semantic_verification"] == "not_performed"


def test_records_failed_stage_and_does_not_run_downstream_stages(tmp_path: Path):
    manifest, _ = archive_manifest(tmp_path)
    steps = FakeSteps(fail_at="index")
    result = run_video_pipeline(manifest, tmp_path / "run", tmp_path / "derived", steps=steps)

    assert steps.calls == ["adapter", "index"]
    assert result["status"] == "failed_with_receipt"
    assert result["stages"]["index"]["status"] == "failed"
    assert result["stages"]["index"]["error"] == "RuntimeError: ffprobe unavailable"
    assert result["stages"]["extraction"]["status"] == "not_run_after_failure"


def test_failure_on_resume_replaces_stale_downstream_statuses(tmp_path: Path):
    manifest, _ = archive_manifest(tmp_path)
    run_dir = tmp_path / "run"
    run_video_pipeline(manifest, run_dir, tmp_path / "derived", steps=FakeSteps())

    result = run_video_pipeline(manifest, run_dir, tmp_path / "derived", steps=FakeSteps(fail_at="index"))

    assert result["stages"]["index"]["status"] == "failed"
    assert result["stages"]["extraction"]["status"] == "not_run_after_failure"
    assert result["stages"]["text"]["status"] == "not_run_after_failure"


class VerifiedIndexSteps(FakeSteps):
    def adapter(self, source, output, receipt, *, overwrite):
        self.calls.append("adapter")
        return build_video_index_manifest(source, output, receipt, overwrite=overwrite)

    def indexer(self, source, output, **kwargs):
        return super().indexer(source, output, **kwargs)


def test_declared_telegram_digest_mismatch_cannot_be_reported_as_bounded(tmp_path: Path):
    manifest, _ = archive_manifest(tmp_path)
    row = json.loads(manifest.read_text(encoding="utf-8"))
    row["sha256"] = "0" * 64
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")

    result = run_video_pipeline(manifest, tmp_path / "run", tmp_path / "derived", steps=VerifiedIndexSteps())

    assert result["stages"]["index"]["status"] == "partial"
    assert result["stages"]["index"]["summary"]["declared_source_sha256_mismatch_count"] == 1
    assert result["status"] == "completed_partial"


def test_reused_partial_text_result_keeps_run_partial(tmp_path: Path):
    manifest, _ = archive_manifest(tmp_path)
    run_dir = tmp_path / "run"
    tesseract = _fake_version_binary(tmp_path / "tesseract", "tesseract 1.0")
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    (tessdata / "ukr.traineddata").write_bytes(b"ukr-model")
    ocr_options = {"tesseract": str(tesseract), "tesseract_languages": "ukr", "tessdata_dir": tessdata}
    first = run_video_pipeline(manifest, run_dir, tmp_path / "derived", **ocr_options,
                               steps=FakeSteps(text_statuses={"ocr:failed": 1}))
    second_steps = FakeSteps(text_statuses={"ocr:complete": 1})

    second = run_video_pipeline(manifest, run_dir, tmp_path / "derived", **ocr_options,
                                steps=second_steps)

    assert first["status"] == "completed_partial"
    assert second_steps.calls == ["adapter", "index", "extract"]
    assert second["stages"]["text"]["status"] == "partial"
    assert second["status"] == "completed_partial"


def test_unpinned_asr_alias_is_recomputed_instead_of_reusing_stale_text(tmp_path: Path):
    manifest, _ = archive_manifest(tmp_path)
    options = {"asr_model_name": "small"}
    first = run_video_pipeline(manifest, tmp_path / "run", tmp_path / "derived",
                               steps=FakeSteps(), **options)
    second_steps = FakeSteps()

    second = run_video_pipeline(manifest, tmp_path / "run", tmp_path / "derived",
                                steps=second_steps, **options)

    assert first["run_config"]["text_stage_cacheable"] is False
    assert second_steps.calls == ["adapter", "index", "extract", "text"]
    assert second["stages"]["text"]["status"] == "completed"
    assert second["stages"]["text"]["summary"].get("reused_existing_output") is not True


def test_uncacheable_text_journal_without_receipt_is_overwritten(tmp_path: Path):
    manifest, _ = archive_manifest(tmp_path)
    run_dir = tmp_path / "run"
    options = {"asr_model_name": "small"}
    run_video_pipeline(manifest, run_dir, tmp_path / "derived", steps=FakeSteps(), **options)
    (run_dir / "text_observations.jsonl.receipt.json").unlink()
    resumed_steps = FakeSteps()

    result = run_video_pipeline(manifest, run_dir, tmp_path / "derived",
                                steps=resumed_steps, **options)

    assert result["run_config"]["text_stage_cacheable"] is False
    assert resumed_steps.calls == ["adapter", "index", "extract", "text"]
    assert resumed_steps.text_overwrite_values == [True]


def _fake_ffmpeg_with_media_outputs(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "args=sys.argv[1:]\n"
        "if args == ['-version']:\n"
        " print('ffmpeg fake 1.0'); raise SystemExit(0)\n"
        "out=pathlib.Path(args[-1])\n"
        "if out.suffix == '.wav': out.write_bytes(b'WAVE-audio')\n"
        "else:\n"
        " pattern=out.name\n"
        " for n in (1,2): out.with_name(pattern.replace('%06d', f'{n:06d}')).write_bytes(b'frame-'+bytes([n]))\n"
        " print('[Parsed_showinfo_1] n: 0 pts: 0 pts_time:0.000000 fmt:yuv420p')\n"
        " print('[Parsed_showinfo_1] n: 1 pts: 5 pts_time:5.000000 fmt:yuv420p')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


class RealExtractionSteps(FakeSteps):
    def __init__(self, ffmpeg: Path):
        super().__init__()
        self.ffmpeg = ffmpeg

    def indexer(self, source, output, **kwargs):
        self.calls.append("index")
        source_rows = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
        indexed = []
        for number, row in enumerate(source_rows, 1):
            indexed.append({
                "record_id": f"index-row-{number}",
                "source": row,
                "media_index": {"status": "ok", "verified_media_facts": {
                    "sha256": row["sha256"], "size_bytes": row["size_bytes_claim"],
                    "ffprobe": {"format": {"duration": "10.0", "start_time": "0.000000"},
                                "streams": [
                                    {"codec_type": "video", "index": 0, "start_time": "0.000000"},
                                    {"codec_type": "audio", "index": 1, "start_time": "0.200000",
                                     "sample_rate": "44100", "channels": 2},
                                ]},
                }},
            })
        output.write_text("".join(json.dumps(row) + "\n" for row in indexed), encoding="utf-8")
        return {"records": len(indexed), "status_counts": {"ok": len(indexed)}}

    def extractor(self, source, output, **kwargs):
        self.calls.append("extract")
        return video_pipeline_runner.extract_manifest(
            source, output, media_dir=kwargs["media_dir"], ffmpeg=self.ffmpeg,
            interval_ms=kwargs["interval_ms"], max_frames=kwargs["max_frames"],
            timeout=kwargs["timeout"], overwrite=kwargs["overwrite"],
        )


def test_pipeline_resume_accepts_identical_real_extraction_input(tmp_path: Path):
    manifest, _ = archive_manifest(tmp_path)
    run_dir = tmp_path / "run"
    ffmpeg = _fake_ffmpeg_with_media_outputs(tmp_path / "ffmpeg")
    options = {"ffmpeg": str(ffmpeg), "asr_model_name": "small"}

    first = run_video_pipeline(manifest, run_dir, tmp_path / "derived",
                               steps=RealExtractionSteps(ffmpeg), **options)
    resumed_steps = RealExtractionSteps(ffmpeg)
    second = run_video_pipeline(manifest, run_dir, tmp_path / "derived",
                                steps=resumed_steps, **options)

    assert first["status"] == "completed_bounded"
    assert second["status"] == "completed_bounded"
    assert resumed_steps.calls == ["adapter", "index", "extract", "text"]
    assert second["stages"]["extraction"]["status"] == "completed"
    assert second["stages"]["text"]["status"] == "completed"


def test_remote_revision_is_not_marked_cacheable(monkeypatch):
    monkeypatch.setattr(video_pipeline_runner, "_asr_package_version", lambda enabled: "1.1.0")
    parameters = video_pipeline_runner._effective_text_parameters(
        asr_model_name="Systran/faster-whisper-small", asr_model_revision="abcdef123456",
        asr_language=None, asr_device="cpu", compute_type="int8", tesseract=None,
        tesseract_languages="ukr+rus+eng", tessdata_dir=None, timeout=1800,
    )

    cacheable = video_pipeline_runner._text_cacheable(
        parameters, asr_model_preloaded=False, effective_tessdata_fingerprint={},
        local_model_content_fingerprint=None,
    )

    assert parameters["asr_model_fingerprint"] == "remote_revision_requires_local_snapshot"
    assert cacheable is False


def _fake_version_binary(path: Path, version: str) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"print({version!r})\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("component", ["tesseract", "tessdata", "asr_model", "asr_model_pinned_content"])
def test_run_identity_tracks_effective_ocr_asr_and_model_data(tmp_path: Path, component: str):
    manifest, _ = archive_manifest(tmp_path)
    tesseract = _fake_version_binary(tmp_path / "tesseract", "tesseract 1.0")
    tessdata = tmp_path / "tessdata"
    tessdata.mkdir()
    (tessdata / "ukr.traineddata").write_bytes(b"language-model-v1")
    asr_model = tmp_path / "asr-model"
    asr_model.mkdir()
    (asr_model / "model.bin").write_bytes(b"asr-model-v1")
    run_dir = tmp_path / "run"
    options = {"tesseract": str(tesseract), "tesseract_languages": "ukr", "tessdata_dir": tessdata,
               "asr_model_name": str(asr_model)}
    if component == "asr_model_pinned_content":
        options["asr_model_revision"] = "local-revision-1"
    baseline = run_video_pipeline(manifest, run_dir, tmp_path / "derived", steps=FakeSteps(), **options)
    parameters = baseline["run_config"]["text_stage_parameters"]
    assert parameters["tesseract_version"] == "tesseract 1.0"
    assert parameters["tessdata_fingerprint"]["files"]["ukr"] == sha(tessdata / "ukr.traineddata")

    if component == "tesseract":
        _fake_version_binary(tesseract, "tesseract 2.0")
    elif component == "tessdata":
        (tessdata / "ukr.traineddata").write_bytes(b"language-model-v2")
    else:
        (asr_model / "model.bin").write_bytes(b"asr-model-v2")

    with pytest.raises(ValueError, match="run directory already belongs"):
        run_video_pipeline(manifest, run_dir, tmp_path / "derived", steps=FakeSteps(), **options)


@pytest.mark.parametrize(
    ("unsafe_name", "configured_text", "expected_calls"),
    [
        ("media_index.jsonl", False, ["adapter"]),
        ("local_extraction.jsonl", False, ["adapter", "index"]),
        ("local_extraction.jsonl.receipt.json", False, ["adapter", "index"]),
        ("text_observations.jsonl", True, ["adapter", "index", "extract"]),
        ("text_observations.jsonl.receipt.json", True, ["adapter", "index", "extract"]),
    ],
)
def test_refuses_symlinked_stage_outputs_before_calling_stage(
    tmp_path: Path, unsafe_name: str, configured_text: bool, expected_calls: list[str]
):
    manifest, _ = archive_manifest(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = tmp_path / "must-not-change.txt"
    target.write_text("preserve", encoding="utf-8")
    (run_dir / unsafe_name).symlink_to(target)
    steps = FakeSteps()

    kwargs = {"tesseract": "tesseract"} if configured_text else {}
    result = run_video_pipeline(manifest, run_dir, tmp_path / "derived", steps=steps, **kwargs)

    assert result["status"] == "failed_with_receipt"
    assert steps.calls == expected_calls
    assert target.read_text(encoding="utf-8") == "preserve"


def test_refuses_hard_linked_index_output_before_indexer_can_append(tmp_path: Path):
    manifest, _ = archive_manifest(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = tmp_path / "linked-output-target.jsonl"
    target.write_text("preserve this data\n", encoding="utf-8")
    (run_dir / "media_index.jsonl").hardlink_to(target)
    steps = FakeSteps()

    result = run_video_pipeline(manifest, run_dir, tmp_path / "derived", steps=steps)

    assert result["status"] == "failed_with_receipt"
    assert result["stages"]["index"]["status"] == "failed"
    assert steps.calls == ["adapter"]
    assert target.read_text(encoding="utf-8") == "preserve this data\n"


def test_run_directory_cannot_be_reused_for_different_input_snapshot(tmp_path: Path):
    manifest, video = archive_manifest(tmp_path)
    run_dir = tmp_path / "run"
    run_video_pipeline(manifest, run_dir, tmp_path / "derived", steps=FakeSteps())
    video.write_bytes(b"changed-video-bytes")
    row = json.loads(manifest.read_text(encoding="utf-8"))
    row["sha256"] = sha(video)
    row["file_size_bytes"] = video.stat().st_size
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="run directory already belongs"):
        run_video_pipeline(manifest, run_dir, tmp_path / "derived", steps=FakeSteps())


def test_checkout_detection_stops_at_parent_filesystem_boundary(tmp_path: Path, monkeypatch):
    parent = tmp_path / "home"
    (parent / ".git").mkdir(parents=True)
    downloads = parent / "Downloads"
    candidate = downloads / "SocialIncidentVideos" / "runs" / "run-1"
    candidate.mkdir(parents=True)

    def fake_filesystem_device(path: Path) -> int:
        resolved = path.resolve(strict=False)
        return 2 if resolved == downloads or downloads in resolved.parents else 1

    monkeypatch.setattr(video_pipeline_runner, "_filesystem_device", fake_filesystem_device, raising=False)

    assert not video_pipeline_runner._is_inside_git_checkout(candidate)
    project_path = parent / "project" / "src"
    project_path.mkdir(parents=True)
    assert video_pipeline_runner._is_inside_git_checkout(project_path)


def test_local_media_manifest_keeps_unavailable_platform_rows_in_coverage(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"local-platform-video")
    rows = [
        {"platform": "tiktok", "status": "already_present", "url": "https://tiktok.example/v/1",
         "video_id": "1", "local_file": str(video), "file_size_bytes": video.stat().st_size,
         "sha256": sha(video), "retrieved_at_utc": "2026-11-07T20:00:00Z"},
        {"platform": "youtube", "status": "download_failed", "url": "https://youtube.example/watch?v=2",
         "video_id": "2", "error": "access gate"},
    ]
    manifest = tmp_path / "platform-manifest.jsonl"
    manifest.write_text(json.dumps(rows[0]) + "\n\n" + json.dumps(rows[1]) + "\n", encoding="utf-8")
    steps = FakeSteps()

    result = run_video_pipeline(manifest, tmp_path / "run", tmp_path / "derived",
                                source_format="local_media_manifest", steps=steps)

    assert steps.calls == ["index", "extract"]
    assert result["source_coverage"]["platform_status_counts"] == {
        "tiktok": {"already_present": 1}, "youtube": {"download_failed": 1}}
    assert result["source_coverage"]["included_local_media_records"] == 1
    assert result["status"] == "completed_partial"
    assert result["stages"]["source_normalization"]["status"] == "partial"
    assert result["stages"]["source_normalization"]["summary"]["excluded_reason_counts"] == {
        "manifest_status_download_failed": 1}
    normalization_receipt = json.loads((tmp_path / "run" / "source_normalization.receipt.json").read_text(encoding="utf-8"))
    assert normalization_receipt["exclusions"][0]["line"] == 3
    normalized = (tmp_path / "run" / "index_input.jsonl").read_text(encoding="utf-8")
    assert "youtube" not in normalized


def _fake_ffprobe(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'format': {'duration':'2.0','start_time':'0'}, 'streams':["
        "{'codec_type':'video','index':0,'start_time':'0'},"
        "{'codec_type':'audio','index':1,'start_time':'0'}]}))\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _fake_ffmpeg(path: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "args=sys.argv[1:]\n"
        "if args == ['-version']:\n print('ffmpeg synthetic 1.0'); raise SystemExit(0)\n"
        "out=pathlib.Path(args[-1])\n"
        "if out.suffix == '.wav': out.write_bytes(b'RIFFsynthetic-wave')\n"
        "else:\n"
        " out.with_name(out.name.replace('%06d','000001')).write_bytes(b'jpeg-sample')\n"
        " print('[Parsed_showinfo_1] n: 0 pts_time:0.000000 fmt:yuv420p')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_smoke_real_stages_with_synthetic_manifest(tmp_path: Path):
    manifest, video = archive_manifest(tmp_path)
    result = run_video_pipeline(manifest, tmp_path / "run", tmp_path / "derived",
                                ffprobe=_fake_ffprobe(tmp_path / "ffprobe"),
                                ffmpeg=_fake_ffmpeg(tmp_path / "ffmpeg"))

    assert result["status"] == "completed_bounded"
    assert result["stages"]["index"]["summary"]["status_counts"] == {"ok": 1}
    assert result["stages"]["extraction"]["summary"]["status_counts"] == {"complete": 1}
    extracted = json.loads((tmp_path / "run" / "local_extraction.jsonl").read_text(encoding="utf-8"))
    assert extracted["extraction"]["source_sha256"] == sha(video)
