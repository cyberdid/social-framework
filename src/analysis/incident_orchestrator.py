"""Bounded incident run controller with fixed collection and local-analysis adapters.

Planning is local. Collection requires execute=True/--execute. This module never
loads collector names, commands, callbacks, credentials, or models from JSON.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

from src import source_watch_collect
from src.analysis.offline_case_runner import _INPUTS as _OFFLINE_INPUTS, run_offline_case
from src.analysis.source_ingest_bridge import ingest_source_watch_manifest
from src.contracts import ContractValidationError, validate_incident_spec, validate_run_manifest
from src.evidence_ledger import EvidenceLedger

# Code-owned registry; IncidentSpec cannot extend or select import paths.
_ADAPTERS = {"web": source_watch_collect}
_STAGES = ("normalized", "extracting", "verifying", "reconstructing", "reporting")
_OUTCOMES = {"complete", "partial", "blocked", "not_found", "failed"}
_MAX_PASSAGE_FILE_BYTES = 128 * 1024 * 1024
_MAX_PASSAGE_RECORDS = 200_000
_MAX_PASSAGE_LINE_BYTES = 4 * 1024 * 1024
_LIMITATIONS = [
    "Only the fixed source_watch public watchlist is integrated for collection; other platform collectors are not run.",
    "Watchlist targets are not inferred from incident seed URLs or filtered by incident dates or relevance.",
    "Local analysis uses caller-typed inputs plus eligible collector passages; it does not normalize raw sources or extract claims.",
    "Retrieval results are candidates; semantic truth, source authenticity and incident completeness are not established.",
    "Detailed planned/not_run receipts project to blocked in RunManifest v1; consult detail_ref.",
    "completed_bounded means controller processing finished; it does not establish incident completeness.",
    "Collection observes present availability; as_of_utc does not reconstruct historical web content.",
]


class OrchestratorError(ValueError):
    """Invalid input or unsafe/occupied output location."""


class BridgeIntegrationError(OrchestratorError):
    """A fixed source-watch bridge step could not safely complete."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _path(value):
    path = Path(value).expanduser().absolute()
    if ".." in path.parts:
        raise OrchestratorError("path traversal is not allowed")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise OrchestratorError("symlink path components are not allowed")
    return path


def _read_json(path, limit):
    path = _path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise OrchestratorError("input must be a bounded regular JSON file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise OrchestratorError("input is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise OrchestratorError("JSON input must be an object")
    return value


def _read_bounded_file(path, limit):
    path = _path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise OrchestratorError("input must be a bounded regular file")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > limit or len(data) != info.st_size:
            raise OrchestratorError("input size changed or exceeds limit")
    finally:
        os.close(descriptor)
    return data


def _read_typed_inputs(path):
    """Read a bounded, code-owned map of input names to local paths."""
    data = _read_bounded_file(path, 1_000_000)

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    try:
        inputs = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError) as exc:
        raise OrchestratorError("typed input manifest is invalid UTF-8 JSON") from exc
    if not isinstance(inputs, dict):
        raise OrchestratorError("typed input manifest must be an object")
    if not inputs or set(inputs) - set(_OFFLINE_INPUTS):
        raise OrchestratorError("typed input manifest has unsupported or missing inputs")
    result = {}
    for name, value in inputs.items():
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise OrchestratorError("typed input manifest values must be non-empty paths")
        result[name] = _path(value)
    return result, hashlib.sha256(data).hexdigest()


def _empty_source_ingest_receipt():
    return {
        "schema_version": "1.0", "status": "not_run",
        "reason": "fresh_source_watch_manifest_unavailable",
        "source_manifest_path": None, "source_manifest_sha256": None,
        "ledger_path": None, "ledger_sha256": None,
        "merged_passages_path": None, "merged_passages_sha256": None,
        "previous_passages_sha256": None,
        "target_count": 0, "bridge_counts": {}, "records_added": 0,
        "source_count": 0, "artifact_count": 0, "passage_count": 0,
        "previous_passages_count": 0, "merged_passages_count": 0,
    }


def _parse_utc(value, *, field):
    if not isinstance(value, str) or not value:
        reason = "incident_as_of_invalid" if field == "IncidentSpec.as_of_utc" else "capture_timestamp_missing"
        raise BridgeIntegrationError(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        reason = "incident_as_of_invalid" if field == "IncidentSpec.as_of_utc" else "capture_timestamp_invalid"
        raise BridgeIntegrationError(reason) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        reason = "incident_as_of_invalid" if field == "IncidentSpec.as_of_utc" else "capture_timestamp_invalid"
        raise BridgeIntegrationError(reason)
    return parsed.astimezone(timezone.utc)


def _read_passage_jsonl(path):
    """Read a caller-owned passage input for merge without modifying its bytes."""
    data = _read_bounded_file(path, _MAX_PASSAGE_FILE_BYTES)

    def unique_object(pairs):
        row = {}
        for key, value in pairs:
            if key in row:
                raise ValueError("duplicate JSON object key")
            row[key] = value
        return row

    try:
        lines = data.decode("utf-8").splitlines()
        rows = []
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            if len(line.encode("utf-8")) > _MAX_PASSAGE_LINE_BYTES:
                raise ValueError("line too large")
            row = json.loads(line, object_pairs_hook=unique_object)
            if not isinstance(row, dict):
                raise ValueError("passage row must be an object")
            passage_id = row.get("passage_id")
            if not isinstance(passage_id, str) or not passage_id or passage_id != passage_id.strip():
                raise ValueError("passage_id is required")
            json.dumps(row, ensure_ascii=False, allow_nan=False)
            rows.append(row)
            if len(rows) > _MAX_PASSAGE_RECORDS:
                raise ValueError("too many passage rows")
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise BridgeIntegrationError("caller_passages_invalid") from exc
    return rows, hashlib.sha256(data).hexdigest()


def _passage_rows_from_ledger(records):
    sources = {row["record_id"]: row for row in records if row.get("record_type") == "Source"}
    artifacts = {row["record_id"]: row for row in records if row.get("record_type") == "Artifact"}
    rows = []
    for record in records:
        if record.get("record_type") != "Passage":
            continue
        artifact = artifacts.get(record.get("artifact_id"))
        source = sources.get(artifact.get("source_id")) if artifact else None
        if artifact is None or source is None:
            raise BridgeIntegrationError("bridge_ledger_reference_invalid")
        rows.append({
            "passage_id": record["record_id"],
            "source_id": artifact["source_id"],
            "text": record["text"],
            "captured_at_utc": artifact["captured_at"],
            "artifact_id": artifact["record_id"],
            "artifact_path": artifact["path"],
            "artifact_sha256": artifact["sha256"],
            "artifact_media_type": artifact["media_type"],
            "artifact_start_offset": None,
            "source_uri": source["uri"],
            "locator": record.get("locator"),
            "producer": record.get("producer"),
            "limitations": record.get("limitations", []),
        })
    return rows


def _merge_passage_rows(previous, fresh):
    merged = []
    by_id = {}
    for row in [*previous, *fresh]:
        passage_id = row.get("passage_id")
        if not isinstance(passage_id, str) or not passage_id:
            raise BridgeIntegrationError("caller_passages_invalid")
        prior = by_id.get(passage_id)
        if prior is not None:
            if prior != row:
                raise BridgeIntegrationError("passage_id_conflict")
            continue
        by_id[passage_id] = row
        merged.append(row)
        if len(merged) > _MAX_PASSAGE_RECORDS:
            raise BridgeIntegrationError("merged_passages_limit_exceeded")
    return merged


def _write_jsonl_new(path, rows):
    content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True,
                                 allow_nan=False) + "\n" for row in rows).encode("utf-8")
    if len(content) > _MAX_PASSAGE_FILE_BYTES:
        raise BridgeIntegrationError("merged_passages_limit_exceeded")
    with path.open("xb") as stream:
        stream.write(content)


def _capture_time_mismatch(rows, as_of_utc):
    cutoff = _parse_utc(as_of_utc, field="IncidentSpec.as_of_utc")
    if not rows:
        raise BridgeIntegrationError("fresh_manifest_empty")
    for row in rows:
        if not isinstance(row, dict):
            raise BridgeIntegrationError("fresh_manifest_invalid")
        captured = _parse_utc(row.get("requested_at"), field="SourceWatch.requested_at")
        if captured > cutoff:
            return True
    return False


def _build_source_ingest_receipt(manifest_path, run_dir, *, as_of_utc,
                                 previous_passages_path=None):
    """Bridge one exact fresh manifest and retain a merged, typed passage file."""
    receipt_data = _empty_source_ingest_receipt()
    run_dir = Path(run_dir)
    merged_path = None
    ledger_path = run_dir / "evidence_ledger.sqlite"
    merged_file = run_dir / "merged_passages.jsonl"
    manifest_path = _path(manifest_path)
    try:
        manifest_relative = manifest_path.relative_to(run_dir).as_posix()
    except ValueError as exc:
        raise BridgeIntegrationError("fresh_manifest_outside_run") from exc
    receipt_data["source_manifest_path"] = manifest_relative
    try:
        manifest_bytes = _read_bounded_file(manifest_path, 10_000_000)
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("rows"), list):
            raise BridgeIntegrationError("fresh_manifest_invalid")
        receipt_data["source_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
        receipt_data["target_count"] = len(manifest["rows"])
        try:
            time_mismatch = _capture_time_mismatch(manifest["rows"], as_of_utc)
        except BridgeIntegrationError as exc:
            time_mismatch = False
            time_error = exc.reason
        else:
            time_error = None

        with EvidenceLedger(ledger_path) as ledger:
            bridge_summary = ingest_source_watch_manifest(manifest_path, ledger, root=run_dir)
            records = ledger.records()
        counts = bridge_summary.get("counts", {})
        if isinstance(counts, dict):
            receipt_data["bridge_counts"] = {
                key: value for key, value in counts.items()
                if isinstance(key, str) and type(value) is int and value >= 0
            }
        receipt_data["records_added"] = bridge_summary.get("records_added", 0)
        receipt_data["source_count"] = sum(row.get("record_type") == "Source" for row in records)
        receipt_data["artifact_count"] = sum(row.get("record_type") == "Artifact" for row in records)
        receipt_data["passage_count"] = sum(row.get("record_type") == "Passage" for row in records)
        receipt_data["ledger_path"] = ledger_path.relative_to(run_dir).as_posix()
        receipt_data["ledger_sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest()

        fresh_rows = _passage_rows_from_ledger(records)
        if previous_passages_path is not None:
            previous_rows, previous_hash = _read_passage_jsonl(previous_passages_path)
            receipt_data["previous_passages_sha256"] = previous_hash
        else:
            previous_rows = []
        receipt_data["previous_passages_count"] = len(previous_rows)
        merged_rows = _merge_passage_rows(previous_rows, fresh_rows)
        _write_jsonl_new(merged_file, merged_rows)
        merged_path = merged_file
        receipt_data["merged_passages_path"] = merged_file.relative_to(run_dir).as_posix()
        receipt_data["merged_passages_sha256"] = hashlib.sha256(merged_file.read_bytes()).hexdigest()
        receipt_data["merged_passages_count"] = len(merged_rows)

        if time_error is not None:
            receipt_data.update(status="failed", reason=time_error)
        elif time_mismatch:
            receipt_data.update(status="failed", reason="time_scope_contract_mismatch")
        elif not records or (receipt_data["artifact_count"] == 0 and receipt_data["passage_count"] == 0):
            receipt_data.update(status="failed", reason="source_ingest_no_records")
        elif (receipt_data["passage_count"] == receipt_data["target_count"]
              and all(key in {"ingested", "already_present"} for key in receipt_data["bridge_counts"])):
            receipt_data.update(status="complete", reason="all_fresh_passages_ingested")
        else:
            receipt_data.update(status="partial", reason="artifact_only_or_partial_ingest")
    except BridgeIntegrationError as exc:
        receipt_data.update(status="failed", reason=exc.reason)
        if ledger_path.is_file():
            receipt_data["ledger_path"] = ledger_path.relative_to(run_dir).as_posix()
            receipt_data["ledger_sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    except Exception as exc:
        receipt_data.update(status="failed", reason="source_ingest_failed",
                            error_type=type(exc).__name__[:80])
        if ledger_path.is_file():
            receipt_data["ledger_path"] = ledger_path.relative_to(run_dir).as_posix()
            receipt_data["ledger_sha256"] = hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    return receipt_data, merged_path


def _read_local_stage_receipts(path):
    """Read the fixed offline runner's bounded per-stage receipts."""
    try:
        lines = _read_bounded_file(path, 1_000_000).decode("utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
    except (UnicodeError, ValueError) as exc:
        raise OrchestratorError("local stage receipts are invalid JSONL") from exc
    output = {}
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("task_id"), str)
                or row["task_id"] in output or type(row.get("executed")) is not bool
                or not isinstance(row.get("attempted_at_utc"), str)
                or not isinstance(row.get("reason"), str)
                or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", row["reason"])):
            raise OrchestratorError("local stage receipt identities are invalid")
        try:
            value = row["attempted_at_utc"]
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if not value.endswith(("Z", "+00:00")) or moment.utcoffset().total_seconds() != 0:
                raise ValueError("timestamp is not UTC")
        except (ValueError, AttributeError) as exc:
            raise OrchestratorError("local stage receipt timestamp is invalid") from exc
        output[row["task_id"]] = row
    return output


def _write_json(path, value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(encoded)
    temporary.replace(path)


def _row_status(row):
    status = row.get("status") or 0
    if status in (401, 403, 429) or row.get("error") == "access_challenge":
        return "blocked"
    if status in (404, 410):
        return "not_found"
    if row.get("truncated"):
        return "partial"
    if row.get("error") or not 200 <= status < 300:
        return "failed"
    return "complete"


def _overall(statuses):
    if not statuses:
        return "failed"
    if len(set(statuses)) == 1:
        return statuses[0]
    return "partial"


def run_incident(spec_path, run_dir, *, audit_path, git_commit, execute=False,
                 timeout=20, pause=1, max_bytes=2_000_000, max_targets=25,
                 run_local_analysis=False, typed_input_manifest_path=None):
    """Create one new bounded run. Existing directories are always refused.

    Resource limits are per target, with at most min(100, max_targets,
    IncidentSpec.budgets.max_new_sources_per_run) targets. No resume is offered.
    """
    spec = _read_json(spec_path, 1_000_000)
    try:
        validate_incident_spec(spec)
    except ContractValidationError as exc:
        # Schema error text may embed supplied values. Do not log it.
        raise OrchestratorError("IncidentSpec validation failed") from exc
    if not isinstance(git_commit, str) or not re.fullmatch(r"[a-f0-9]{7,40}", git_commit):
        raise OrchestratorError("git_commit must identify the producer revision (7..40 lowercase hex)")
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
            or not 1 <= timeout <= 60 or isinstance(pause, bool) or not isinstance(pause, (int, float))
            or not math.isfinite(pause) or not 0 <= pause <= 60
            or type(max_bytes) is not int or not 1024 <= max_bytes <= 10_000_000
            or type(max_targets) is not int or not 1 <= max_targets <= 100 or type(execute) is not bool
            or type(run_local_analysis) is not bool):
        raise OrchestratorError("resource bounds: timeout 1..60, pause 0..60, bytes 1024..10000000, targets 1..100")
    folder = _path(run_dir)
    if folder.exists():
        raise OrchestratorError("run directory already exists; use a new run directory")
    folder.parent.mkdir(parents=True, exist_ok=True)
    try:
        folder.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as exc:
        raise OrchestratorError("run directory already exists") from exc

    manifest = {
        "schema_version": "1.0", "run_id": folder.name, "incident_id": spec["incident_id"],
        "state": "created", "started_at_utc": _now(), "finished_at_utc": None,
        "producer": "social.incident_orchestrator/1.0", "git_commit": git_commit,
        "policy_version": "source-watch-and-explicit-offline-v1" if run_local_analysis else "source-watch-only-v1",
        "model_config": {"enabled": False},
        "source_snapshots": [], "task_receipts": [], "outputs": [], "limitations": list(_LIMITATIONS),
    }
    coverage = {"schema_version": "1.0", "run_id": folder.name,
                "mode": "execute" if execute else "dry_run", "platforms": [], "targets": [],
                "deferred_target_count": 0, "seed_urls_not_dispatched": len(spec["seed_urls"]),
                "limits": {"timeout_seconds": timeout, "pause_seconds": pause,
                           "max_bytes_per_target": max_bytes, "max_targets": max_targets,
                           "incident_source_budget": spec["budgets"]["max_new_sources_per_run"]}}
    receipts_path = folder / "task_receipts.jsonl"
    unresolved_path = folder / "unresolved_queue.jsonl"
    receipts_path.touch(exist_ok=False)
    unresolved_path.touch(exist_ok=False)

    def save_manifest():
        validate_run_manifest(manifest)
        _write_json(folder / "run_manifest.json", manifest)

    def receipt(task_id, stage, status, reason, *, executed=False, **extra):
        row = {"task_id": task_id, "stage": stage, "status": status, "reason": reason,
               "executed": executed, "attempted_at_utc": _now(), **extra}
        with receipts_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n")
        manifest["task_receipts"].append({"task_id": task_id,
            "status": status if status in _OUTCOMES else "blocked",
            "attempted_at_utc": row["attempted_at_utc"],
            "detail_ref": "task_receipts.jsonl#" + task_id})
        if status != "complete":
            with unresolved_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"task_id": task_id, "status": status, "reason": reason},
                                        sort_keys=True) + "\n")

    def transition(state):
        manifest["state"] = state
        save_manifest()

    receipt("run_created", "created", "complete", "new_run_directory")
    save_manifest()
    for platform in spec["platforms"]:
        available = platform in _ADAPTERS
        coverage["platforms"].append({"platform": platform,
            "adapter": "source_watch" if available else None,
            "status": "planned" if available else "not_run",
            "reason": "fixed_public_watchlist_only" if available else "unsupported_adapter"})
        if not available:
            receipt("coverage:" + platform, "collecting", "not_run", "unsupported_adapter", platform=platform)
    _write_json(folder / "coverage_plan.json", coverage)
    transition("collecting")
    failed = False
    web = next((row for row in coverage["platforms"] if row["platform"] == "web"), None)
    fresh_manifest_path = None
    try:
        if web is None:
            receipt("source_watch", "collecting", "not_run", "web_not_requested")
        elif "public_web" not in spec["collection_policy"]["access_methods"]:
            web.update(status="not_run", reason="public_web_not_authorized")
            receipt("source_watch", "collecting", "not_run", "public_web_not_authorized")
        elif spec["budgets"]["max_new_sources_per_run"] == 0:
            web.update(status="not_run", reason="source_budget_zero")
            receipt("source_watch", "collecting", "not_run", "source_budget_zero")
        else:
            audit = _path(audit_path)
            _read_json(audit, 10_000_000)
            collector = _ADAPTERS["web"]
            plan = collector.build_plan(audit)
            limit = min(max_targets, spec["budgets"]["max_new_sources_per_run"])
            selected = plan[:limit]
            coverage["targets"] = selected
            coverage["deferred_target_count"] = len(plan) - len(selected)
            if len(selected) < len(plan):
                receipt("source_watch:budget", "collecting", "not_run", "target_budget_exhausted",
                        deferred_target_count=len(plan) - len(selected))
            _write_json(folder / "coverage_plan.json", coverage)
            if not execute:
                receipt("source_watch", "collecting", "planned", "execute_flag_required",
                        target_count=len(selected))
            else:
                summary = collector.run(selected, raw_root=folder / "raw/source_watch",
                    report_dir=folder / "reports/source_watch", dry_run=False,
                    timeout=timeout, pause=pause, max_bytes=max_bytes)
                archive = _path(summary["manifest"])
                relative = archive.relative_to(folder).as_posix()
                snapshot_id = summary.get("snapshot")
                expected_archive = folder / "raw/source_watch" / str(snapshot_id) / "manifest.json"
                if (not isinstance(snapshot_id, str) or not snapshot_id
                        or archive != _path(expected_archive)):
                    raise OrchestratorError("collector did not return this run's exact Source Watch manifest")
                archive_summary = _read_json(archive, 10_000_000)
                if archive_summary.get("snapshot") != snapshot_id or not isinstance(archive_summary.get("rows"), list):
                    raise OrchestratorError("collector manifest does not match this run's snapshot")
                fresh_manifest_path = archive
                statuses = []
                for index, row in enumerate(summary["rows"]):
                    status = _row_status(row)
                    statuses.append(status)
                    receipt(f"source_watch:target:{index}", "collecting", status,
                            "public_fetch_" + status, executed=True, http_status=row.get("status"),
                            archive_manifest=relative)
                outcome = _overall(statuses)
                manifest["source_snapshots"].append({"snapshot_id": summary["snapshot"],
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "access_status": outcome, "platform": "web", "fetched_at_utc": _now(),
                    "snapshot_artifact_path": relative})
                receipt("source_watch", "collecting", outcome, "collection_" + outcome, executed=True,
                        archive_manifest=relative, target_count=len(selected))
                web.update(status=outcome, reason="collection_" + outcome)
                failed = outcome != "complete"
    except Exception as exc:
        # A collector may raise after saving partial raw bodies. Preserve them and
        # write a bounded diagnostic; never echo an arbitrary exception payload.
        failed = True
        if web is not None:
            web.update(status="failed", reason="adapter_exception")
        receipt("source_watch", "collecting", "failed", "adapter_exception", executed=execute,
                error_type=type(exc).__name__[:80])
    _write_json(folder / "coverage_plan.json", coverage)
    collection_failed = failed

    local_inputs = None
    typed_manifest_sha256 = None
    typed_manifest_error = None
    if run_local_analysis and typed_input_manifest_path is not None:
        try:
            local_inputs, typed_manifest_sha256 = _read_typed_inputs(typed_input_manifest_path)
        except Exception as exc:
            typed_manifest_error = exc

    source_ingest_receipt = _empty_source_ingest_receipt()
    merged_passages_path = None
    if fresh_manifest_path is not None:
        previous_passages_path = (local_inputs or {}).get("passages")
        try:
            source_ingest_receipt, merged_passages_path = _build_source_ingest_receipt(
                fresh_manifest_path, folder, as_of_utc=spec["as_of_utc"],
                previous_passages_path=previous_passages_path,
            )
        except Exception as exc:
            source_ingest_receipt.update(status="failed", reason="source_ingest_failed",
                                         error_type=type(exc).__name__[:80])
    source_ingest_failed = source_ingest_receipt["status"] == "failed"
    time_scope_mismatch = source_ingest_receipt["reason"] == "time_scope_contract_mismatch"
    _write_json(folder / "source_ingest_receipt.json", source_ingest_receipt)
    receipt("source_ingest_bridge", "collecting", source_ingest_receipt["status"],
            source_ingest_receipt["reason"], executed=fresh_manifest_path is not None,
            detail_path="source_ingest_receipt.json")

    # The local runner consumes caller-typed files and eligible collector
    # passages; it does not normalize raw sources or perform evidence adjudication.
    # Fresh collector passages are passed through a retained, run-local merge.
    local_summary = None
    local_failed = False
    query_budget_zero = False
    if run_local_analysis:
        if typed_input_manifest_path is None:
            local_failed = True
            receipt("local_analysis", "local_analysis", "failed", "typed_input_manifest_missing")
        elif typed_manifest_error is not None:
            local_failed = True
            receipt("local_analysis", "local_analysis", "failed", "typed_input_manifest_invalid",
                    executed=False, error_type=type(typed_manifest_error).__name__[:80])
        elif source_ingest_receipt["reason"] == "passage_id_conflict":
            local_failed = True
            receipt("local_analysis", "local_analysis", "failed", source_ingest_receipt["reason"],
                    executed=False, typed_input_manifest_sha256=typed_manifest_sha256,
                    detail_path="source_ingest_receipt.json")
        else:
            local_started = True
            try:
                runner_inputs = dict(local_inputs)
                if merged_passages_path is not None:
                    runner_inputs["passages"] = merged_passages_path
                result = run_offline_case(spec, folder / "analysis", git_commit=git_commit,
                                          inputs=runner_inputs, execute=True)
                local_summary = _read_json(folder / "analysis/case_summary.json", 1_000_000)
                if result.get("analysis_status") not in {"complete", "partial", "failed"}:
                    raise OrchestratorError("local runner returned an invalid status")
                if local_summary.get("analysis_status") != result["analysis_status"]:
                    raise OrchestratorError("local runner status mismatch")
                stage_status = local_summary.get("stage_status")
                local_stages = ("source_lineage", "claim_retrieval", "event_reconstruction", "conflict_analysis",
                                "information_dynamics", "uncertainty_scenarios", "report_generation", "evaluation")
                if (not isinstance(stage_status, dict) or set(stage_status) != set(local_stages)
                        or any(status not in _OUTCOMES | {"not_run"} for status in stage_status.values())):
                    raise OrchestratorError("local runner stage statuses are invalid")
                inner_receipts = _read_local_stage_receipts(folder / "analysis/task_receipts.jsonl")
                if (set(inner_receipts) != set(local_stages)
                        or any(inner_receipts[name].get("status") != stage_status[name] for name in local_stages)
                        or any(stage_status[name] == "not_run" and inner_receipts[name]["executed"]
                               for name in local_stages)):
                    raise OrchestratorError("local runner receipt statuses are inconsistent")
                derived_status = ("failed" if "failed" in stage_status.values() else
                                  "complete" if all(status == "complete" for status in stage_status.values()) else
                                  "partial")
                if result["analysis_status"] != derived_status:
                    raise OrchestratorError("local runner aggregate status is inconsistent")
                query_budget_zero = (stage_status["claim_retrieval"] == "not_run"
                                     and inner_receipts["claim_retrieval"].get("reason") == "query_budget_zero")
                for name in local_stages:
                    status = stage_status[name]
                    receipt("local:" + name, "local_analysis", status, inner_receipts[name]["reason"],
                            executed=inner_receipts[name]["executed"],
                            attempted_at_utc=inner_receipts[name]["attempted_at_utc"],
                            detail_path="analysis/task_receipts.jsonl#" + name)
                local_failed = result["analysis_status"] == "failed" or time_scope_mismatch
                receipt("local_analysis", "local_analysis",
                        "failed" if time_scope_mismatch else result["analysis_status"],
                        "time_scope_contract_mismatch" if time_scope_mismatch else
                        "local_typed_pipeline_semantic_accuracy_not_evaluated", executed=True,
                        detail_path="analysis/case_summary.json", unresolved_path="analysis/unresolved_queue.jsonl",
                        typed_input_manifest_sha256=typed_manifest_sha256)
            except Exception as exc:
                local_failed = True
                local_summary = None
                receipt("local_analysis", "local_analysis", "failed", "typed_local_analysis_failed",
                        executed=local_started, error_type=type(exc).__name__[:80],
                        **({"typed_input_manifest_sha256": typed_manifest_sha256} if typed_manifest_sha256 else {}))
    else:
        receipt("local_analysis", "local_analysis", "not_run", "local_analysis_not_requested")

    statuses = local_summary.get("stage_status", {}) if local_summary else {}
    for stage in _STAGES:
        if not collection_failed and not source_ingest_failed and not local_failed:
            transition(stage)
        if local_failed and local_summary is None:
            status, reason = "not_run", "local_analysis_failed"
        elif local_summary is None:
            status, reason = "not_run", "upstream_failed" if failed else "local_analysis_not_requested"
        elif stage in {"normalized", "extracting"}:
            status, reason = "not_run", "pretyped_inputs_no_raw_normalization_or_extraction"
        elif stage == "verifying":
            if time_scope_mismatch:
                status, reason = "failed", "time_scope_contract_mismatch"
            else:
                inner = statuses["claim_retrieval"]
                status = inner if inner in {"failed", "blocked", "not_found"} else "partial" if inner in {"complete", "partial"} else "not_run"
                reason = ("query_budget_zero" if query_budget_zero else
                          "retrieval_candidates_only_no_evidence_adjudication" if status == "partial" else "local_claim_retrieval_" + inner)
        elif stage == "reconstructing":
            inner = {statuses["event_reconstruction"], statuses["conflict_analysis"]}
            status = "failed" if "failed" in inner else "partial" if inner & {"complete", "partial"} else "not_run"
            reason = "typed_event_or_conflict_processing_not_semantic_validation" if status == "partial" else "local_event_and_conflict_processing_" + status
        else:
            inner = statuses["report_generation"]
            status = inner if inner in _OUTCOMES else "not_run"
            reason = "typed_report_structural_gates_only" if status == "complete" else "local_report_generation_" + status
        receipt(stage, stage, status, reason, executed=status in {"complete", "partial", "failed"})
    failed = collection_failed or source_ingest_failed or local_failed
    failure_reason = ("collection_and_local_analysis_failed" if collection_failed and local_failed else
                      "collection_failed" if collection_failed else
                      "source_ingest_and_local_analysis_failed" if source_ingest_failed and local_failed else
                      "source_ingest_failed" if source_ingest_failed else
                      "local_analysis_failed" if local_failed else "bounded_controller_finished")
    receipt("run_finished", "failed_with_receipt" if failed else "completed_bounded", "failed" if failed else "complete",
            failure_reason)
    manifest["finished_at_utc"] = _now()
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path != folder / "run_manifest.json":
            _path(path)
            manifest["outputs"].append({"path": path.relative_to(folder).as_posix(),
                                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    transition("failed_with_receipt" if failed else "completed_bounded")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="validated IncidentSpec JSON")
    parser.add_argument("--run-dir", type=Path, required=True, help="new, exclusive run directory")
    parser.add_argument("--audit", type=Path, required=True, help="public source-watch URL audit JSON")
    parser.add_argument("--git-commit", required=True, help="producer revision (7..40 lowercase hex)")
    parser.add_argument("--execute", action="store_true", help="perform bounded read-only public collection")
    parser.add_argument("--run-local-analysis", action="store_true", help="execute deterministic typed-input analysis without enabling collection")
    parser.add_argument("--typed-input-manifest", type=Path, help="JSON object mapping approved offline input names to local paths")
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--pause", type=float, default=1)
    parser.add_argument("--max-bytes", type=int, default=2_000_000)
    parser.add_argument("--max-targets", type=int, default=25)
    args = parser.parse_args(argv)
    try:
        manifest = run_incident(args.spec, args.run_dir, audit_path=args.audit, git_commit=args.git_commit,
            execute=args.execute, timeout=args.timeout, pause=args.pause,
            max_bytes=args.max_bytes, max_targets=args.max_targets,
            run_local_analysis=args.run_local_analysis, typed_input_manifest_path=args.typed_input_manifest)
    except (OrchestratorError, OSError) as exc:
        print("incident-orchestrator: " + (str(exc) if isinstance(exc, OrchestratorError) else type(exc).__name__),
              file=sys.stderr)
        return 2
    print(json.dumps({"run_id": manifest["run_id"], "state": manifest["state"], "run_dir": str(args.run_dir)},
                     sort_keys=True))
    return 1 if manifest["state"] == "failed_with_receipt" else 0


if __name__ == "__main__":
    raise SystemExit(main())
