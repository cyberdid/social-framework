"""Extract bounded timestamped video frames and mono audio from indexed local media."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from src.media_pipeline import sha256_file


_PTS_TIME = re.compile(r"\bpts_time:([-+]?(?:\d+(?:\.\d*)?|\.\d+))")


def _command(binary: str | os.PathLike[str] | Sequence[str], args: Sequence[str]) -> list[str]:
    if isinstance(binary, (str, os.PathLike)):
        return [os.fspath(binary), *args]
    return [*(os.fspath(part) for part in binary), *args]


def _read_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid input JSONL at line {line_number}: {exc.msg}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"input JSONL line {line_number} must contain an object")
            records.append(row)
    return records


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _safe_hash(path: Path) -> tuple[str, int, tuple[int, int, int]]:
    before = path.stat()
    if not path.is_file():
        raise OSError("source media is not a regular file")
    digest = sha256_file(path)
    after = path.stat()
    signature_before = (before.st_size, before.st_mtime_ns, before.st_ino)
    signature_after = (after.st_size, after.st_mtime_ns, after.st_ino)
    if signature_before != signature_after:
        raise RuntimeError("source media changed while hashing")
    return digest, before.st_size, signature_after


def _ffmpeg_version(binary) -> str:
    try:
        completed = subprocess.run(_command(binary, ["-version"]), capture_output=True, text=True,
                                   timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    first = (completed.stdout or completed.stderr).splitlines()
    return first[0][:300] if completed.returncode == 0 and first else "unavailable"


def _asset_source(row: dict) -> tuple[Path, str, dict, list[dict]]:
    if not isinstance(row, dict):
        raise ValueError("index row must be an object")
    source = row.get("source")
    index = row.get("media_index")
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    if not isinstance(index, dict):
        raise ValueError("media_index must be an object")
    facts = index.get("verified_media_facts")
    if not isinstance(facts, dict):
        raise ValueError("media_index.verified_media_facts must be an object")
    ffprobe = facts.get("ffprobe")
    if not isinstance(ffprobe, dict):
        raise ValueError("media_index.verified_media_facts.ffprobe must be an object")
    streams = ffprobe.get("streams")
    if not isinstance(streams, list) or any(not isinstance(stream, dict) for stream in streams):
        raise ValueError("media_index.verified_media_facts.ffprobe.streams must be a list of objects")
    path_value = source.get("local_path")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError("source.local_path is required")
    path = Path(path_value).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("source.local_path must refer to a regular file")
    digest = facts.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("media_index.verified_media_facts.sha256 must be a lowercase SHA-256")
    if index.get("status") != "ok":
        raise ValueError("media_index.status must be ok")
    return path, digest, ffprobe, streams


def _validate_existing_asset(asset_dir: Path, *, expected_sha: str, parameters: dict,
                             ffmpeg_version: str) -> dict | None:
    if asset_dir.is_symlink():
        raise ValueError("asset cache directory must not be a symlink")
    receipt_path = asset_dir / "extraction.json"
    if receipt_path.is_symlink():
        raise ValueError("asset cache receipt must not be a symlink")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(receipt, dict) or receipt.get("status") not in {"complete", "partial"}:
        return None
    provenance = receipt.get("provenance")
    if (receipt.get("source_sha256") != expected_sha or receipt.get("parameters") != parameters
            or not isinstance(provenance, dict) or provenance.get("ffmpeg") != ffmpeg_version):
        return None

    def valid_derivative(path_value: object, digest_value: object, size_value: object) -> bool:
        if not isinstance(path_value, str) or not isinstance(digest_value, str):
            return False
        if not re.fullmatch(r"[a-f0-9]{64}", digest_value):
            return False
        path = Path(path_value)
        if path.is_symlink() or path.parent.resolve() != asset_dir.resolve():
            return False
        try:
            digest, size, _ = _safe_hash(path)
        except (OSError, RuntimeError):
            return False
        return digest == digest_value and type(size_value) is int and size == size_value

    frames = receipt.get("frames")
    if not isinstance(frames, list) or not frames:
        return None
    if any(not isinstance(frame, dict) or not valid_derivative(frame.get("path"), frame.get("sha256"),
                                                                   frame.get("size_bytes"))
           for frame in frames):
        return None
    audio = receipt.get("audio")
    if not isinstance(audio, dict):
        return None
    if audio.get("status") == "extracted" and not valid_derivative(
            audio.get("path"), audio.get("sha256"), audio.get("size_bytes")):
        return None
    return receipt


def _run_ffmpeg(binary, args: list[str], timeout: float) -> subprocess.CompletedProcess:
    command = _command(binary, args)
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def _extract_one(row: dict, media_dir: Path, *, ffmpeg, interval_ms: int, max_frames: int, timeout: float,
                 ffmpeg_version: str) -> dict:
    try:
        record_id = str(row.get("record_id") or "") if isinstance(row, dict) else ""
        path, expected_sha, probe, streams = _asset_source(row)
        source = row["source"]
        actual_sha, byte_size, source_signature = _safe_hash(path)
        supplied_source_sha = source.get("sha256")
        if actual_sha != expected_sha or (supplied_source_sha and supplied_source_sha != actual_sha):
            return {"record_id": record_id, "status": "failed", "error_code": "source_hash_mismatch",
                    "expected_sha256": expected_sha, "actual_sha256": actual_sha,
                    "limitations": ["No derivative was created because the indexed source digest did not match current bytes."]}
    except (OSError, ValueError, RuntimeError) as exc:
        return {"record_id": record_id, "status": "failed", "error_code": "invalid_or_changed_source",
                "error": str(exc), "limitations": ["No media observation was created."]}

    parameters = {"frame_interval_ms": interval_ms, "max_frames": max_frames, "audio_sample_rate": 16000, "audio_channels": 1}
    asset_dir = media_dir / actual_sha
    if asset_dir.exists():
        try:
            receipt = _validate_existing_asset(asset_dir, expected_sha=actual_sha, parameters=parameters,
                                               ffmpeg_version=ffmpeg_version)
        except ValueError as exc:
            return {"record_id": record_id, "status": "failed", "error_code": "existing_asset_output_conflict",
                    "asset_sha256": actual_sha, "error": str(exc)}
        if receipt:
            return {"record_id": record_id, **receipt, "reused_existing_asset_extraction": True}
        return {"record_id": record_id, "status": "failed", "error_code": "existing_asset_output_conflict",
                "asset_sha256": actual_sha, "limitations": ["Existing derivatives were not overwritten."]}

    media_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{actual_sha}.staging-", dir=media_dir))
    frame_status = "failed"
    frames = []
    error = None
    try:
        frame_pattern = staging / "frame_%06d.jpg"
        interval_seconds = interval_ms / 1000
        frame_args = [
            "-hide_banner", "-loglevel", "info", "-protocol_whitelist", "file",
            "-i", str(path), "-map", "0:v:0", "-vf", f"fps=1/{interval_seconds:g},showinfo",
            "-frames:v", str(max_frames), "-fps_mode", "passthrough", "-q:v", "2", str(frame_pattern),
        ]
        try:
            video_run = _run_ffmpeg(ffmpeg, frame_args, timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            video_run = None
            error = f"frame_extraction_failed: {exc}"
        if video_run and video_run.returncode == 0:
            files = sorted(staging.glob("frame_*.jpg"))
            pts_log = (video_run.stderr or "") + "\n" + (video_run.stdout or "")
            pts = [float(match) for match in _PTS_TIME.findall(pts_log)]
            if files and len(files) == len(pts):
                for number, (frame_path, offset_seconds) in enumerate(zip(files, pts), 1):
                    frame_sha, frame_bytes, _ = _safe_hash(frame_path)
                    frames.append({
                        "frame_id": f"{actual_sha}:frame:{number:06d}",
                        "path": str((asset_dir / frame_path.name).resolve(strict=False)),
                        "media_offset_ms": round(offset_seconds * 1000),
                        "time_basis": "ffmpeg_showinfo_pts_time",
                        "sha256": frame_sha,
                        "size_bytes": frame_bytes,
                    })
                frame_status = "extracted"
            else:
                error = "frame_pts_count_mismatch"
        elif video_run:
            error = (video_run.stderr or f"ffmpeg exited {video_run.returncode}")[-2000:]

        audio_streams = [stream for stream in streams if stream.get("codec_type") == "audio"]
        audio = {"status": "not_present", "original_start_offset_ms": None, "path": None,
                 "sha256": None, "size_bytes": None, "sample_rate": 16000, "channels": 1}
        if audio_streams:
            audio_stream = audio_streams[0]
            offset_ms = None
            try:
                start_value = audio_stream.get("start_time")
                if start_value is None:
                    start_value = (probe.get("format") or {}).get("start_time")
                if start_value is not None:
                    start_seconds = float(start_value)
                    if math.isfinite(start_seconds):
                        offset_ms = round(start_seconds * 1000)
            except (TypeError, ValueError, OverflowError):
                offset_ms = None
            audio_path = staging / "audio_16khz_mono.wav"
            audio_args = [
                "-hide_banner", "-loglevel", "error", "-protocol_whitelist", "file", "-i", str(path),
                "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "wav", str(audio_path),
            ]
            try:
                audio_run = _run_ffmpeg(ffmpeg, audio_args, timeout)
                if audio_run.returncode == 0 and audio_path.is_file():
                    audio_sha, audio_bytes, _ = _safe_hash(audio_path)
                    audio = {"status": "extracted", "original_start_offset_ms": offset_ms,
                             "path": str((asset_dir / audio_path.name).resolve(strict=False)),
                             "sha256": audio_sha, "size_bytes": audio_bytes,
                             "sample_rate": 16000, "channels": 1,
                             "source_stream_index": audio_stream.get("index")}
                else:
                    audio = {**audio, "status": "failed", "error": (audio_run.stderr or "ffmpeg audio extraction failed")[-2000:]}
            except (OSError, subprocess.TimeoutExpired) as exc:
                audio = {**audio, "status": "failed", "error": str(exc)}

        if not frames:
            return {"record_id": record_id, "status": "failed", "error_code": "frame_extraction_failed",
                    "asset_sha256": actual_sha, "error": error or "no frames and no PTS timestamps were returned"}
        status = "complete" if audio["status"] in {"extracted", "not_present"} else "partial"
        duration = (probe.get("format") or {}).get("duration")
        receipt = {
            "schema_version": "1.0",
            "status": status,
            "asset_sha256": actual_sha,
            "source_sha256": actual_sha,
            "source_path": str(path),
            "source_bytes": byte_size,
            "source_signature": {"size_bytes": source_signature[0], "mtime_ns": source_signature[1], "inode": source_signature[2]},
            "asset_path": str(asset_dir),
            "duration_seconds": float(duration) if duration is not None else None,
            "parameters": parameters,
            "frames": frames,
            "audio": audio,
            "asr": {"status": "not_configured", "text": None, "limitations": ["No speech recognition model ran in this pipeline step."]},
            "ocr": {"status": "not_configured", "spans": [], "limitations": ["No OCR engine ran in this pipeline step."]},
            "vad": {"status": "not_configured", "intervals": [], "limitations": ["No voice activity detector ran in this pipeline step."]},
            "provenance": {"producer": "social.analysis.video_local_extract@1.0", "ffmpeg": ffmpeg_version,
                           "parameters_sha256": hashlib.sha256(json.dumps(parameters, sort_keys=True).encode()).hexdigest()},
            "limitations": [
                "Frames are periodic samples, so brief actions between samples can be missed.",
                "PTS offsets identify positions in the media stream, not clock time or event time.",
                "Frame/audio extraction does not establish recording date, location, identity, or claim truth.",
            ],
        }
        receipt_path = staging / "extraction.json"
        receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        source_sha_after, _, source_signature_after = _safe_hash(path)
        if source_sha_after != actual_sha or source_signature_after != source_signature:
            raise RuntimeError("source media changed during extraction")
        os.replace(staging, asset_dir)
        return {"record_id": record_id, **receipt, "reused_existing_asset_extraction": False}
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return {"record_id": record_id, "status": "failed", "error_code": "extraction_error",
                "asset_sha256": actual_sha, "error": f"{type(exc).__name__}: {exc}"}


def extract_manifest(input_path: str | os.PathLike[str], output_path: str | os.PathLike[str], *,
                     media_dir: str | os.PathLike[str], ffmpeg: str | os.PathLike[str] | Sequence[str] = "ffmpeg",
                     interval_ms: int = 5000, max_frames: int = 120, timeout: float = 1800,
                     overwrite: bool = False) -> dict:
    input_file = Path(input_path).expanduser().resolve()
    raw_output_file = Path(output_path).expanduser()
    if raw_output_file.is_symlink():
        raise ValueError("extraction output must not be a symlink")
    output_file, media_root = raw_output_file.resolve(), Path(media_dir).expanduser()
    receipt_path = output_file.with_suffix(output_file.suffix + ".receipt.json")
    if receipt_path.is_symlink():
        raise ValueError("extraction receipt output must not be a symlink")
    if output_file == input_file or receipt_path == input_file:
        raise ValueError("input and output manifests must be different files")
    if type(interval_ms) is not int or interval_ms < 1000:
        raise ValueError("interval_ms must be an integer of at least 1000")
    if type(max_frames) is not int or max_frames < 1:
        raise ValueError("max_frames must be a positive integer")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if not math.isfinite(timeout):
        raise ValueError("timeout must be finite")
    if media_root.is_symlink():
        raise ValueError("media_dir must not be a symlink")
    media_root_resolved = media_root.resolve()
    records = _read_jsonl(input_file)
    for row in records:
        source = row.get("source") or {}
        local_path = source.get("local_path")
        if isinstance(local_path, str) and Path(local_path).expanduser().resolve() in {output_file, receipt_path}:
            raise ValueError("output manifest must not alias a source media file")
    if output_file.exists() or receipt_path.exists():
        if not overwrite:
            raise FileExistsError("refusing to overwrite extraction manifest; pass overwrite=True explicitly")
        for path in (output_file, receipt_path):
            if path.exists() and not path.is_file():
                raise FileExistsError("extraction outputs must be regular files")

    version = _ffmpeg_version(ffmpeg)
    cache = {}
    output_rows = []
    unique_processed = set()
    for row in records:
        try:
            _, digest, _, _ = _asset_source(row)
        except Exception:
            digest = None
        if digest and digest in cache:
            extraction = {"record_id": row.get("record_id"), **cache[digest], "reused_existing_asset_extraction": True}
        else:
            extraction = _extract_one(row, media_root_resolved, ffmpeg=ffmpeg,
                                      interval_ms=interval_ms, max_frames=max_frames,
                                      timeout=timeout, ffmpeg_version=version)
            if digest and extraction.get("status") in {"complete", "partial"}:
                cache[digest] = {key: value for key, value in extraction.items() if key != "record_id"}
                unique_processed.add(digest)
        output_rows.append({"record_id": row.get("record_id"), "source_locator": (row.get("source") or {}).get("source_locator"),
                            "asset_sha256": extraction.get("asset_sha256"), "extraction": extraction})

    status_counts = Counter(row["extraction"].get("status", "unknown") for row in output_rows)
    reused_count = sum(row["extraction"].get("reused_existing_asset_extraction") is True
                       for row in output_rows if isinstance(row.get("extraction"), dict))
    manifest_rows = []
    for row in output_rows:
        extraction = row.get("extraction")
        if isinstance(extraction, dict) and "reused_existing_asset_extraction" in extraction:
            extraction = {key: value for key, value in extraction.items()
                          if key != "reused_existing_asset_extraction"}
        manifest_rows.append({**row, "extraction": extraction})
    serial = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
                     for row in manifest_rows)
    if output_file.exists() and not overwrite:
        raise FileExistsError(str(output_file))
    _write_atomic(output_file, serial)
    output_sha = hashlib.sha256(serial.encode("utf-8")).hexdigest()
    receipt = {
        "schema_version": "1.0",
        "producer": "social.analysis.video_local_extract@1.0",
        "input_path": str(input_file),
        "input_sha256": sha256_file(input_file),
        "output_path": str(output_file),
        "output_sha256": output_sha,
        "media_dir": str(media_root_resolved),
        "parameters": {"frame_interval_ms": interval_ms, "max_frames": max_frames, "timeout_seconds": timeout},
        "ffmpeg": version,
        "summary": {"manifest_record_count": len(output_rows), "unique_assets_processed": len(unique_processed),
                    "reused_existing_asset_extraction_count": reused_count,
                    "status_counts": dict(sorted(status_counts.items()))},
        "limitations": ["Frame samples and extracted audio are derivatives; original bytes are retained and hashed.",
                        "ASR/OCR/VAD were not run by this step.",
                        "Technical extraction does not establish location, event time, identity, or claim truth."],
    }
    _write_atomic(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"records": output_rows, "summary": receipt["summary"], "receipt": receipt}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_index_jsonl", type=Path)
    parser.add_argument("output_jsonl", type=Path)
    parser.add_argument("--media-dir", required=True, type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--interval-ms", type=int, default=5000)
    parser.add_argument("--max-frames", type=int, default=120)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    result = extract_manifest(args.input_index_jsonl, args.output_jsonl, media_dir=args.media_dir,
                              ffmpeg=args.ffmpeg, interval_ms=args.interval_ms,
                              max_frames=args.max_frames, timeout=args.timeout, overwrite=args.overwrite)
    print(json.dumps(result["summary"], ensure_ascii=False, sort_keys=True))
    return 0 if result["summary"]["status_counts"].get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
