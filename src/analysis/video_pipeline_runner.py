"""Run the local video intake, indexing, derivative, and optional OCR/ASR stages.

The runner never downloads media or performs semantic video verification. Raw
media and derivative assets should live outside a Git checkout. OCR/ASR output
is candidate text and must be checked against the source frame/audio before use.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import UTC, datetime
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any
from urllib.parse import urlsplit

from src.analysis.video_archive_adapter import build_video_index_manifest
from src.analysis.video_local_extract import extract_manifest
from src.analysis.video_text_extract import (_model_artifact_fingerprint, _tessdata_fingerprint,
                                             _tesseract_version, extract_text_manifest)
from src.media_pipeline import index_manifest, sha256_file


_VIDEO_SUFFIXES = {".3g2", ".3gp", ".avi", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
                   ".mpeg", ".mpg", ".mts", ".ogv", ".ts", ".webm", ".wmv"}
_SUCCESS_TEXT_STATUSES = {"complete", "no_segments_returned", "audio_not_available", "not_configured"}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _canonical_sha(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _read_jsonl_snapshot(path: Path) -> tuple[bytes, list[dict]]:
    before = path.stat()
    payload = path.read_bytes()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise RuntimeError("source manifest changed while being read")
    rows: list[dict] = []
    for line_no, line in enumerate(payload.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid source JSONL at line {line_no}: {exc}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"source JSONL line {line_no} must contain an object")
        rows.append(row)
    return payload, rows


def _atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ValueError(f"output must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _atomic_jsonl(path: Path, rows: list[dict]) -> None:
    if path.is_symlink():
        raise ValueError(f"output must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        Path(temporary_name).unlink(missing_ok=True)


def _filesystem_device(path: Path) -> int | None:
    try:
        return path.stat().st_dev
    except (FileNotFoundError, NotADirectoryError):
        return None


def _is_inside_git_checkout(path: Path) -> bool:
    candidate = path.expanduser().resolve(strict=False)
    anchor = candidate
    while True:
        device = _filesystem_device(anchor)
        if device is not None:
            break
        parent = anchor.parent
        if parent == anchor:
            return False
        anchor = parent

    # Git does not discover a parent repository across filesystem boundaries.
    # Mirror that behavior so a user-level checkout cannot claim a mounted
    # Downloads or media volume as repository content.
    for ancestor in (candidate, *candidate.parents):
        ancestor_device = _filesystem_device(ancestor)
        if ancestor_device is None:
            continue
        if ancestor_device != device:
            break
        if (ancestor / ".git").exists():
            return True
    return False


def _assert_external_output_directory(path: Path, field: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{field} must not be a symlink")
    resolved = path.expanduser().resolve(strict=False)
    if _is_inside_git_checkout(resolved):
        raise ValueError(f"{field} must be outside a Git checkout to keep media derivatives out of the repository")
    return resolved


def _platform_status_counts(rows: list[dict]) -> dict[str, dict[str, int]]:
    counters: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        platform = str(row.get("platform") or "unknown").casefold()
        status = str(row.get("status") or "missing_status")
        counters[platform][status] += 1
    return {platform: dict(sorted(counter.items())) for platform, counter in sorted(counters.items())}


def _adapt_local_media_manifest(input_path: Path, output_path: Path, receipt_path: Path, *,
                                overwrite: bool = False) -> dict:
    """Turn already-downloaded platform rows into media-indexer inputs.

    Rows representing failed or gated retrieval stay in the receipt's coverage
    counts; only locally present, hash/size-verified files enter the index.
    The function does not retry acquisition or inspect access-gated sources.
    """
    raw_output, raw_receipt = output_path, receipt_path
    if raw_output.is_symlink() or raw_receipt.is_symlink():
        raise ValueError("source normalization outputs must not be symlinks")
    source_path = input_path.expanduser().resolve(strict=True)
    output_path = output_path.expanduser().resolve(strict=False)
    receipt_path = receipt_path.expanduser().resolve(strict=False)
    if len({source_path, output_path, receipt_path}) != 3:
        raise ValueError("source input, normalized manifest, and receipt must be distinct")
    payload, rows = _read_jsonl_snapshot(source_path)
    input_sha = hashlib.sha256(payload).hexdigest()
    physical_lines = [line_no for line_no, line in enumerate(payload.splitlines(), 1) if line.strip()]
    platform_status = _platform_status_counts(rows)
    included: list[dict] = []
    exclusions: list[dict] = []
    exclusion_counts: Counter[str] = Counter()
    unique_media: dict[str, int] = {}

    def exclude(line_no: int, row: dict, reason: str) -> None:
        exclusions.append({"line": line_no, "platform": row.get("platform"), "status": row.get("status"),
                           "reason": reason})
        exclusion_counts[reason] += 1

    for line_no, row in zip(physical_lines, rows, strict=True):
        status = row.get("status")
        if status not in {"downloaded", "downloaded_verified", "already_present"}:
            exclude(line_no, row, f"manifest_status_{status}" if status else "manifest_status_missing")
            continue
        locator = row.get("url") or row.get("source_locator")
        try:
            parsed_locator = urlsplit(locator) if isinstance(locator, str) else None
            valid_locator = (parsed_locator is not None and bool(locator.strip())
                             and parsed_locator.scheme in {"http", "https"}
                             and bool(parsed_locator.netloc))
        except ValueError:
            valid_locator = False
        if not valid_locator:
            exclude(line_no, row, "invalid_source_locator")
            continue
        local_value = row.get("local_file") or row.get("local_path")
        if not isinstance(local_value, str) or not local_value.strip():
            exclude(line_no, row, "missing_local_path")
            continue
        raw_local = Path(local_value).expanduser()
        if raw_local.is_symlink():
            exclude(line_no, row, "source_media_is_symlink")
            continue
        try:
            local_path = raw_local.resolve(strict=True)
        except (OSError, RuntimeError):
            exclude(line_no, row, "source_media_missing")
            continue
        if not local_path.is_file():
            exclude(line_no, row, "source_media_not_regular_file")
            continue
        if _is_inside_git_checkout(local_path):
            raise ValueError("raw source media must be outside a Git checkout")
        if local_path.suffix.casefold() not in _VIDEO_SUFFIXES:
            exclude(line_no, row, "source_file_not_video_suffix")
            continue
        expected_size = row.get("file_size_bytes")
        if type(expected_size) is not int or expected_size < 0:
            exclude(line_no, row, "invalid_expected_size")
            continue
        expected_sha = row.get("sha256")
        if not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha) is None:
            exclude(line_no, row, "invalid_expected_sha256")
            continue
        try:
            before_hash = local_path.stat()
            actual_size = before_hash.st_size
            actual_sha = sha256_file(local_path)
            after_hash = local_path.stat()
        except OSError:
            exclude(line_no, row, "source_media_unreadable")
            continue
        if (before_hash.st_size, before_hash.st_mtime_ns, before_hash.st_ino) != (
                after_hash.st_size, after_hash.st_mtime_ns, after_hash.st_ino):
            exclude(line_no, row, "source_media_changed_during_hash")
            continue
        if actual_size != expected_size:
            exclude(line_no, row, "file_size_mismatch")
            continue
        if actual_sha != expected_sha.casefold():
            exclude(line_no, row, "file_hash_mismatch")
            continue
        platform = row.get("platform") if isinstance(row.get("platform"), str) else "unknown"
        external_id = row.get("video_id") or row.get("message_id") or row.get("id")
        record_key = _canonical_sha([platform.casefold(),
                                     str(external_id) if external_id is not None else None,
                                     locator, actual_sha])[:24]
        included.append({
            "record_id": f"{platform.casefold()}:{record_key}",
            "local_path": str(local_path),
            "source_locator": locator,
            "platform": platform,
            "fetched_at": row.get("retrieved_at_utc") or row.get("downloaded_at_utc"),
            "sha256": actual_sha,
            "provenance": {"input_manifest_sha256": input_sha, "input_line": line_no,
                           "input_status": status},
        })
        unique_media.setdefault(actual_sha, actual_size)

    if output_path.exists() or receipt_path.exists():
        if not overwrite:
            raise FileExistsError("refusing to overwrite normalized manifest; set overwrite=True")
        if any(path.is_symlink() or not path.is_file() for path in (output_path, receipt_path) if path.exists()):
            raise FileExistsError("existing normalization outputs must be regular files")
    if source_path in {output_path, receipt_path}:
        raise ValueError("input and normalization outputs must be different files")

    serialized = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
        for row in included
    )
    serialized_sha = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    summary = {
        "input_record_count": len(rows), "included_local_media_records": len(included),
        "unique_asset_count": len(unique_media), "unique_asset_bytes": sum(unique_media.values()),
        "excluded_record_count": len(exclusions), "excluded_reason_counts": dict(sorted(exclusion_counts.items())),
    }
    receipt = {
        "schema_version": "1.0", "producer": "social.analysis.video_pipeline_runner.local_manifest_adapter@1.0",
        "input_path": str(source_path), "input_sha256": input_sha,
        "output_path": str(output_path), "output_sha256": serialized_sha,
        "summary": summary, "platform_status_counts": platform_status,
        "exclusions": exclusions,
        "limitations": ["Only existing files with matching declared size and SHA-256 are indexed.",
                        "Failed/gated retrieval rows remain visible in coverage counts and are not retried.",
                        "This adapter does not copy title/caption text or verify event relevance, time, location, or claims."],
    }
    _atomic_jsonl(output_path, included)
    _atomic_json(receipt_path, receipt)
    return {"summary": summary, "receipt": receipt}


def _output_sha(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected regular, non-symlink output: {path}")
    return sha256_file(path)


def _verify_transform_receipt(receipt: dict, *, input_sha: str, output_path: Path, stage: str) -> None:
    if not isinstance(receipt, dict):
        raise RuntimeError(f"{stage} did not return a receipt")
    if receipt.get("input_sha256") != input_sha:
        raise RuntimeError(f"{stage} receipt input SHA-256 mismatch")
    if receipt.get("output_sha256") != _output_sha(output_path):
        raise RuntimeError(f"{stage} receipt output SHA-256 mismatch")


def _tool_string(value: Any) -> Any:
    if isinstance(value, (str, os.PathLike)):
        return os.fspath(value)
    if isinstance(value, (list, tuple)):
        return [os.fspath(part) if isinstance(part, os.PathLike) else str(part) for part in value]
    return str(value)


def _tool_version(value: Any) -> str:
    if isinstance(value, (str, os.PathLike)):
        command = [os.fspath(value)]
    elif isinstance(value, (list, tuple)):
        command = [os.fspath(part) if isinstance(part, os.PathLike) else str(part) for part in value]
    else:
        return "unavailable"
    try:
        result = subprocess.run([*command, "-version"], capture_output=True, text=True,
                                 timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0][:300] if result.returncode == 0 and lines else "unavailable"


def _asr_package_version(enabled: bool) -> str:
    if not enabled:
        return "not_configured"
    try:
        return importlib.metadata.version("faster-whisper")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _effective_text_parameters(*, asr_model_name: str | None, asr_model_revision: str | None,
                              asr_language: str | None, asr_device: str, compute_type: str,
                              tesseract: str | os.PathLike[str] | None,
                              tesseract_languages: str, tessdata_dir: str | os.PathLike[str] | None,
                              timeout: float, asr_model: Any = None) -> dict:
    asr_enabled = bool(asr_model_name or asr_model is not None)
    return {
        "asr_model": asr_model_name,
        "asr_language": asr_language,
        "asr_device": asr_device,
        "asr_model_revision": asr_model_revision,
        "asr_package_version": _asr_package_version(asr_enabled),
        "asr_model_fingerprint": ("preloaded_model_unverifiable" if asr_model is not None else
                                  _model_artifact_fingerprint(asr_model_name, asr_model_revision)),
        "compute_type": compute_type,
        "tesseract": os.fspath(tesseract) if tesseract else None,
        "tesseract_version": _tesseract_version(tesseract) if tesseract else "not_configured",
        "tesseract_languages": tesseract_languages,
        "tessdata_fingerprint": (_tessdata_fingerprint(tessdata_dir, tesseract_languages)
                                 if (asr_enabled or tesseract) else {"status": "not_configured"}),
        "timeout_seconds": timeout,
    }


def _effective_tessdata_fingerprint(tesseract: str | os.PathLike[str],
                                    tessdata_dir: str | os.PathLike[str] | None,
                                    languages: str) -> dict:
    fingerprint = _tessdata_fingerprint(tessdata_dir, languages)
    if fingerprint.get("files") != "unresolved_default_tessdata":
        return fingerprint
    try:
        result = subprocess.run([os.fspath(tesseract), "--list-langs"], capture_output=True, text=True,
                                 timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return fingerprint
    output = (result.stdout or result.stderr).splitlines()
    if result.returncode == 0 and output:
        match = re.search(r"in\s+['\"]([^'\"]+)['\"]", output[0])
        if match:
            return _tessdata_fingerprint(match.group(1), languages)
    return fingerprint


def _text_cacheable(parameters: dict, *, asr_model_preloaded: bool,
                    effective_tessdata_fingerprint: dict,
                    local_model_content_fingerprint: str | None) -> bool:
    if asr_model_preloaded:
        return False
    model_fingerprint = parameters.get("asr_model_fingerprint")
    if parameters.get("asr_model") and model_fingerprint in {
            "provider_alias_revision_unpinned", "remote_revision_requires_local_snapshot",
            "local_model_unavailable", "preloaded_model_unverifiable",
            "local_model_changed_during_hash"}:
        return False
    if parameters.get("asr_model") and parameters.get("asr_package_version") == "unknown":
        return False
    if local_model_content_fingerprint and re.fullmatch(r"[0-9a-f]{64}", local_model_content_fingerprint) is None:
        return False
    if parameters.get("tesseract"):
        if parameters.get("tesseract_version") == "unavailable":
            return False
        files = (effective_tessdata_fingerprint or {}).get("files")
        languages = [part.strip() for part in parameters.get("tesseract_languages", "").split("+") if part.strip()]
        if not isinstance(files, dict) or any(not files.get(language) for language in languages):
            return False
    return True


def _local_model_content_fingerprint(model_name: str | None, model_revision: str | None,
                                     declared_fingerprint: str) -> str | None:
    if not model_name:
        return None
    path = Path(model_name).expanduser()
    if not path.exists() and not path.is_symlink():
        return None
    if path.is_symlink():
        return "local_model_path_is_symlink"
    if path.is_dir():
        try:
            if any(item.is_symlink() for item in path.rglob("*")):
                return "local_model_contains_symlinks"
        except OSError:
            return "local_model_unavailable"
    if not model_revision:
        return declared_fingerprint
    try:
        if path.is_file():
            return sha256_file(path)
        if not path.is_dir():
            return "local_model_unavailable"
        entries = []
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                return "local_model_contains_symlinks"
            if not item.is_file():
                continue
            before = item.stat()
            digest = sha256_file(item)
            after = item.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (
                    after.st_size, after.st_mtime_ns, after.st_ino):
                return "local_model_changed_during_hash"
            entries.append((str(item.relative_to(path)), digest))
        return _canonical_sha(entries)
    except (OSError, RuntimeError):
        return "local_model_unavailable"


def _text_status_names(summary: dict) -> set[str]:
    names = set()
    for key, count in (summary.get("status_counts") or {}).items():
        if not isinstance(key, str) or not key.startswith(("asr:", "ocr:")):
            continue
        if type(count) is int and count == 0:
            continue
        names.add(key.split(":", 1)[1])
    return names


def _code_revision() -> str | None:
    repo = Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _pipeline_code_fingerprints() -> dict[str, str]:
    src_root = Path(__file__).resolve().parents[1]
    component_paths = {
        "video_pipeline_runner.py": Path(__file__).resolve(),
        "video_archive_adapter.py": src_root / "analysis" / "video_archive_adapter.py",
        "media_pipeline.py": src_root / "media_pipeline.py",
        "video_local_extract.py": src_root / "analysis" / "video_local_extract.py",
        "video_text_extract.py": src_root / "analysis" / "video_text_extract.py",
    }
    return {name: sha256_file(path) for name, path in component_paths.items()}


def _assert_index_inputs_outside_checkout(path: Path) -> None:
    _, rows = _read_jsonl_snapshot(path)
    for line_no, row in enumerate(rows, 1):
        local_value = row.get("local_path")
        if not isinstance(local_value, str) or not local_value.strip():
            continue
        candidate = Path(local_value).expanduser()
        if candidate.is_symlink():
            raise ValueError(f"raw media path at normalized manifest line {line_no} is a symlink")
        try:
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"raw media path at normalized manifest line {line_no} is unavailable") from exc
        if _is_inside_git_checkout(resolved):
            raise ValueError("raw source media must be outside a Git checkout")


def _assert_safe_output_files(paths: list[Path]) -> None:
    for path in paths:
        if path.is_symlink():
            raise ValueError(f"stage output must not be a symlink: {path.name}")
        if path.exists():
            if not path.is_file():
                raise ValueError(f"stage output must be a regular file: {path.name}")
            if path.stat().st_nlink > 1:
                raise ValueError(f"stage output must not have multiple hard links: {path.name}")


def _digest_claim_mismatches(input_path: Path, index_path: Path) -> list[dict]:
    _, inputs = _read_jsonl_snapshot(input_path)
    _, indexed = _read_jsonl_snapshot(index_path)
    if len(inputs) != len(indexed):
        raise RuntimeError("media index output record count does not match normalized input")
    mismatches = []
    for line_no, (source, record) in enumerate(zip(inputs, indexed, strict=True), 1):
        indexed_source = record.get("source")
        if not isinstance(indexed_source, dict) or indexed_source != source:
            raise RuntimeError(f"media index source provenance mismatch at input line {line_no}")
        declared = source.get("sha256")
        if not isinstance(declared, str):
            continue
        verified = record.get("media_index", {}).get("verified_media_facts", {}).get("sha256")
        if not isinstance(verified, str) or declared.casefold() != verified.casefold():
            mismatches.append({"input_line": line_no, "record_id": source.get("record_id"),
                               "declared_sha256": declared, "verified_sha256": verified})
    return mismatches


def _load_prior_run(path: Path, run_id: str) -> dict | None:
    if path.is_symlink():
        raise ValueError("run manifest must not be a symlink")
    if not path.exists():
        return None
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("existing run manifest is unreadable") from exc
    if not isinstance(current, dict):
        raise ValueError("existing run manifest must be an object")
    if current.get("run_id") != run_id:
        raise ValueError("run directory already belongs to a different input snapshot or pipeline configuration")
    return current


def run_video_pipeline(archive_manifest: str | os.PathLike[str], run_dir: str | os.PathLike[str],
                       derived_media_dir: str | os.PathLike[str], *,
                       source_format: str = "telegram_archive",
                       ffprobe: str | os.PathLike[str] = "ffprobe",
                       ffmpeg: str | os.PathLike[str] = "ffmpeg",
                       strict_decode: bool = False,
                       interval_ms: int = 5000,
                       max_frames: int = 120,
                       timeout: float = 1800,
                       tesseract: str | os.PathLike[str] | None = None,
                       tesseract_languages: str = "ukr+rus+eng",
                       tessdata_dir: str | os.PathLike[str] | None = None,
                       asr_model_name: str | None = None,
                       asr_model_revision: str | None = None,
                       asr_model: Any = None,
                       asr_language: str | None = None,
                       asr_device: str = "cpu",
                       compute_type: str = "int8",
                       steps: Any = None) -> dict:
    """Run deterministic local stages against one immutable source manifest.

    ``source_format`` can be ``telegram_archive`` (the resumable Telegram
    archive schema) or ``local_media_manifest`` (flat platform rows with local
    paths, status, size, and SHA-256). Downloaded files remain where they are.
    The second format keeps failed/gated retrievals in a coverage receipt while
    excluding them from media processing.
    """
    if source_format not in {"telegram_archive", "local_media_manifest"}:
        raise ValueError("source_format must be telegram_archive or local_media_manifest")
    if type(strict_decode) is not bool:
        raise ValueError("strict_decode must be boolean")
    if type(interval_ms) is not int or interval_ms < 1000:
        raise ValueError("interval_ms must be an integer of at least 1000")
    if type(max_frames) is not int or max_frames < 1:
        raise ValueError("max_frames must be a positive integer")
    if type(timeout) not in {int, float} or timeout <= 0:
        raise ValueError("timeout must be positive")
    if asr_model is not None and not asr_model_name:
        raise ValueError("asr_model_name is required when supplying a preloaded ASR model")
    input_file = Path(archive_manifest).expanduser().resolve(strict=True)
    initial_payload, input_rows = _read_jsonl_snapshot(input_file)
    input_sha = hashlib.sha256(initial_payload).hexdigest()
    run_path = _assert_external_output_directory(Path(run_dir), "run_dir")
    media_path = _assert_external_output_directory(Path(derived_media_dir), "derived_media_dir")
    if input_file.is_relative_to(run_path):
        raise ValueError("source manifest must be outside the output run directory")
    if run_path == media_path or run_path.is_relative_to(media_path) or media_path.is_relative_to(run_path):
        raise ValueError("run_dir and derived_media_dir must be separate, non-overlapping directories")
    code_fingerprints = _pipeline_code_fingerprints()
    text_enabled = bool(tesseract or asr_model_name or asr_model is not None)
    text_parameters = _effective_text_parameters(
        asr_model_name=asr_model_name, asr_model_revision=asr_model_revision,
        asr_language=asr_language, asr_device=asr_device, compute_type=compute_type,
        tesseract=tesseract, tesseract_languages=tesseract_languages,
        tessdata_dir=tessdata_dir, timeout=timeout, asr_model=asr_model,
    )
    asr_enabled = bool(asr_model_name or asr_model is not None)
    if tesseract:
        effective_tessdata_fingerprint = _effective_tessdata_fingerprint(
            tesseract, tessdata_dir, tesseract_languages)
    elif asr_enabled:
        effective_tessdata_fingerprint = _tessdata_fingerprint(tessdata_dir, tesseract_languages)
    else:
        effective_tessdata_fingerprint = {"status": "not_configured"}
    asr_local_model_content_fingerprint = _local_model_content_fingerprint(
        asr_model_name, asr_model_revision, text_parameters["asr_model_fingerprint"])
    text_cacheable = _text_cacheable(text_parameters, asr_model_preloaded=asr_model is not None,
                                    effective_tessdata_fingerprint=effective_tessdata_fingerprint,
                                    local_model_content_fingerprint=asr_local_model_content_fingerprint)

    config = {
        "schema_version": "1.0", "source_format": source_format, "input_sha256": input_sha,
        "pipeline_code_sha256": code_fingerprints,
        "ffprobe": _tool_string(ffprobe), "ffprobe_version": _tool_version(ffprobe),
        "ffmpeg": _tool_string(ffmpeg), "ffmpeg_version": _tool_version(ffmpeg),
        "strict_decode": strict_decode,
        "interval_ms": interval_ms, "max_frames": max_frames, "timeout_seconds": timeout,
        "text_enabled": text_enabled, "text_stage_parameters": text_parameters,
        "effective_tessdata_fingerprint": effective_tessdata_fingerprint,
        "asr_local_model_content_sha256": asr_local_model_content_fingerprint,
        "text_stage_cacheable": text_cacheable,
        "asr_model_preloaded": asr_model is not None,
        "tesseract": _tool_string(tesseract) if tesseract else None,
        "tesseract_languages": tesseract_languages,
        "tessdata_dir": str(Path(tessdata_dir).expanduser().resolve(strict=False)) if tessdata_dir else None,
        "asr_model_name": asr_model_name, "asr_model_revision": asr_model_revision,
        "asr_language": asr_language, "asr_device": asr_device, "compute_type": compute_type,
    }
    run_id = _canonical_sha(config)[:20]
    run_path.mkdir(parents=True, exist_ok=True)
    run_manifest_path = run_path / "run_manifest.json"
    prior = _load_prior_run(run_manifest_path, run_id)
    started_at = prior.get("started_at_utc") if prior else _utc_now()
    stage_receipts = dict(prior.get("stages", {})) if prior else {}
    manifest = {
        "schema_version": "1.0", "producer": "social.analysis.video_pipeline_runner@1.0",
        "run_id": run_id, "status": "running", "started_at_utc": started_at,
        "updated_at_utc": _utc_now(), "source_manifest_path": str(input_file),
        "source_manifest_sha256": input_sha, "source_format": source_format,
        "run_config": config, "code_revision": _code_revision(),
        "pipeline_code_sha256": code_fingerprints,
        "run_dir": str(run_path), "derived_media_dir": str(media_path),
        "stages": stage_receipts, "semantic_verification": "not_performed",
        "limitations": ["This runner performs local acquisition-manifest normalization, technical indexing, and derivative extraction only.",
                        "OCR/ASR outputs, when enabled, are candidate observations and are not verified quotations or facts.",
                        "No location, recording-time, speaker-identity, event-match, or claim-truth assessment is performed.",
                        "Raw input media are not copied or modified by the runner; keep them outside the repository."],
    }

    def save_manifest() -> None:
        manifest["updated_at_utc"] = _utc_now()
        _atomic_json(run_manifest_path, manifest)

    def get_step(name: str, default: Any) -> Any:
        if steps is None:
            return default
        if isinstance(steps, dict):
            return steps.get(name, default)
        return getattr(steps, name, default)

    def stage_record(name: str, status: str, *, summary: dict | None = None,
                     artifacts: dict | None = None, error: str | None = None) -> None:
        row = {"status": status}
        if summary is not None:
            row["summary"] = summary
        if artifacts is not None:
            row["artifacts"] = artifacts
        if error is not None:
            row["error"] = error
        row["completed_at_utc"] = _utc_now()
        manifest["stages"][name] = row
        save_manifest()

    def stage_started(name: str) -> None:
        manifest["stages"][name] = {"status": "running", "started_at_utc": _utc_now()}
        save_manifest()

    save_manifest()

    index_input = run_path / "index_input.jsonl"
    source_receipt_path = run_path / "source_normalization.receipt.json"
    media_index = run_path / "media_index.jsonl"
    extraction_manifest = run_path / "local_extraction.jsonl"
    text_manifest = run_path / "text_observations.jsonl"
    source_coverage = {"input_record_count": len(input_rows), "platform_status_counts": _platform_status_counts(input_rows),
                       "included_local_media_records": 0}
    manifest["source_coverage"] = source_coverage

    failed = False
    current_stage = "source_normalization"
    try:
        stage_started(current_stage)
        _assert_safe_output_files([index_input, source_receipt_path])
        if source_format == "telegram_archive":
            adapter_result = get_step("adapter", build_video_index_manifest)(
                input_file, index_input, source_receipt_path, overwrite=True)
            adapter_receipt = adapter_result.get("receipt", {})
            _verify_transform_receipt(adapter_receipt, input_sha=input_sha, output_path=index_input,
                                      stage="Telegram archive adapter")
            source_summary = adapter_result.get("summary", {})
        else:
            adapter_result = _adapt_local_media_manifest(input_file, index_input, source_receipt_path,
                                                         overwrite=True)
            adapter_receipt = adapter_result["receipt"]
            _verify_transform_receipt(adapter_receipt, input_sha=input_sha, output_path=index_input,
                                      stage="local media manifest adapter")
            source_summary = adapter_result["summary"]
        source_coverage["included_local_media_records"] = source_summary.get(
            "included_records", source_summary.get("included_local_media_records", 0))
        source_coverage["unique_asset_count"] = source_summary.get(
            "unique_media_count", source_summary.get("unique_asset_count", 0))
        source_coverage["excluded_record_count"] = source_summary.get(
            "excluded_records", source_summary.get("excluded_record_count", 0))
        source_coverage["excluded_reason_counts"] = source_summary.get(
            "excluded_reasons", source_summary.get("excluded_reason_counts", {}))
        if source_format == "local_media_manifest":
            source_coverage["platform_status_counts"] = adapter_receipt.get("platform_status_counts", {})
        _assert_index_inputs_outside_checkout(index_input)
        excluded_count = source_coverage["excluded_record_count"]
        normalization_status = "partial" if excluded_count else "completed"
        stage_record(current_stage, normalization_status, summary=source_summary,
                     artifacts={"index_input_jsonl": {"path": index_input.name, "sha256": _output_sha(index_input)},
                                "receipt_json": {"path": source_receipt_path.name,
                                                 "sha256": _output_sha(source_receipt_path)}})

        current_stage = "index"
        stage_started(current_stage)
        _assert_safe_output_files([media_index])
        index_result = get_step("indexer", index_manifest)(index_input, media_index, ffprobe=ffprobe,
                                                            ffmpeg=ffmpeg, strict_decode=strict_decode)
        index_counts = index_result.get("status_counts", {})
        digest_mismatches = _digest_claim_mismatches(index_input, media_index)
        index_status = "completed" if set(index_counts) <= {"ok"} and not digest_mismatches else "partial"
        index_summary = {**index_result,
                         "declared_source_sha256_mismatch_count": len(digest_mismatches),
                         "declared_source_sha256_mismatches": digest_mismatches}
        stage_record(current_stage, index_status, summary=index_summary,
                     artifacts={"media_index_jsonl": {"path": media_index.name, "sha256": _output_sha(media_index)}})

        current_stage = "extraction"
        stage_started(current_stage)
        extraction_receipt_path = extraction_manifest.with_suffix(extraction_manifest.suffix + ".receipt.json")
        _assert_safe_output_files([extraction_manifest, extraction_receipt_path])
        extraction_result = get_step("extractor", extract_manifest)(
            media_index, extraction_manifest, media_dir=media_path, ffmpeg=ffmpeg,
            interval_ms=interval_ms, max_frames=max_frames, timeout=timeout, overwrite=True)
        extraction_counts = extraction_result.get("summary", {}).get("status_counts", {})
        extraction_status = "completed" if set(extraction_counts) <= {"complete"} else "partial"
        extraction_receipt = extraction_result.get("receipt", {})
        _verify_transform_receipt(extraction_receipt, input_sha=_output_sha(media_index),
                                  output_path=extraction_manifest, stage="local media extraction")
        stage_record(current_stage, extraction_status, summary=extraction_result.get("summary", {}),
                     artifacts={"local_extraction_jsonl": {"path": extraction_manifest.name,
                                                            "sha256": _output_sha(extraction_manifest)},
                                "receipt_json": {"path": extraction_receipt_path.name,
                                                 "sha256": _output_sha(extraction_receipt_path)}})

        text_enabled = bool(tesseract or asr_model_name or asr_model is not None)
        current_stage = "text"
        stage_started(current_stage)
        existing_text_receipt = text_manifest.with_suffix(text_manifest.suffix + ".receipt.json")
        _assert_safe_output_files([text_manifest, existing_text_receipt])
        if not text_enabled:
            stage_record(current_stage, "skipped", summary={"reason": "OCR/ASR not configured"})
        else:
            reusable = False
            force_text_recompute = (not text_cacheable
                                    and (text_manifest.exists() or existing_text_receipt.exists()))
            if text_manifest.exists() or existing_text_receipt.exists():
                if text_manifest.is_symlink() or existing_text_receipt.is_symlink():
                    raise ValueError("text outputs must not be symlinks")
                _assert_safe_output_files([text_manifest, existing_text_receipt])
                if text_manifest.is_file() and existing_text_receipt.is_file():
                    try:
                        prior_text_receipt = json.loads(existing_text_receipt.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        prior_text_receipt = None
                    receipt_matches = (isinstance(prior_text_receipt, dict)
                                       and prior_text_receipt.get("input_sha256") == _output_sha(extraction_manifest)
                                       and prior_text_receipt.get("output_sha256") == _output_sha(text_manifest)
                                       and prior_text_receipt.get("parameters") == text_parameters)
                    if not receipt_matches and existing_text_receipt.exists():
                        raise ValueError("existing text-stage receipt does not match the current extraction input")
                    reusable = receipt_matches and text_cacheable
                    force_text_recompute = receipt_matches and not text_cacheable
            if reusable:
                text_summary = prior_text_receipt.get("summary", {})
                text_summary = {**text_summary, "reused_existing_output": True}
                status_values = _text_status_names(text_summary)
                text_status = "reused" if status_values <= _SUCCESS_TEXT_STATUSES else "partial"
                text_artifacts = {"text_observations_jsonl": {"path": text_manifest.name,
                                                                "sha256": _output_sha(text_manifest)},
                                  "receipt_json": {"path": existing_text_receipt.name,
                                                   "sha256": _output_sha(existing_text_receipt)}}
            else:
                text_result = get_step("text_extractor", extract_text_manifest)(
                    extraction_manifest, text_manifest, asr_model_name=asr_model_name,
                    asr_model_revision=asr_model_revision, asr_model=asr_model,
                    asr_language=asr_language, asr_device=asr_device, compute_type=compute_type,
                    tesseract=tesseract, tesseract_languages=tesseract_languages,
                    tessdata_dir=tessdata_dir, timeout=timeout, overwrite=force_text_recompute)
                text_summary = text_result.get("summary", {})
                status_values = _text_status_names(text_summary)
                text_status = "completed" if status_values <= _SUCCESS_TEXT_STATUSES else "partial"
                text_receipt = text_result.get("receipt", {})
                _verify_transform_receipt(text_receipt, input_sha=_output_sha(extraction_manifest),
                                          output_path=text_manifest, stage="OCR/ASR text extraction")
                text_artifacts = {"text_observations_jsonl": {"path": text_manifest.name,
                                                                "sha256": _output_sha(text_manifest)},
                                  "receipt_json": {"path": existing_text_receipt.name,
                                                   "sha256": _output_sha(existing_text_receipt)}}
            stage_record(current_stage, text_status, summary=text_summary, artifacts=text_artifacts)
    except Exception as exc:
        failed = True
        stage_record(current_stage, "failed", error=f"{type(exc).__name__}: {exc}")
        ordered_stages = ("source_normalization", "index", "extraction", "text")
        failed_index = ordered_stages.index(current_stage)
        for name in ordered_stages[failed_index + 1:]:
            stage_record(name, "not_run_after_failure")

    try:
        final_input_sha = hashlib.sha256(input_file.read_bytes()).hexdigest()
    except OSError:
        final_input_sha = None
    if final_input_sha != input_sha:
        manifest["input_stability_error"] = "source manifest changed during pipeline execution"
        failed = True
    statuses = {row.get("status") for row in manifest["stages"].values()}
    if failed:
        manifest["status"] = "failed_with_receipt"
    elif statuses & {"partial"}:
        manifest["status"] = "completed_partial"
    else:
        manifest["status"] = "completed_bounded"
    manifest["finished_at_utc"] = _utc_now()
    manifest["source_coverage"] = source_coverage
    save_manifest()
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive_manifest", type=Path)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--derived-media-dir", required=True, type=Path)
    parser.add_argument("--source-format", choices=("telegram_archive", "local_media_manifest"),
                        default="telegram_archive")
    parser.add_argument("--ffprobe", default="ffprobe")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--strict-decode", action="store_true")
    parser.add_argument("--interval-ms", type=int, default=5000)
    parser.add_argument("--max-frames", type=int, default=120)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--tesseract")
    parser.add_argument("--tessdata-dir", type=Path)
    parser.add_argument("--languages", default="ukr+rus+eng")
    parser.add_argument("--asr-model")
    parser.add_argument("--asr-model-revision")
    parser.add_argument("--asr-language")
    parser.add_argument("--asr-device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    args = parser.parse_args(argv)
    result = run_video_pipeline(
        args.archive_manifest, args.run_dir, args.derived_media_dir,
        source_format=args.source_format, ffprobe=args.ffprobe, ffmpeg=args.ffmpeg,
        strict_decode=args.strict_decode, interval_ms=args.interval_ms, max_frames=args.max_frames,
        timeout=args.timeout, tesseract=args.tesseract, tessdata_dir=args.tessdata_dir,
        tesseract_languages=args.languages, asr_model_name=args.asr_model,
        asr_model_revision=args.asr_model_revision, asr_language=args.asr_language,
        asr_device=args.asr_device, compute_type=args.compute_type,
    )
    print(json.dumps({"run_id": result["run_id"], "status": result["status"],
                      "stages": {key: value.get("status") for key, value in result["stages"].items()},
                      "source_coverage": result.get("source_coverage")}, ensure_ascii=False, sort_keys=True))
    return 1 if result["status"] == "failed_with_receipt" else 0


if __name__ == "__main__":
    raise SystemExit(main())
