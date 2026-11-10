"""Bounded local integration of already collected Social evidence artifacts.

This runner performs no collection, network access, model inference, or truth
attribution. Inputs must already match the typed contracts of the component
being invoked. Incompatible or absent stages are recorded as not_run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.claim_verification import aggregate_verification, retrieve_evidence_candidates
from src.analysis.conflict_analysis import analyze_conflict_episode
from src.analysis.event_reconstruction import reconstruct_timeline
from src.analysis.evaluation_harness import evaluate_run_bundle, run_frozen_suite, verify_report_bundle
from src.analysis.information_dynamics import summarize_information_dynamics
from src.analysis.report_generator import build_report_bundle
from src.analysis.source_lineage import analyze_source_lineage
from src.analysis.uncertainty_scenarios import analyze_uncertainty_scenarios
from src.contracts import ContractValidationError, validate_incident_spec, validate_run_manifest

_INPUTS = {
    "claims": "jsonl", "passages": "jsonl", "event_mentions": "jsonl",
    "lineage_records": "jsonl", "information_posts": "jsonl",
    "uncertainty_records": "jsonl", "coverage": "json",
    "conflict_input": "json", "report_bundle": "json",
}
_MAX_FILE_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_INPUT_BYTES = 512 * 1024 * 1024
_MAX_RECORDS_PER_FILE = 200_000
_MAX_LINE_BYTES = 4 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_AS_OF_TIME_FIELDS = {
    "as_of_utc", "captured_at", "captured_at_utc", "created_at_utc", "fetched_at",
    "fetched_at_utc", "observed_at", "observed_at_utc", "publication_time",
    "publication_time_utc", "published_at_utc", "queried_at", "view_count_observed_at",
}


class OfflineRunError(ValueError):
    """Invalid input, unsafe path, or occupied output directory."""


class TimeScopeError(ValueError):
    """Supplied timestamp is malformed or outside the IncidentSpec's as-of/window."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_path(value: str | os.PathLike[str], *, must_exist: bool) -> Path:
    raw = Path(value).expanduser()
    if ".." in raw.parts:
        raise OfflineRunError("path traversal is not allowed")
    path = raw.absolute()
    for component in (path, *path.parents):
        if component.is_symlink():
            raise OfflineRunError("symlink path components are not allowed")
    if must_exist and not path.is_file():
        raise OfflineRunError("input path must be a regular file")
    return path


def _read_bounded(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OfflineRunError("input could not be opened safely") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FILE_BYTES:
            raise OfflineRunError("input must be a bounded regular file")
        chunks = []
        remaining = _MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_FILE_BYTES or len(data) != info.st_size:
            raise OfflineRunError("input size changed or exceeds the configured limit")
        return data
    finally:
        os.close(descriptor)


def _decode_json(data: bytes) -> dict:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineRunError("JSON input is invalid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise OfflineRunError("JSON input must contain an object")
    return payload


def _decode_jsonl(data: bytes) -> list[dict]:
    try:
        text = data.decode("utf-8")
    except UnicodeError as exc:
        raise OfflineRunError("JSONL input is not UTF-8") from exc
    rows = []
    for line_number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > _MAX_LINE_BYTES:
            raise OfflineRunError("JSONL record exceeds the configured line limit")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise OfflineRunError(f"JSONL contains an invalid record at line {line_number}") from exc
        if not isinstance(row, dict):
            raise OfflineRunError(f"JSONL record at line {line_number} must be an object")
        rows.append(row)
        if len(rows) > _MAX_RECORDS_PER_FILE:
            raise OfflineRunError("JSONL exceeds the configured record-count limit")
    return rows


def _write_bytes_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def _write_json_new(path: Path, value: Any) -> None:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    _write_bytes_new(path, content)


def _write_jsonl_new(path: Path, rows: list[dict]) -> None:
    content = b"".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n" for row in rows)
    _write_bytes_new(path, content)


def _replace_jsonl(path: Path, rows: list[dict]) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    content = b"".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8") + b"\n" for row in rows)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _replace_json(path: Path, value: Any) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_inputs(inputs: dict[str, str | os.PathLike[str]]) -> tuple[dict, dict]:
    if not isinstance(inputs, dict) or set(inputs) - set(_INPUTS):
        unknown = sorted(set(inputs) - set(_INPUTS)) if isinstance(inputs, dict) else []
        raise OfflineRunError("unsupported input names: " + ", ".join(unknown))
    total_bytes = 0
    payloads: dict[str, Any] = {}
    metadata = {}
    for name in sorted(inputs):
        path = _safe_path(inputs[name], must_exist=True)
        try:
            data = _read_bounded(path)
        except OfflineRunError:
            raise
        total_bytes += len(data)
        if total_bytes > _MAX_TOTAL_INPUT_BYTES:
            raise OfflineRunError("combined input size exceeds the configured limit")
        kind = _INPUTS[name]
        payload = _decode_json(data) if kind == "json" else _decode_jsonl(data)
        payloads[name] = payload
        metadata[name] = {
            "file_name": path.name,
            "input_type": kind,
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data),
            "record_count": len(payload) if kind == "jsonl" else None,
        }
    return payloads, metadata


def _parse_scoped_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise TimeScopeError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TimeScopeError(f"{field} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TimeScopeError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _check_as_of(payload: Any, *, as_of_utc: str, stage: str) -> None:
    """Reject any supplied source/capture timestamp later than the run's as-of cutoff."""
    cutoff = _parse_scoped_time(as_of_utc, "IncidentSpec.as_of_utc")

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                if key in _AS_OF_TIME_FIELDS and child not in (None, ""):
                    observed = _parse_scoped_time(child, f"{stage}.{child_path}")
                    if observed > cutoff:
                        raise TimeScopeError("timestamp exceeds IncidentSpec.as_of_utc")
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, f"{path}[{index}]")

    visit(payload, stage)


def _check_window_point(value: Any, *, start: datetime, end: datetime, field: str) -> None:
    if value in (None, ""):
        return
    moment = _parse_scoped_time(value, field)
    if moment < start or moment > end:
        raise TimeScopeError("timestamp falls outside IncidentSpec.time_window_utc")


def _check_window_interval(earliest: Any, latest: Any, *, start: datetime, end: datetime, field: str) -> None:
    first = _parse_scoped_time(earliest, f"{field}.earliest") if earliest not in (None, "") else None
    last = _parse_scoped_time(latest, f"{field}.latest") if latest not in (None, "") else None
    if first is not None and last is not None and first > last:
        raise TimeScopeError("time interval starts after it ends")
    if first is not None and first > end or last is not None and last < start:
        raise TimeScopeError("time interval does not overlap IncidentSpec.time_window_utc")


def _check_incident_window(input_name: str, payload: Any, incident_spec: dict) -> None:
    """Validate event/publication times for records used as in-scope observations."""
    start = _parse_scoped_time(incident_spec["time_window_utc"][0], "IncidentSpec.time_window_utc.start")
    end = _parse_scoped_time(incident_spec["time_window_utc"][1], "IncidentSpec.time_window_utc.end")
    if input_name == "information_posts":
        for index, row in enumerate(payload):
            _check_window_point(row.get("publication_time"), start=start, end=end,
                                field=f"information_posts[{index}].publication_time")
    elif input_name == "event_mentions":
        for index, row in enumerate(payload):
            if row.get("event_kind") == "communication":
                _check_window_point(row.get("publication_time_utc"), start=start, end=end,
                                    field=f"event_mentions[{index}].publication_time_utc")
            else:
                interval = row.get("time") or {}
                if isinstance(interval, dict):
                    _check_window_interval(interval.get("earliest_utc"), interval.get("latest_utc"),
                                           start=start, end=end,
                                           field=f"event_mentions[{index}].event_time")
    elif input_name == "conflict_input" and isinstance(payload, dict):
        for index, row in enumerate(payload.get("physical_event_records", [])):
            _check_window_point(row.get("time_utc"), start=start, end=end,
                                field=f"conflict_input.physical_event_records[{index}].time_utc")
        for index, row in enumerate(payload.get("position_records", [])):
            _check_window_point(row.get("publication_time_utc"), start=start, end=end,
                                field=f"conflict_input.position_records[{index}].publication_time_utc")
        for index, row in enumerate(payload.get("information_attention_records", [])):
            _check_window_interval(row.get("interval_start_utc"), row.get("interval_end_utc"),
                                   start=start, end=end,
                                   field=f"conflict_input.information_attention_records[{index}]")
    elif input_name == "uncertainty_records":
        for index, row in enumerate(payload):
            value = row.get("publication_time") or row.get("publication_time_utc")
            _check_window_point(value, start=start, end=end,
                                field=f"uncertainty_records[{index}].publication_time")


def _receipt(task_id: str, status: str, reason: str, detail_ref: str, *, error_type: str | None = None) -> dict:
    row = {"task_id": task_id, "stage": task_id, "status": status, "reason": reason,
           "attempted_at_utc": _now(), "executed": status not in {"not_run", "planned"}}
    if error_type:
        row["error_type"] = error_type
    row["detail_ref"] = detail_ref
    return row


def run_offline_case(incident_spec: dict, run_dir: str | os.PathLike[str], *, git_commit: str,
                     inputs: dict[str, str | os.PathLike[str]], execute: bool = False,
                     uncertainty_target_label: str = "target") -> dict:
    """Create a private run over supplied local artifacts.

    Dry-run is the default. ``execute=True`` invokes only deterministic local
    processors. It does not fetch sources or call an AI provider.
    """
    try:
        validate_incident_spec(incident_spec)
    except (ContractValidationError, TypeError, KeyError) as exc:
        raise OfflineRunError("IncidentSpec validation failed") from exc
    if not isinstance(git_commit, str) or not re.fullmatch(r"[a-f0-9]{7,40}", git_commit):
        raise OfflineRunError("git_commit must be 7..40 lowercase hexadecimal characters")
    if type(execute) is not bool:
        raise OfflineRunError("execute must be boolean")
    if not isinstance(uncertainty_target_label, str) or not uncertainty_target_label.strip():
        raise OfflineRunError("uncertainty_target_label must be a non-empty string")
    folder = _safe_path(run_dir, must_exist=False)
    if folder.exists():
        raise FileExistsError("run directory already exists; select a new output directory")
    payloads, input_metadata = _load_inputs(inputs)
    try:
        folder.parent.mkdir(parents=True, exist_ok=True)
        folder.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError("run directory already exists; select a new output directory") from exc
    os.chmod(folder, 0o700)

    run_id = folder.name
    started = _now()
    task_rows: list[dict] = []
    unresolved: list[dict] = []
    stage_status: dict[str, str] = {}
    outputs: list[dict] = []
    limitations = [
        "Only supplied local artifacts are analyzed; no source collection or historical retrieval is performed.",
        "Retrieval outputs are candidates. No model or rule adjudication is performed by this runner.",
        "Missing or incompatible stages remain not_run and are listed in unresolved_queue.jsonl.",
        "Semantic truth, source independence, and incident completeness are not established by structural checks.",
    ]

    def record(stage: str, status: str, reason: str, error: Exception | None = None):
        stage_status[stage] = status
        task_rows.append(_receipt(stage, status, reason, "task_receipts.jsonl#" + stage,
                                  error_type=type(error).__name__ if error else None))

    _write_json_new(folder / "input_manifest.json", {
        "schema_version": "1.0", "incident_id": incident_spec["incident_id"],
        "run_id": run_id, "inputs": input_metadata,
        "combined_bytes": sum(row["byte_size"] for row in input_metadata.values()),
    })

    if not execute:
        for stage in ("source_lineage", "claim_retrieval", "event_reconstruction", "conflict_analysis",
                      "information_dynamics", "uncertainty_scenarios", "report_generation"):
            record(stage, "not_run", "dry_run_only")
            unresolved.append({"item_id": stage, "stage": stage, "status": "not_run", "reason": "dry_run_only"})
        try:
            evaluation = run_frozen_suite()
        except Exception as exc:
            evaluation = {"schema_version": "1.0", "status": "failed",
                          "hard_gate_status": "failed", "error_type": type(exc).__name__}
        _write_json_new(folder / "evaluation.json", evaluation)
        evaluation_status = "complete" if evaluation.get("status") == "passed" else "failed"
        record("evaluation", evaluation_status,
               "frozen_synthetic_invariant_suite_passed" if evaluation_status == "complete"
               else "frozen_synthetic_invariant_suite_failed")
        if evaluation_status != "complete":
            unresolved.append({"item_id": "release_evaluation", "stage": "evaluation",
                               "status": "failed", "reason": "frozen_synthetic_invariant_suite_failed"})
        analysis_status = "not_run" if evaluation_status == "complete" else "failed"
    else:
        lineage = None
        if "lineage_records" in payloads:
            try:
                _check_as_of(payloads["lineage_records"], as_of_utc=incident_spec["as_of_utc"], stage="lineage_records")
                lineage = analyze_source_lineage(payloads["lineage_records"])
                _write_json_new(folder / "source_lineage.json", lineage)
                record("source_lineage", "complete", "typed_evidence_lineage_records")
            except Exception as exc:
                reason = "time_scope_contract_mismatch" if isinstance(exc, TimeScopeError) else "lineage_input_not_accepted"
                record("source_lineage", "failed", reason, exc)
                unresolved.append({"item_id": "source_lineage", "stage": "source_lineage",
                                   "status": "failed", "reason": reason})
        else:
            record("source_lineage", "not_run", "lineage_records_not_supplied")

        claims, passages = payloads.get("claims"), payloads.get("passages")
        if claims is not None and passages is not None and incident_spec["budgets"]["max_queries_per_claim"] == 0:
            _write_jsonl_new(folder / "evidence_candidates.jsonl", [])
            _write_jsonl_new(folder / "verification_results.jsonl", [])
            record("claim_retrieval", "not_run", "query_budget_zero")
            unresolved.append({"item_id": "claim_retrieval", "stage": "claim_retrieval",
                               "status": "not_run", "reason": "query_budget_zero"})
        elif claims is not None and passages is not None:
            try:
                _check_as_of(claims, as_of_utc=incident_spec["as_of_utc"], stage="claims")
                _check_as_of(passages, as_of_utc=incident_spec["as_of_utc"], stage="passages")
                if len({row.get("claim_id") for row in claims}) != len(claims):
                    raise ValueError("duplicate claim_id")
                retrieval_rows = []
                verification_rows = []
                retrieval_issues = []
                for claim in claims:
                    result = retrieve_evidence_candidates(
                        claim, passages, as_of_utc=incident_spec["as_of_utc"],
                        top_k=20,
                        max_queries=incident_spec["budgets"]["max_queries_per_claim"],
                    )
                    retrieval_rows.append(result)
                    scope = claim.get("scope")
                    if isinstance(scope, dict) and scope:
                        verification_rows.append(aggregate_verification(
                            claim, [], as_of_utc=incident_spec["as_of_utc"], search_complete=False,
                        ))
                        unresolved.append({"item_id": claim["claim_id"], "claim_id": claim["claim_id"],
                                           "stage": "claim_retrieval", "status": "unverified",
                                           "reason": "retrieval_candidates_require_adjudication"})
                    else:
                        retrieval_issues.append(claim.get("claim_id"))
                        unresolved.append({"item_id": str(claim.get("claim_id") or "unknown_claim"),
                                           "claim_id": claim.get("claim_id"), "stage": "claim_retrieval",
                                           "status": "not_run", "reason": "claim_scope_required_for_verification_projection"})
                _write_jsonl_new(folder / "evidence_candidates.jsonl", retrieval_rows)
                _write_jsonl_new(folder / "verification_results.jsonl", verification_rows)
                record("claim_retrieval", "partial" if retrieval_issues else "complete",
                       "candidates_only_no_evidence_assessments" if claims else "empty_claim_list")
            except Exception as exc:
                reason = "time_scope_contract_mismatch" if isinstance(exc, TimeScopeError) else "claims_or_passages_contract_mismatch"
                record("claim_retrieval", "failed", reason, exc)
                unresolved.append({"item_id": "claim_retrieval", "stage": "claim_retrieval",
                                   "status": "failed", "reason": reason})
        elif claims is not None or passages is not None:
            record("claim_retrieval", "not_run", "both_claims_and_passages_are_required")
            unresolved.append({"item_id": "claim_retrieval", "stage": "claim_retrieval",
                               "status": "not_run", "reason": "both_claims_and_passages_are_required"})
        else:
            record("claim_retrieval", "not_run", "claims_and_passages_not_supplied")

        reconstruction = None
        if "event_mentions" in payloads:
            try:
                _check_as_of(payloads["event_mentions"], as_of_utc=incident_spec["as_of_utc"], stage="event_mentions")
                _check_incident_window("event_mentions", payloads["event_mentions"], incident_spec)
                reconstruction = reconstruct_timeline(payloads["event_mentions"], run_context={
                    "incident_id": incident_spec["incident_id"], "run_id": run_id,
                    "git_commit": git_commit, "as_of_utc": incident_spec["as_of_utc"],
                })
                _write_json_new(folder / "event_reconstruction.json", reconstruction)
                record("event_reconstruction", "complete", "typed_event_mentions_only")
                for row in reconstruction.get("unresolved", []):
                    unresolved.append({"item_id": row.get("event_mention_id", "unknown"),
                                       "stage": "event_reconstruction", "status": "unresolved",
                                       "reason": row.get("reason", "event_mention_unresolved")})
            except Exception as exc:
                reason = "time_scope_contract_mismatch" if isinstance(exc, TimeScopeError) else "event_mention_contract_mismatch"
                record("event_reconstruction", "failed", reason, exc)
                unresolved.append({"item_id": "event_reconstruction", "stage": "event_reconstruction",
                                   "status": "failed", "reason": reason})
        else:
            record("event_reconstruction", "not_run", "event_mentions_not_supplied")

        if "conflict_input" in payloads:
            try:
                conflict_input = payloads["conflict_input"]
                _check_as_of(conflict_input, as_of_utc=incident_spec["as_of_utc"], stage="conflict_input")
                _check_incident_window("conflict_input", conflict_input, incident_spec)
                conflict = analyze_conflict_episode(
                    conflict_input.get("episode_id", run_id),
                    conflict_input.get("physical_event_records", []),
                    conflict_input.get("position_records", []),
                    conflict_input.get("information_attention_records", []),
                    context={"incident_id": incident_spec["incident_id"], "run_id": run_id},
                )
                _write_json_new(folder / "conflict_analysis.json", conflict)
                record("conflict_analysis", "complete", "explicit_conflict_records_only")
                for row in conflict.get("unresolved", []):
                    unresolved.append({"item_id": row.get("record_id", "unknown"),
                                       "stage": "conflict_analysis", "status": "unresolved",
                                       "reason": row.get("reason", "conflict_record_unresolved")})
            except Exception as exc:
                reason = "time_scope_contract_mismatch" if isinstance(exc, TimeScopeError) else "conflict_input_contract_mismatch"
                record("conflict_analysis", "failed", reason, exc)
                unresolved.append({"item_id": "conflict_analysis", "stage": "conflict_analysis",
                                   "status": "failed", "reason": reason})
        else:
            record("conflict_analysis", "not_run", "preclassified_conflict_input_not_supplied")

        if "information_posts" in payloads:
            try:
                _check_as_of(payloads["information_posts"], as_of_utc=incident_spec["as_of_utc"], stage="information_posts")
                if "coverage" in payloads:
                    _check_as_of(payloads["coverage"], as_of_utc=incident_spec["as_of_utc"], stage="coverage")
                _check_incident_window("information_posts", payloads["information_posts"], incident_spec)
                information = summarize_information_dynamics(
                    payloads["information_posts"], coverage=payloads.get("coverage"),
                    source_lineage=lineage,
                )
                _write_json_new(folder / "information_dynamics.json", information)
                record("information_dynamics", "complete", "descriptive_post_snapshot_summary")
            except Exception as exc:
                reason = "time_scope_contract_mismatch" if isinstance(exc, TimeScopeError) else "information_post_contract_mismatch"
                record("information_dynamics", "failed", reason, exc)
                unresolved.append({"item_id": "information_dynamics", "stage": "information_dynamics",
                                   "status": "failed", "reason": reason})
        else:
            record("information_dynamics", "not_run", "information_posts_not_supplied")

        if "uncertainty_records" in payloads:
            try:
                _check_as_of(payloads["uncertainty_records"], as_of_utc=incident_spec["as_of_utc"], stage="uncertainty_records")
                _check_incident_window("uncertainty_records", payloads["uncertainty_records"], incident_spec)
                uncertainty = analyze_uncertainty_scenarios(
                    payloads["uncertainty_records"], target_label=uncertainty_target_label,
                )
                _write_json_new(folder / "uncertainty_scenarios.json", uncertainty)
                record("uncertainty_scenarios", "complete", "explicit_label_and_group_scenarios")
            except Exception as exc:
                reason = "time_scope_contract_mismatch" if isinstance(exc, TimeScopeError) else "uncertainty_record_contract_mismatch"
                record("uncertainty_scenarios", "failed", reason, exc)
                unresolved.append({"item_id": "uncertainty_scenarios", "stage": "uncertainty_scenarios",
                                   "status": "failed", "reason": reason})
        else:
            record("uncertainty_scenarios", "not_run", "uncertainty_records_not_supplied")

        report_context = None
        report_payload = None
        report_result = None
        if "report_bundle" in payloads:
            report_payload = payloads["report_bundle"]
            report_context = {
                "incident_id": incident_spec["incident_id"], "run_id": run_id,
                "as_of_utc": incident_spec["as_of_utc"],
                "working_title": incident_spec["working_title"], "git_commit": git_commit,
                "scope": incident_spec["scope"],
                "coverage": payloads.get("coverage") or {"input_contract": "coverage_not_supplied"},
            }
            try:
                _check_as_of(report_payload, as_of_utc=incident_spec["as_of_utc"], stage="report_bundle")
                if "coverage" in payloads:
                    _check_as_of(payloads["coverage"], as_of_utc=incident_spec["as_of_utc"], stage="coverage")
                report_result = build_report_bundle(
                    report_payload.get("findings"), report_payload.get("evidence_records"),
                    report_payload.get("verification_results"), report_context,
                    folder / "report",
                )
                integrity = verify_report_bundle(folder / "report")
                _write_json_new(folder / "report_integrity.json", integrity)
                if integrity["status"] != "passed":
                    raise ValueError("report bundle output integrity check failed")
                record("report_generation", "complete", "typed_findings_passed_structural_citation_gates")
            except Exception as exc:
                reason = "time_scope_contract_mismatch" if isinstance(exc, TimeScopeError) else "report_input_rejected_or_integrity_failed"
                record("report_generation", "failed", reason, exc)
                unresolved.append({"item_id": "report_generation", "stage": "report_generation",
                                   "status": "failed", "reason": reason})
                report_payload = None
        else:
            record("report_generation", "not_run", "preverified_report_bundle_not_supplied")

        finished = _now()
        manifest = {
            "schema_version": "1.0", "run_id": run_id,
            "incident_id": incident_spec["incident_id"], "state": "completed_bounded",
            "started_at_utc": started, "finished_at_utc": finished,
            "producer": "social.analysis.offline_case_runner/1.0", "git_commit": git_commit,
            "policy_version": "offline-typed-inputs-v1", "model_config": {"enabled": False},
            "source_snapshots": [],
            "task_receipts": [{"task_id": row["task_id"],
                                "status": row["status"] if row["status"] in {"complete", "partial", "blocked", "not_found", "failed"} else "blocked",
                                "attempted_at_utc": row["attempted_at_utc"],
                                "detail_ref": row["detail_ref"]} for row in task_rows],
            "outputs": [], "limitations": limitations,
        }
        _write_jsonl_new(folder / "task_receipts.jsonl", task_rows)
        _write_jsonl_new(folder / "unresolved_queue.jsonl", unresolved)
        status_path = folder / "case_summary.json"
        active_statuses = list(stage_status.values())
        analysis_status = "failed" if "failed" in active_statuses else "partial" if any(value != "complete" for value in active_statuses) else "complete"
        summary = {"schema_version": "1.0", "run_id": run_id,
                   "incident_id": incident_spec["incident_id"], "analysis_status": analysis_status,
                   "stage_status": stage_status, "semantic_accuracy": "not_evaluated",
                   "claim_verification_policy": "retrieval_candidates_only_no_automatic_acceptance",
                   "limitations": limitations}
        _write_json_new(status_path, summary)

        manifest["outputs"] = _output_hashes(folder)
        try:
            validate_run_manifest(manifest)
        except ContractValidationError as exc:
            raise OfflineRunError("generated RunManifest failed contract validation") from exc
        _write_json_new(folder / "run_manifest.json", manifest)

        report_integrity = verify_report_bundle(folder / "report") if report_result else None
        try:
            evaluation = evaluate_run_bundle(
                run_manifest=manifest, incident_spec=incident_spec, artifact_root=folder,
                report_context=report_context if report_result else None,
                findings=report_payload.get("findings") if report_result else None,
                evidence_records=report_payload.get("evidence_records") if report_result else None,
                verification_results=report_payload.get("verification_results") if report_result else None,
                event_reconstruction=reconstruction,
                source_dependency_edges=(lineage or {}).get("edges") if lineage else None,
                report_bundle_dir=folder / "report" if report_result else None,
            ) if report_result or reconstruction else run_frozen_suite()
        except Exception as exc:
            # Finish the run with a typed failure receipt instead of leaving an
            # occupied directory whose interim manifest claims completion.
            evaluation = {"schema_version": "1.0", "status": "failed",
                          "hard_gate_status": "failed", "error_type": type(exc).__name__,
                          "semantic_evaluation": {"status": "not_evaluated"}}
        if report_integrity is not None:
            evaluation["report_integrity"] = report_integrity
        _write_json_new(folder / "evaluation.json", evaluation)
        if evaluation.get("status") != "passed" or evaluation.get("hard_gate_status") == "failed":
            gate_ids = sorted({row.get("gate_id") for row in evaluation.get("hard_gate_failures", [])
                               if isinstance(row, dict) and isinstance(row.get("gate_id"), str)})
            unresolved.append({"item_id": "release_evaluation", "stage": "evaluation",
                               "status": "failed", "reason": "release_gates_not_passed",
                               "failed_gate_ids": gate_ids})
            _replace_jsonl(folder / "unresolved_queue.jsonl", unresolved)
        evaluation_stage_status = (
            "complete" if evaluation.get("status") == "passed" and evaluation.get("hard_gate_status") != "failed" else
            "failed" if evaluation.get("status") == "failed" or evaluation.get("hard_gate_status") == "failed" else
            "partial"
        )
        record("evaluation", evaluation_stage_status,
               "structural_gates_and_frozen_controls_semantic_accuracy_not_evaluated")
        _replace_jsonl(folder / "task_receipts.jsonl", task_rows)
        manifest["finished_at_utc"] = _now()
        manifest["task_receipts"] = [{"task_id": row["task_id"],
                                      "status": row["status"] if row["status"] in {"complete", "partial", "blocked", "not_found", "failed"} else "blocked",
                                      "attempted_at_utc": row["attempted_at_utc"],
                                      "detail_ref": row["detail_ref"]} for row in task_rows]
        manifest["outputs"] = _output_hashes(folder, exclude={"run_manifest.json"})
        validate_run_manifest(manifest)
        _replace_json(folder / "run_manifest.json", manifest)
        summary["analysis_status"] = "failed" if any(value == "failed" for value in stage_status.values()) else (
            "partial" if any(value != "complete" for value in stage_status.values()) or evaluation.get("status") != "passed" else "complete")
        summary["stage_status"] = stage_status
        summary["evaluation_status"] = evaluation.get("status")
        _replace_json(folder / "case_summary.json", summary)
        # Refresh the manifest after the summary was finalized.
        manifest["outputs"] = _output_hashes(folder, exclude={"run_manifest.json"})
        validate_run_manifest(manifest)
        _replace_json(folder / "run_manifest.json", manifest)
        return {**manifest, "analysis_status": summary["analysis_status"]}

    unresolved_path = folder / "unresolved_queue.jsonl"
    _write_jsonl_new(unresolved_path, unresolved)
    _write_jsonl_new(folder / "task_receipts.jsonl", task_rows)
    manifest = {
        "schema_version": "1.0", "run_id": run_id,
        "incident_id": incident_spec["incident_id"], "state": "completed_bounded",
        "started_at_utc": started, "finished_at_utc": _now(),
        "producer": "social.analysis.offline_case_runner/1.0", "git_commit": git_commit,
        "policy_version": "offline-typed-inputs-v1", "model_config": {"enabled": False},
        "source_snapshots": [],
        "task_receipts": [{"task_id": row["task_id"], "status": "blocked" if row["status"] == "not_run" else row["status"],
                            "attempted_at_utc": row["attempted_at_utc"], "detail_ref": row["detail_ref"]} for row in task_rows],
        "outputs": _output_hashes(folder), "limitations": limitations,
    }
    validate_run_manifest(manifest)
    _write_json_new(folder / "run_manifest.json", manifest)
    _write_json_new(folder / "case_summary.json", {
        "schema_version": "1.0", "run_id": run_id, "incident_id": incident_spec["incident_id"],
        "analysis_status": analysis_status, "stage_status": stage_status,
        "semantic_accuracy": "not_evaluated", "evaluation_status": evaluation.get("status"),
        "limitations": limitations,
    })
    manifest["outputs"] = _output_hashes(folder, exclude={"run_manifest.json"})
    _replace_json(folder / "run_manifest.json", manifest)
    return {**manifest, "analysis_status": analysis_status}


def _output_hashes(folder: Path, *, exclude: set[str] | None = None) -> list[dict]:
    excluded = exclude or set()
    rows = []
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise OfflineRunError("run output contains a symlink")
        if not path.is_file() or path.name in excluded:
            continue
        relative = path.relative_to(folder).as_posix()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append({"path": relative, "sha256": digest})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True, help="Social IncidentSpec JSON")
    parser.add_argument("--run-dir", type=Path, required=True, help="new private output directory")
    parser.add_argument("--git-commit", required=True)
    parser.add_argument("--execute", action="store_true", help="run deterministic local processors; default only plans")
    parser.add_argument("--uncertainty-target-label", default="target")
    for input_name, file_kind in _INPUTS.items():
        parser.add_argument("--" + input_name.replace("_", "-"), type=Path,
                            required=False, help=f"optional local {file_kind} {input_name} input")
    args = parser.parse_args(argv)
    try:
        spec_path = _safe_path(args.spec, must_exist=True)
        spec = _decode_json(_read_bounded(spec_path))
        inputs = {name: getattr(args, name) for name in _INPUTS if getattr(args, name) is not None}
        result = run_offline_case(
            spec, args.run_dir, git_commit=args.git_commit, inputs=inputs,
            execute=args.execute, uncertainty_target_label=args.uncertainty_target_label,
        )
    except (OfflineRunError, OSError, ValueError) as exc:
        print("offline-case-runner: " + (str(exc) if isinstance(exc, OfflineRunError) else type(exc).__name__), file=sys.stderr)
        return 2
    print(json.dumps({"run_id": result["run_id"], "analysis_status": result["analysis_status"],
                      "run_dir": str(args.run_dir)}, sort_keys=True))
    return 1 if result["analysis_status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
