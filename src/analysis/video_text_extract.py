"""Create timestamped, candidate-only ASR and OCR records from local video derivatives."""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import io
import json
import math
import os
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from src.media_pipeline import sha256_file


def _read_snapshot(path: Path) -> tuple[list[dict], str]:
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError("input manifest changed while reading")
    rows = []
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"input JSONL is not UTF-8: {exc}") from exc
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_no}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"JSONL line {line_no} must contain an object")
        rows.append(row)
    return rows, hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _file_integrity(path_value: Any, expected_sha: Any) -> tuple[Path | None, str | None]:
    if not isinstance(path_value, str) or not isinstance(expected_sha, str):
        return None, "path_or_digest_missing"
    path = Path(path_value).expanduser()
    if path.is_symlink():
        return None, "derivative_is_symlink"
    try:
        path = path.resolve(strict=True)
        if not path.is_file():
            return None, "derivative_not_regular_file"
        actual = sha256_file(path)
    except (OSError, RuntimeError):
        return None, "derivative_unavailable"
    if actual != expected_sha:
        return None, "derivative_hash_mismatch"
    return path, None


def _asset_groups(records: list[dict]) -> tuple[list[dict], list[dict]]:
    grouped: dict[str, dict] = {}
    skipped = []
    for row in records:
        extraction = row.get("extraction")
        if not isinstance(extraction, dict):
            skipped.append({"record_id": row.get("record_id"), "reason": "missing_extraction_object"})
            continue
        digest = extraction.get("asset_sha256") or row.get("asset_sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            skipped.append({"record_id": row.get("record_id"), "reason": "missing_asset_sha256"})
            continue
        if extraction.get("status") not in {"complete", "partial"}:
            skipped.append({"record_id": row.get("record_id"), "reason": "local_extraction_not_usable"})
            continue
        asset = grouped.setdefault(digest, {"asset_sha256": digest, "extractions": [], "publications": []})
        asset["extractions"].append(extraction)
        asset["publications"].append({"record_id": row.get("record_id"),
                                      "source_locator": row.get("source_locator")})
    return list(grouped.values()), skipped


def _tesseract_version(binary: str | os.PathLike[str]) -> str:
    try:
        result = subprocess.run([os.fspath(binary), "--version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0][:300] if result.returncode == 0 and lines else "unavailable"


def _ocr_frame(binary: str | os.PathLike[str], frame_path: Path, *, languages: str,
               tessdata_dir: str | os.PathLike[str] | None, timeout: float) -> list[dict]:
    command = [os.fspath(binary), str(frame_path), "stdout", "--psm", "6", "-l", languages, "tsv"]
    if tessdata_dir:
        command[1:1] = ["--tessdata-dir", os.fspath(tessdata_dir)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or f"tesseract exited {result.returncode}")[-1500:])
    spans = []
    for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf", "-1"))
            box = {key: int(row.get(key, "0")) for key in ("left", "top", "width", "height")}
        except (TypeError, ValueError):
            continue
        if not math.isfinite(confidence):
            continue
        spans.append({"text": text, "bbox_px": box, "engine_confidence": confidence,
                      "confidence_is_calibrated": False})
    return spans


def _asr_audio(model: Any, audio_path: Path, *, language: str | None, offset_ms: int | None) -> dict:
    segments, info = model.transcribe(str(audio_path), beam_size=5, word_timestamps=True,
                                      vad_filter=True, language=language)
    output = []
    for segment in segments:
        effective_offset = offset_ms if offset_ms is not None else 0
        start_ms = effective_offset + round(float(segment.start) * 1000)
        end_ms = effective_offset + round(float(segment.end) * 1000)
        words = []
        for word in getattr(segment, "words", None) or []:
            if word.start is None or word.end is None:
                continue
            words.append({"text": word.word, "start_ms": effective_offset + round(float(word.start) * 1000),
                          "end_ms": effective_offset + round(float(word.end) * 1000),
                          "engine_probability": getattr(word, "probability", None)})
        output.append({"text": str(segment.text).strip(), "start_ms": start_ms, "end_ms": end_ms,
                       "time_basis": "audio_stream_start_plus_asr_offset" if offset_ms is not None
                       else "audio_relative_extraction",
                       "avg_logprob": getattr(segment, "avg_logprob", None),
                       "no_speech_probability": getattr(segment, "no_speech_prob", None),
                       "words": words})
    language_code = getattr(info, "language", None)
    return {"status": "complete" if output else "no_segments_returned", "language": language_code,
            "language_probability": getattr(info, "language_probability", None),
            "segments": output, "vad_filter_applied": True,
            "vad_status": "integrated_filter_applied_vad_intervals_not_exposed"}


def _safe_relative_path(path: Path, root: Path) -> bool:
    try:
        return path.parent.resolve() == root.resolve()
    except (OSError, RuntimeError):
        return False


def _validated_extraction_directory(extraction: dict, asset_sha256: str) -> tuple[Path | None, str | None]:
    """Require a derivative record to match its immutable per-asset extraction receipt."""
    asset_value = extraction.get("asset_path")
    if not isinstance(asset_value, str) or not asset_value.strip():
        return None, "asset_path_invalid"
    raw_directory = Path(asset_value).expanduser()
    if raw_directory.is_symlink():
        return None, "asset_directory_is_symlink"
    try:
        directory = raw_directory.resolve(strict=True)
        if not directory.is_dir():
            return None, "asset_directory_unavailable"
        receipt_path = directory / "extraction.json"
        if receipt_path.is_symlink() or not receipt_path.is_file():
            return None, "upstream_extraction_receipt_unavailable"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, json.JSONDecodeError):
        return None, "upstream_extraction_receipt_unavailable"
    if not isinstance(receipt, dict):
        return None, "upstream_extraction_receipt_invalid"
    if (receipt.get("status") not in {"complete", "partial"}
            or receipt.get("asset_sha256") != asset_sha256
            or receipt.get("source_sha256") != asset_sha256):
        return None, "upstream_extraction_receipt_mismatch"
    try:
        receipt_directory = Path(receipt.get("asset_path", "")).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError):
        return None, "upstream_extraction_receipt_mismatch"
    if receipt_directory != directory:
        return None, "upstream_extraction_receipt_mismatch"
    for key in ("status", "asset_sha256", "asset_path", "frames", "audio", "parameters",
                "provenance", "source_sha256", "source_path", "source_bytes", "source_signature",
                "duration_seconds"):
        if key in receipt and extraction.get(key) != receipt[key]:
            return None, "upstream_extraction_receipt_mismatch"
    return directory, None


def _tessdata_fingerprint(tessdata_dir: str | os.PathLike[str] | None, languages: str) -> dict:
    root_value = tessdata_dir or os.environ.get("TESSDATA_PREFIX")
    if not root_value:
        return {"configured_path": None, "files": "unresolved_default_tessdata"}
    root = Path(root_value).expanduser()
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return {"configured_path": str(root), "files": "unavailable"}
    hashes = {}
    for language in sorted(set(part.strip() for part in languages.split("+") if part.strip())):
        traineddata = root / f"{language}.traineddata"
        try:
            hashes[language] = sha256_file(traineddata) if traineddata.is_file() and not traineddata.is_symlink() else None
        except OSError:
            hashes[language] = None
    return {"configured_path": str(root), "files": hashes}


def _model_artifact_fingerprint(model_name: str | None, revision: str | None) -> str:
    if not model_name:
        return "not_configured"
    local_path = Path(model_name).expanduser()
    if local_path.exists() and local_path.is_file():
        try:
            return sha256_file(local_path)
        except OSError:
            return "local_model_unavailable"
    if local_path.exists() and local_path.is_dir():
        entries = []
        try:
            for file_path in sorted(item for item in local_path.rglob("*") if item.is_file()):
                resolved = file_path.resolve(strict=True)
                before = resolved.stat()
                digest = sha256_file(resolved)
                after = resolved.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                        after.st_size, after.st_mtime_ns, after.st_ino):
                    return "local_model_changed_during_hash"
                entries.append((str(file_path.relative_to(local_path)), digest))
        except (OSError, RuntimeError):
            return "local_model_unavailable"
        if not entries:
            return "local_model_unavailable"
        return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()
    if revision:
        return "remote_revision_requires_local_snapshot"
    return "provider_alias_revision_unpinned"


def _asset_analysis_key(asset: dict, *, asr_model_name: str | None, asr_device: str,
                        compute_type: str, asr_language: str | None,
                        tesseract: str | os.PathLike[str] | None,
                        tesseract_version: str, tesseract_languages: str,
                        tessdata_fingerprint: dict, asr_package_version: str,
                        asr_model_fingerprint: str) -> str:
    dependencies = []
    for extraction in asset["extractions"]:
        audio = extraction.get("audio") if isinstance(extraction.get("audio"), dict) else {}
        dependencies.append({
            "asset_path": extraction.get("asset_path"),
            "audio": {key: audio.get(key) for key in ("path", "sha256", "status", "original_start_offset_ms")},
            "frames": [{key: frame.get(key) for key in ("path", "sha256", "media_offset_ms", "time_basis")}
                       for frame in extraction.get("frames") or [] if isinstance(frame, dict)],
        })
    payload = {"asset_sha256": asset["asset_sha256"], "publications": asset["publications"],
               "dependencies": dependencies, "asr_model": asr_model_name, "asr_device": asr_device,
               "asr_model_fingerprint": asr_model_fingerprint, "asr_package_version": asr_package_version,
               "asr_language": asr_language,
               "compute_type": compute_type, "tesseract": os.fspath(tesseract) if tesseract else None,
               "tesseract_version": tesseract_version, "tesseract_languages": tesseract_languages,
               "tessdata_fingerprint": tessdata_fingerprint}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _cached_dependencies_are_intact(asset: dict) -> bool:
    for extraction in asset["extractions"]:
        directory, error = _validated_extraction_directory(extraction, asset["asset_sha256"])
        if error or directory is None:
            return False
        audio = extraction.get("audio") if isinstance(extraction.get("audio"), dict) else {}
        if audio.get("status") == "extracted":
            path, error = _file_integrity(audio.get("path"), audio.get("sha256"))
            if error or path is None or not _safe_relative_path(path, directory):
                return False
        frames = extraction.get("frames")
        if not isinstance(frames, list):
            return False
        for frame in frames:
            if not isinstance(frame, dict):
                return False
            path, error = _file_integrity(frame.get("path"), frame.get("sha256"))
            if error or path is None or not _safe_relative_path(path, directory):
                return False
    return True


def _read_journal(path: Path) -> dict[str, dict]:
    latest = {}
    if not path.exists():
        return latest
    payload = path.read_bytes()
    if payload and not payload.endswith(b"\n"):
        last_start = payload.rfind(b"\n") + 1
        try:
            json.loads(payload[last_start:].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = payload[:last_start]
            path.write_bytes(payload)
        else:
            payload += b"\n"
            path.write_bytes(payload)
    for line_no, line in enumerate(payload.splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid existing text journal at line {line_no}: {exc}") from exc
        if not isinstance(record, dict) or not isinstance(record.get("record_id"), str):
            raise ValueError(f"existing text journal line {line_no} has no record_id")
        latest[record["record_id"]] = record
    return latest


def _analyze_asset(asset: dict, *, asr_model: Any, asr_model_name: str | None,
                   asr_language: str | None,
                   tesseract: str | os.PathLike[str] | None, tesseract_languages: str,
                   tessdata_dir: str | os.PathLike[str] | None, tesseract_timeout: float,
                   tesseract_version: str, asr_device: str, compute_type: str,
                   asr_package_version: str, asr_model_fingerprint: str, analysis_key: str) -> dict:
    digest = asset["asset_sha256"]
    extractions = asset["extractions"]
    primary = extractions[0]
    primary_directory, primary_error = _validated_extraction_directory(primary, digest)
    audio_record = primary.get("audio") if isinstance(primary.get("audio"), dict) else {}
    audio_status = audio_record.get("status")
    asr = {"status": "not_configured", "model": asr_model_name, "segments": [],
           "limitations": ["No speech recognition model ran for this asset."]}
    if asr_model is not None:
        if primary_error:
            asr = {"status": "failed", "model": asr_model_name, "segments": [], "error": primary_error}
        elif audio_status != "extracted":
            asr = {"status": "audio_not_available", "model": asr_model_name, "segments": [],
                   "limitations": ["The source has no successfully extracted audio derivative."]}
        else:
            audio_path, error = _file_integrity(audio_record.get("path"), audio_record.get("sha256"))
            if error or audio_path is None or primary_directory is None or not _safe_relative_path(audio_path, primary_directory):
                asr = {"status": "failed", "model": asr_model_name, "segments": [], "error": error}
                if not error:
                    asr["error"] = "audio_outside_asset_directory"
            else:
                try:
                    offset = audio_record.get("original_start_offset_ms")
                    result = _asr_audio(asr_model, audio_path, language=asr_language,
                                        offset_ms=offset if type(offset) is int else None)
                    asr = {**result, "model": asr_model_name, "audio_sha256": audio_record.get("sha256"),
                           "limitations": ["ASR text and confidence are model outputs, not verified quotations.",
                                           "Integrated VAD filter is not an independent speech-activity annotation."]}
                except Exception as exc:
                    asr = {"status": "failed", "model": asr_model_name, "segments": [],
                           "error": f"{type(exc).__name__}: {exc}"}

    ocr_spans = []
    ocr_errors = []
    indexed_frames = []
    ocr_cache: dict[str, list[dict]] = {}
    for extraction in extractions:
        directory, directory_error = _validated_extraction_directory(extraction, digest)
        if directory_error or directory is None:
            ocr_errors.append(directory_error or "asset_directory_unavailable")
            continue
        frames = extraction.get("frames")
        if not isinstance(frames, list):
            ocr_errors.append("invalid_frames_record")
            continue
        for frame in frames:
            if not isinstance(frame, dict):
                ocr_errors.append("invalid_frame_record")
                continue
            frame_path, error = _file_integrity(frame.get("path"), frame.get("sha256"))
            if error or frame_path is None:
                ocr_errors.append(error or "frame_unavailable")
                continue
            if not _safe_relative_path(frame_path, directory):
                ocr_errors.append("frame_outside_asset_directory")
                continue
            indexed_frames.append((frame, frame_path))
            if tesseract is None:
                continue
            frame_digest = frame.get("sha256")
            if frame_digest not in ocr_cache:
                try:
                    ocr_cache[frame_digest] = _ocr_frame(tesseract, frame_path, languages=tesseract_languages,
                                                         tessdata_dir=tessdata_dir, timeout=tesseract_timeout)
                except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
                    ocr_errors.append(f"{type(exc).__name__}: {exc}")
                    continue
            for span_no, span in enumerate(ocr_cache[frame_digest], 1):
                ocr_spans.append({"span_id": f"{frame.get('frame_id') or frame_digest}:ocr:{span_no}", **span,
                                  "frame_id": frame.get("frame_id"), "frame_sha256": frame_digest,
                                  "media_offset_ms": frame.get("media_offset_ms"),
                                  "time_basis": frame.get("time_basis")})
    if tesseract is None:
        ocr = {"status": "not_configured", "spans": [], "frames_considered": 0,
               "limitations": ["No OCR engine was configured."]}
    else:
        ocr = {"status": "complete" if not ocr_errors else ("partial" if indexed_frames else "failed"),
               "engine": "tesseract", "engine_version": tesseract_version,
               "languages": tesseract_languages, "spans": ocr_spans,
               "frames_considered": len(indexed_frames), "frame_errors": ocr_errors,
               "limitations": ["OCR text and engine confidence are candidate observations, not verified content.",
                               "Only periodic sampled frames are examined; unsampled on-screen text can be missed."]}
    return {
        "schema_version": "1.0", "record_id": f"media_text_{digest}", "asset_sha256": digest,
        "analysis_key": analysis_key,
        "source_publications": asset["publications"],
        "asr": asr, "vad": {"status": asr.get("vad_status", "not_configured"), "intervals": [],
                               "limitations": ["No separate VAD interval model is configured in this step."]},
        "ocr": ocr,
        "provenance": {"producer": "social.analysis.video_text_extract@1.0",
                       "asr_model": asr_model_name, "asr_device": asr_device,
                       "compute_type": compute_type, "asr_package_version": asr_package_version,
                       "asr_model_fingerprint": asr_model_fingerprint,
                       "tesseract_version": tesseract_version,
                       "input_extraction_records": [row.get("record_id") for row in asset["publications"]]},
        "limitations": ["This step creates candidate transcription/OCR spans and does not verify the depicted event, speaker identity, time, location, or truth of a claim."],
    }


def extract_text_manifest(input_path: str | os.PathLike[str], output_path: str | os.PathLike[str], *,
                          asr_model_name: str | None = None, asr_language: str | None = None,
                          asr_model_revision: str | None = None,
                          asr_model: Any = None,
                          asr_model_factory: Any = None, asr_device: str = "cpu", compute_type: str = "int8",
                          tesseract: str | os.PathLike[str] | None = None,
                          tesseract_languages: str = "ukr+rus+eng",
                          tessdata_dir: str | os.PathLike[str] | None = None,
                          timeout: float = 1800, overwrite: bool = False) -> dict:
    input_file = Path(input_path).expanduser().resolve(strict=True)
    raw_output_file = Path(output_path).expanduser()
    if raw_output_file.is_symlink():
        raise ValueError("text extraction output must not be a symlink")
    output_file = raw_output_file.resolve()
    receipt_file = output_file.with_suffix(output_file.suffix + ".receipt.json")
    if output_file == input_file or receipt_file == input_file:
        raise ValueError("input and output manifests must be different files")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    if receipt_file.is_symlink():
        raise ValueError("text extraction outputs must not be symlinks")

    records, input_sha = _read_snapshot(input_file)
    assets, skipped = _asset_groups(records)
    local_model_dir = Path(asr_model_name).expanduser() if asr_model_name else None
    if asr_model_revision and (asr_model is not None or local_model_dir is None
                               or not local_model_dir.exists() or not local_model_dir.is_dir()):
        raise ValueError("asr_model_revision requires a local model snapshot directory; remote revisions are unsupported by this pinned faster-whisper loader")
    for asset in assets:
        for extraction in asset["extractions"]:
            frame_records = extraction.get("frames")
            paths = [frame.get("path") for frame in frame_records if isinstance(frame, dict)] if isinstance(frame_records, list) else []
            audio = extraction.get("audio") if isinstance(extraction.get("audio"), dict) else {}
            if audio.get("path"):
                paths.append(audio["path"])
            if isinstance(extraction.get("source_path"), str):
                paths.append(extraction["source_path"])
            directory_value = extraction.get("asset_path")
            if not isinstance(directory_value, str) or not directory_value.strip():
                continue
            try:
                directory = Path(directory_value).expanduser().resolve()
            except (OSError, RuntimeError):
                continue
            if output_file == input_file or receipt_file == input_file:
                raise ValueError("input and output manifests must be different files")
            if output_file == directory or receipt_file == directory or output_file.is_relative_to(directory) or receipt_file.is_relative_to(directory):
                raise ValueError("text extraction outputs must not be inside a media asset directory")
            for value in paths:
                if isinstance(value, str) and Path(value).expanduser().resolve() in {output_file, receipt_file}:
                    raise ValueError("text extraction outputs must not alias a media derivative")
    if receipt_file.exists() and not overwrite:
        raise FileExistsError("a completed text extraction run already exists; pass overwrite=True explicitly")
    if (output_file.exists() or receipt_file.exists()) and overwrite:
        if output_file.exists() and not output_file.is_file():
            raise FileExistsError("text extraction output must be a regular file")
    model = asr_model
    if asr_model_name and model is None:
        if asr_model_factory:
            model = asr_model_factory(asr_model_name, asr_device, compute_type)
        else:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError("install faster-whisper to enable ASR") from exc
            model = WhisperModel(asr_model_name, device=asr_device, compute_type=compute_type)

    tesseract_version = _tesseract_version(tesseract) if tesseract is not None else "not_configured"
    try:
        asr_package_version = importlib.metadata.version("faster-whisper") if model is not None else "not_configured"
    except importlib.metadata.PackageNotFoundError:
        asr_package_version = "unknown"
    asr_model_fingerprint = ("preloaded_model_unverifiable" if asr_model is not None else
                             _model_artifact_fingerprint(asr_model_name, asr_model_revision))
    tessdata_fingerprint = _tessdata_fingerprint(tessdata_dir, tesseract_languages)
    existing = _read_journal(output_file) if output_file.exists() and not overwrite else {}
    output_rows = []
    for asset in assets:
        key = _asset_analysis_key(asset, asr_model_name=asr_model_name, asr_device=asr_device,
                                  compute_type=compute_type, asr_language=asr_language, tesseract=tesseract,
                                  tesseract_version=tesseract_version,
                                  tesseract_languages=tesseract_languages,
                                  tessdata_fingerprint=tessdata_fingerprint,
                                  asr_package_version=asr_package_version,
                                  asr_model_fingerprint=asr_model_fingerprint)
        record_id = f"media_text_{asset['asset_sha256']}"
        previous = existing.get(record_id)
        if (asr_model is None and previous and previous.get("analysis_key") == key
                and _cached_dependencies_are_intact(asset)):
            output_rows.append(previous)
            continue
        current = _analyze_asset(asset, asr_model=model, asr_model_name=asr_model_name,
                                 asr_language=asr_language,
                                 tesseract=tesseract, tesseract_languages=tesseract_languages,
                                 tessdata_dir=tessdata_dir, tesseract_timeout=timeout,
                                 tesseract_version=tesseract_version, asr_device=asr_device,
                                 compute_type=compute_type, asr_package_version=asr_package_version,
                                 asr_model_fingerprint=asr_model_fingerprint,
                                 analysis_key=key)
        output_rows.append(current)
        if not overwrite:
            output_file.parent.mkdir(parents=True, exist_ok=True)
            with output_file.open("a", encoding="utf-8") as journal:
                journal.write(json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n")
                journal.flush()
                os.fsync(journal.fileno())
    counts = Counter(f"asr:{row['asr']['status']}" for row in output_rows)
    counts.update(f"ocr:{row['ocr']['status']}" for row in output_rows)
    serialized = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                        for row in output_rows)
    summary = {"source_manifest_records": len(records), "unique_assets": len(assets),
               "skipped_source_records": len(skipped), "status_counts": dict(sorted(counts.items()))}
    receipt = {"schema_version": "1.0", "producer": "social.analysis.video_text_extract@1.0",
               "input_path": str(input_file), "input_sha256": input_sha,
               "output_path": str(output_file), "output_sha256": hashlib.sha256(serialized.encode()).hexdigest(),
               "summary": summary, "skipped_source_records": skipped,
               "parameters": {"asr_model": asr_model_name, "asr_language": asr_language,
                              "asr_device": asr_device,
                              "asr_model_revision": asr_model_revision,
                              "asr_package_version": asr_package_version,
                              "asr_model_fingerprint": asr_model_fingerprint,
                              "compute_type": compute_type, "tesseract": os.fspath(tesseract) if tesseract else None,
                              "tesseract_version": tesseract_version, "tesseract_languages": tesseract_languages,
                              "tessdata_fingerprint": tessdata_fingerprint,
                              "timeout_seconds": timeout},
               "limitations": ["ASR/OCR/VAD are candidate extractors and do not by themselves establish evidence truth.",
                               "No separate standalone VAD interval output is configured.",
                               "Text confidence values are uncalibrated engine outputs."]}
    _atomic_write(output_file, serialized)
    _atomic_write(receipt_file, json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    return {"records": output_rows, "summary": summary, "receipt": receipt}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_extraction_jsonl", type=Path)
    parser.add_argument("output_text_jsonl", type=Path)
    parser.add_argument("--asr-model")
    parser.add_argument("--asr-model-revision", help="Pinned model revision/hash for cache provenance")
    parser.add_argument("--asr-language", help="Optional Whisper language code (for example: uk or ru)")
    parser.add_argument("--asr-device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--tesseract")
    parser.add_argument("--languages", default="ukr+rus+eng")
    parser.add_argument("--tessdata-dir", type=Path)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    result = extract_text_manifest(args.input_extraction_jsonl, args.output_text_jsonl,
                                   asr_model_name=args.asr_model, asr_language=args.asr_language,
                                   asr_model_revision=args.asr_model_revision,
                                   asr_device=args.asr_device,
                                   compute_type=args.compute_type, tesseract=args.tesseract,
                                   tesseract_languages=args.languages, tessdata_dir=args.tessdata_dir,
                                   timeout=args.timeout, overwrite=args.overwrite)
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
