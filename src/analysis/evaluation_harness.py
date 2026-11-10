"""Deterministic hard gates and frozen adversarial controls for Social runs.

The harness verifies contracts, references, timestamps, hashes and selected
evidence invariants. It does not judge whether source content semantically
proves a real-world proposition. That semantic status is always reported as
not evaluated by these hard gates.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.event_reconstruction import reconstruct_timeline
from src.analysis.report_generator import ReportValidationError, build_report_bundle
from src.analysis.audience_response_reporting import AudienceReportValidationError, validate_audience_aggregate
from src.contracts import ContractValidationError, validate_document, validate_incident_spec, validate_run_manifest


_UNRESOLVED = {"unverified", "insufficient_evidence", "not_verifiable"}
_SOURCE_UNAVAILABLE = {"partial", "blocked", "not_found", "failed"}
_VERIFICATION_STATUSES = {"unverified", "supported_within_scope", "refuted_within_scope",
                         "conflicting_evidence", "insufficient_evidence", "not_verifiable", "superseded"}
_SCENARIO_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "evaluation_harness"


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} must be an ISO-8601 UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _issue(gate_id: str, message: str, *, path: str | None = None) -> dict:
    row = {"gate_id": gate_id, "message": message}
    if path:
        row["path"] = path
    return row


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_output_issues(run_manifest: dict, artifact_root: str | os.PathLike[str] | None) -> tuple[list[dict], list[str]]:
    issues: list[dict] = []
    warnings: list[str] = []
    outputs = run_manifest.get("outputs", [])
    if not isinstance(outputs, list):
        return [_issue("manifest.outputs_list", "run manifest outputs must be a list")], warnings
    paths = [row.get("path") for row in outputs if isinstance(row, dict) and isinstance(row.get("path"), str)]
    if len(paths) != len(set(paths)):
        issues.append(_issue("manifest.unique_output_paths", "run manifest contains duplicate output paths"))
    if artifact_root is None:
        if outputs:
            warnings.append("output_files_not_checked_without_artifact_root")
        if run_manifest.get("source_snapshots"):
            warnings.append("source_snapshot_bytes_not_checked_without_artifact_root")
        return issues, warnings

    root = Path(artifact_root).expanduser()
    try:
        root = root.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return [_issue("manifest.artifact_root", "artifact_root is unavailable")], warnings
    for index, row in enumerate(outputs):
        if not isinstance(row, dict):
            continue  # schema validation reports this first
        if not isinstance(row.get("path"), str) or not isinstance(row.get("sha256"), str):
            continue
        try:
            raw = Path(row["path"]).expanduser()
            candidate = raw if raw.is_absolute() else root / raw
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            issues.append(_issue("manifest.output_exists", "manifest output file is unavailable", path=f"outputs[{index}].path"))
            continue
        if not resolved.is_relative_to(root):
            issues.append(_issue("manifest.output_within_root", "manifest output escapes artifact_root", path=f"outputs[{index}].path"))
            continue
        if candidate.is_symlink() or not resolved.is_file():
            issues.append(_issue("manifest.output_regular_file", "manifest output must be a regular non-symlink file", path=f"outputs[{index}].path"))
            continue
        try:
            actual = _sha256_file(resolved)
        except OSError:
            issues.append(_issue("manifest.output_readable", "manifest output cannot be read", path=f"outputs[{index}].path"))
            continue
        if actual != row["sha256"]:
            issues.append(_issue("manifest.output_sha256", "manifest output hash does not match bytes", path=f"outputs[{index}].path"))

    snapshots = run_manifest.get("source_snapshots", [])
    for index, row in enumerate(snapshots if isinstance(snapshots, list) else []):
        if not isinstance(row, dict):
            continue  # schema validation reports this first
        path_value = row.get("snapshot_artifact_path")
        if not isinstance(path_value, str) or not path_value:
            issues.append(_issue("manifest.snapshot_path_required", "source snapshot bytes cannot be verified without snapshot_artifact_path", path=f"source_snapshots[{index}].snapshot_artifact_path"))
            continue
        try:
            raw = Path(path_value).expanduser()
            candidate = raw if raw.is_absolute() else root / raw
            resolved = candidate.resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            issues.append(_issue("manifest.snapshot_exists", "source snapshot artifact is unavailable", path=f"source_snapshots[{index}].snapshot_artifact_path"))
            continue
        if not resolved.is_relative_to(root):
            issues.append(_issue("manifest.snapshot_within_root", "source snapshot artifact escapes artifact_root", path=f"source_snapshots[{index}].snapshot_artifact_path"))
            continue
        if candidate.is_symlink() or not resolved.is_file():
            issues.append(_issue("manifest.snapshot_regular_file", "source snapshot artifact must be a regular non-symlink file", path=f"source_snapshots[{index}].snapshot_artifact_path"))
            continue
        try:
            actual = _sha256_file(resolved)
        except OSError:
            issues.append(_issue("manifest.snapshot_readable", "source snapshot artifact cannot be read", path=f"source_snapshots[{index}].snapshot_artifact_path"))
            continue
        if actual != row.get("sha256"):
            issues.append(_issue("manifest.snapshot_sha256", "source snapshot hash does not match artifact bytes", path=f"source_snapshots[{index}].sha256"))
    return issues, warnings


def _event_reference_issues(reconstruction: dict, evidence_records: list[dict]) -> list[dict]:
    issues = []
    evidence_ids = {row.get("evidence_id") for row in evidence_records
                    if isinstance(row, dict) and isinstance(row.get("evidence_id"), str)}
    events = reconstruction.get("events", [])
    event_ids = [row.get("event_id") for row in events if isinstance(row, dict)]
    mention_ids = [row.get("event_mention_id") for row in events if isinstance(row, dict)]
    if len(event_ids) != len(set(event_ids)):
        issues.append(_issue("events.unique_ids", "duplicate event_id values"))
    if len(mention_ids) != len(set(mention_ids)):
        issues.append(_issue("events.unique_mentions", "duplicate event_mention_id values"))
    known_events, known_mentions = set(event_ids), set(mention_ids)
    for index, event in enumerate(events):
        for evidence_id in event.get("evidence_ids", []):
            if evidence_id not in evidence_ids:
                issues.append(_issue("events.evidence_refs", f"unknown evidence ID {evidence_id}", path=f"events[{index}].evidence_ids"))
        for evidence_id in event.get("event_time", {}).get("anchor_evidence_ids", []) if isinstance(event.get("event_time"), dict) else []:
            if evidence_id not in evidence_ids:
                issues.append(_issue("events.time_anchor_refs", f"unknown time anchor evidence ID {evidence_id}", path=f"events[{index}].event_time"))
    for index, link in enumerate(reconstruction.get("candidate_links", [])):
        for event_id in link.get("event_ids", []):
            if event_id not in known_events:
                issues.append(_issue("events.link_refs", f"unknown event ID {event_id}", path=f"candidate_links[{index}]"))
    for scale_name, entries in reconstruction.get("timelines", {}).items():
        for index, entry in enumerate(entries):
            if entry.get("event_id") not in known_events or entry.get("event_mention_id") not in known_mentions:
                issues.append(_issue("events.timeline_refs", "timeline entry references an unknown event", path=f"timelines.{scale_name}[{index}]"))
            for evidence_id in entry.get("evidence_ids", []):
                if evidence_id not in evidence_ids:
                    issues.append(_issue("events.timeline_evidence_refs", f"unknown evidence ID {evidence_id}", path=f"timelines.{scale_name}[{index}]"))
    for index, relation in enumerate(reconstruction.get("temporal_relations", [])):
        for event_id in relation.get("event_ids", []):
            if event_id not in known_events:
                issues.append(_issue("events.temporal_relation_refs", f"unknown event ID {event_id}", path=f"temporal_relations[{index}]"))
    return issues


def _lineage_issues(evidence_records: list[dict], verification_results: list[dict]) -> tuple[list[dict], list[str]]:
    """Check explicit root assignments without treating unknown lineage as independence."""
    by_id = {row.get("evidence_id"): row for row in evidence_records
             if isinstance(row, dict) and isinstance(row.get("evidence_id"), str)}
    issues = []
    warnings = []
    for result_index, result in enumerate(verification_results):
        if not isinstance(result, dict):
            continue
        status = result.get("status")
        if not isinstance(status, str) or status not in _VERIFICATION_STATUSES:
            issues.append(_issue("verification.status_invalid", "verification result has an unknown or malformed status", path=f"verification_results[{result_index}].status"))
            continue
        if status == "conflicting_evidence":
            for field in ("supporting_evidence_ids", "contradicting_evidence_ids"):
                refs = result.get(field) or []
                if not isinstance(refs, list) or not refs:
                    issues.append(_issue("verification.conflict_evidence_required", "conflicting_evidence requires explicit evidence on both sides", path=f"verification_results[{result_index}].{field}"))
        minimum = result.get("minimum_independent_roots", 1)
        if status in {"supported_within_scope", "refuted_within_scope"} and (type(minimum) is not int or minimum < 1):
            issues.append(_issue("verification.minimum_roots", "minimum_independent_roots must be a positive integer", path=f"verification_results[{result_index}].minimum_independent_roots"))
            minimum = None
        for side, refs_field, root_field in (
            ("supporting", "supporting_evidence_ids", "supporting_root_count"),
            ("contradicting", "contradicting_evidence_ids", "contradicting_root_count"),
        ):
            refs = result.get(refs_field) or []
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs):
                warnings.append(f"{result.get('claim_id', result_index)}:{side}_refs_not_auditable")
                continue
            records = [by_id.get(ref) for ref in refs]
            affirmative_side = ((status == "supported_within_scope" and side == "supporting")
                                or (status == "refuted_within_scope" and side == "contradicting"))
            if affirmative_side:
                if not refs:
                    issues.append(_issue("verification.evidence_required", f"{status} requires explicit {side} evidence", path=f"verification_results[{result_index}].{refs_field}"))
                    continue
                if any(record is None for record in records):
                    continue  # dangling IDs are reported by _verification_reference_issues
                assignments = [record.get("independent_root_id") for record in records]
                if any(not isinstance(root, str) or not root.strip() for root in assignments):
                    issues.append(_issue("verification.root_lineage_required", f"{status} requires auditable independent-root IDs", path=f"verification_results[{result_index}].{refs_field}"))
                    continue
            if not records:
                continue
            assignments = [record.get("independent_root_id") for record in records if record]
            if len(assignments) != len(records) or any(not isinstance(root, str) or not root.strip() for root in assignments):
                warnings.append(f"{result.get('claim_id', result_index)}:{side}_root_count_not_independently_recomputable")
                continue
            assignments = [root.strip() for root in assignments]
            recomputed = len(set(assignments))
            reported = result.get(root_field)
            if reported is None:
                reported = result.get("independent_support_roots" if side == "supporting" else "independent_contradiction_roots")
            if reported is not None and reported != recomputed:
                issues.append(_issue("verification.root_count", f"reported {side} root count {reported} differs from explicit evidence-root count {recomputed}", path=f"verification_results[{result_index}].{root_field}"))
            required_status = "supported_within_scope" if side == "supporting" else "refuted_within_scope"
            if minimum is not None and status == required_status and recomputed < minimum:
                issues.append(_issue("verification.minimum_roots", f"{side} status does not meet minimum independent-root count", path=f"verification_results[{result_index}]"))
    return issues, sorted(set(warnings))


def _verification_reference_issues(evidence_records: list[dict], verification_results: list[dict]) -> list[dict]:
    """Reject dangling evidence IDs regardless of whether a report is rendered."""
    evidence_ids = {row.get("evidence_id") for row in evidence_records
                    if isinstance(row, dict) and isinstance(row.get("evidence_id"), str)}
    issues = []
    for result_index, result in enumerate(verification_results):
        if not isinstance(result, dict):
            continue
        for field in ("supporting_evidence_ids", "contradicting_evidence_ids"):
            refs = result.get(field) or []
            if not isinstance(refs, list):
                issues.append(_issue("verification.evidence_refs_list", f"{field} must be a list", path=f"verification_results[{result_index}].{field}"))
                continue
            for evidence_id in refs:
                if not isinstance(evidence_id, str) or evidence_id not in evidence_ids:
                    issues.append(_issue("verification.evidence_refs", f"unknown evidence ID {evidence_id}", path=f"verification_results[{result_index}].{field}"))
    return issues


def _verification_evidence_as_of_issues(evidence_records: list[dict], verification_results: list[dict]) -> list[dict]:
    """Require verification cutoffs and reject cited evidence captured afterward."""
    by_id = {row.get("evidence_id"): row for row in evidence_records
             if isinstance(row, dict) and isinstance(row.get("evidence_id"), str)}
    issues = []
    for result_index, result in enumerate(verification_results):
        if not isinstance(result, dict):
            continue
        result_id = result.get("verification_result_id") or result.get("result_id") or str(result_index)
        try:
            cutoff = _parse_utc(result.get("as_of_utc"), f"verification.{result_id}.as_of_utc")
        except ValueError as exc:
            cutoff = None
            issues.append(_issue("verification.as_of_utc", str(exc),
                                 path=f"verification_results[{result_index}].as_of_utc"))
        for field in ("supporting_evidence_ids", "contradicting_evidence_ids"):
            refs = result.get(field) or []
            if not isinstance(refs, list):
                continue  # _verification_reference_issues reports the malformed list.
            for evidence_id in refs:
                record = by_id.get(evidence_id) if isinstance(evidence_id, str) else None
                if record is None:
                    continue  # dangling references are reported separately.
                try:
                    captured = _parse_utc(
                        record.get("captured_at_utc"), f"evidence.{evidence_id}.captured_at_utc"
                    )
                except ValueError as exc:
                    issues.append(_issue(
                        "verification.evidence_capture_time_required",
                        f"cited evidence {evidence_id} has no valid capture timestamp: {exc}",
                        path=f"verification_results[{result_index}].{field}",
                    ))
                    continue
                if cutoff is not None and captured > cutoff:
                    issues.append(_issue(
                        "verification.evidence_after_as_of",
                        f"cited evidence {evidence_id} was captured after verification as_of_utc",
                        path=f"verification_results[{result_index}].{field}",
                    ))
    return issues


def _event_evidence_as_of_issues(reconstruction: dict, evidence_records: list[dict], cutoff: datetime) -> list[dict]:
    """Require every evidence record cited by an event/timeline to exist by cutoff."""
    referenced: set[str] = set()
    for event in reconstruction.get("events", []):
        if not isinstance(event, dict):
            continue
        referenced.update(item for item in event.get("evidence_ids", []) if isinstance(item, str))
        event_time = event.get("event_time")
        if isinstance(event_time, dict):
            referenced.update(item for item in event_time.get("anchor_evidence_ids", []) if isinstance(item, str))
    for rows in reconstruction.get("timelines", {}).values():
        if isinstance(rows, list):
            for entry in rows:
                if isinstance(entry, dict):
                    referenced.update(item for item in entry.get("evidence_ids", []) if isinstance(item, str))

    by_id = {row.get("evidence_id"): row for row in evidence_records
             if isinstance(row, dict) and isinstance(row.get("evidence_id"), str)}
    issues = []
    for evidence_id in sorted(referenced):
        record = by_id.get(evidence_id)
        if record is None:
            continue  # Missing IDs are reported by _event_reference_issues.
        try:
            captured = _parse_utc(record.get("captured_at_utc"), f"evidence.{evidence_id}.captured_at_utc")
        except ValueError:
            issues.append(_issue("events.evidence_capture_time_required", f"cited evidence {evidence_id} has no valid capture timestamp"))
            continue
        if captured > cutoff:
            issues.append(_issue("events.evidence_as_of", f"cited evidence {evidence_id} was captured after the as-of cutoff"))
    return issues


def verify_report_bundle(report_dir: str | os.PathLike[str]) -> dict:
    """Check the generated report receipt against every listed output byte."""
    directory = Path(report_dir).expanduser()
    if directory.is_symlink() or not directory.is_dir():
        return {"status": "failed", "issues": [_issue("report.directory", "report directory must exist and not be a symlink")], "verified_files": 0}
    receipt_path = directory / "run_receipt.json"
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return {"status": "failed", "issues": [_issue("report.receipt", "run_receipt.json is missing or unsafe")], "verified_files": 0}
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "failed", "issues": [_issue("report.receipt_json", "run_receipt.json is invalid")], "verified_files": 0}
    hashes = receipt.get("output_sha256") if isinstance(receipt, dict) else None
    if not isinstance(hashes, dict) or not hashes:
        return {"status": "failed", "issues": [_issue("report.output_hashes", "receipt has no output_sha256 map")], "verified_files": 0}
    issues = []
    for name, expected in sorted(hashes.items()):
        if not isinstance(name, str) or Path(name).name != name or not isinstance(expected, str):
            issues.append(_issue("report.safe_output_name", f"unsafe report output name: {name!r}"))
            continue
        path = directory / name
        if path.is_symlink() or not path.is_file():
            issues.append(_issue("report.output_exists", f"report output missing or unsafe: {name}"))
            continue
        try:
            actual = _sha256_file(path)
        except OSError:
            issues.append(_issue("report.output_readable", f"report output unreadable: {name}"))
            continue
        if actual != expected:
            issues.append(_issue("report.output_sha256", f"report output hash mismatch: {name}"))
    return {"status": "failed" if issues else "passed", "issues": issues,
            "verified_files": len(hashes) - sum(issue["gate_id"] == "report.output_exists" for issue in issues)}


def evaluate_run_bundle(*, run_manifest: dict, incident_spec: dict | None = None,
                        artifact_root: str | os.PathLike[str] | None = None,
                        report_context: dict | None = None, findings: list[dict] | None = None,
                        evidence_records: list[dict] | None = None,
                        verification_results: list[dict] | None = None,
                        event_reconstruction: dict | None = None,
                        source_dependency_edges: list[dict] | None = None,
                        audience_aggregate: dict | None = None,
                        report_bundle_dir: str | os.PathLike[str] | None = None) -> dict:
    """Run structural release gates over a machine-readable Social run bundle.

    Passing means the supplied contracts, references and recorded bytes pass
    deterministic checks. It does not mean the claims are true or that sources
    semantically entail report statements.
    """
    issues: list[dict] = []
    warnings: list[str] = []
    report_inputs_supplied = any(value is not None for value in (
        findings, evidence_records, verification_results, audience_aggregate
    ))
    if not isinstance(run_manifest, dict):
        return {"schema_version": "1.0", "status": "failed", "hard_gate_status": "failed",
                "hard_gate_failures": [_issue("schema.run_manifest", "run_manifest must be an object")],
                "warnings": [], "counts": {}, "report_bundle": None,
                "semantic_evaluation": {"status": "not_evaluated", "unsupported_assertion_rate": None}}
    normalized_inputs = {}
    for field, value in (("findings", findings), ("evidence_records", evidence_records),
                         ("verification_results", verification_results)):
        if value is None:
            normalized_inputs[field] = []
        elif isinstance(value, list):
            normalized_inputs[field] = value
        else:
            issues.append(_issue(f"bundle.{field}_list", f"{field} must be a list"))
            normalized_inputs[field] = []
    findings = normalized_inputs["findings"]
    evidence_records = normalized_inputs["evidence_records"]
    verification_results = normalized_inputs["verification_results"]
    audience_aggregate_valid = audience_aggregate is None
    if audience_aggregate is not None:
        try:
            validate_audience_aggregate(audience_aggregate)
            audience_aggregate_valid = True
        except (AudienceReportValidationError, TypeError, ValueError, KeyError) as exc:
            issues.append(_issue("audience.schema", str(exc)))

    try:
        validate_run_manifest(run_manifest)
    except (ContractValidationError, TypeError, KeyError) as exc:
        issues.append(_issue("schema.run_manifest", str(exc)))
    if incident_spec is not None:
        try:
            if not isinstance(incident_spec, dict):
                raise TypeError("IncidentSpec must be an object")
            validate_incident_spec(incident_spec)
            if run_manifest.get("incident_id") != incident_spec.get("incident_id"):
                issues.append(_issue("run.incident_match", "run manifest and IncidentSpec incident_id differ"))
        except (ContractValidationError, TypeError, KeyError) as exc:
            issues.append(_issue("schema.incident_spec", str(exc)))

    if run_manifest.get("state") != "completed_bounded":
        issues.append(_issue("run.release_state", "only completed_bounded runs can pass release gates"))
    try:
        started = _parse_utc(run_manifest.get("started_at_utc"), "run_manifest.started_at_utc")
        finished_raw = run_manifest.get("finished_at_utc")
        finished = _parse_utc(finished_raw, "run_manifest.finished_at_utc") if finished_raw else None
        run_receipts = run_manifest.get("task_receipts", [])
        if not isinstance(run_receipts, list):
            run_receipts = []
        for index, receipt in enumerate(run_receipts):
            if not isinstance(receipt, dict) or not receipt.get("attempted_at_utc"):
                continue
            attempted = _parse_utc(receipt["attempted_at_utc"], f"task_receipts[{index}].attempted_at_utc")
            if attempted < started or (finished is not None and attempted > finished):
                issues.append(_issue("run.task_time_bounds", "task attempt falls outside the run interval", path=f"task_receipts[{index}]"))
        if incident_spec is not None:
            as_of_time = _parse_utc(incident_spec.get("as_of_utc"), "IncidentSpec.as_of_utc")
            run_snapshots = run_manifest.get("source_snapshots", [])
            if not isinstance(run_snapshots, list):
                run_snapshots = []
            for index, snapshot in enumerate(run_snapshots):
                fetched = snapshot.get("fetched_at_utc") if isinstance(snapshot, dict) else None
                if fetched and _parse_utc(fetched, f"source_snapshots[{index}].fetched_at_utc") > as_of_time:
                    issues.append(_issue("run.snapshot_as_of", "source snapshot was fetched after the IncidentSpec as_of cutoff", path=f"source_snapshots[{index}]"))
    except (TypeError, ValueError) as exc:
        issues.append(_issue("run.temporal_consistency", str(exc)))

    snapshots = run_manifest.get("source_snapshots", [])
    if not isinstance(snapshots, list):
        snapshots = []
    receipts = run_manifest.get("task_receipts", [])
    if not isinstance(receipts, list):
        receipts = []
    snapshot_ids = [row.get("snapshot_id") for row in snapshots
                    if isinstance(row, dict) and isinstance(row.get("snapshot_id"), str)]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        issues.append(_issue("manifest.unique_snapshot_ids", "run manifest contains duplicate snapshot_id values"))
    task_ids = [row.get("task_id") for row in receipts
                if isinstance(row, dict) and isinstance(row.get("task_id"), str)]
    if len(task_ids) != len(set(task_ids)):
        issues.append(_issue("manifest.unique_task_ids", "run manifest contains duplicate task_id values"))
    output_issues, output_warnings = _manifest_output_issues(run_manifest, artifact_root)
    issues.extend(output_issues)
    warnings.extend(output_warnings)

    as_of = incident_spec.get("as_of_utc") if isinstance(incident_spec, dict) else None
    if report_context is not None:
        if not isinstance(report_context, dict):
            issues.append(_issue("report.context_object", "report context must be an object"))
            report_context = None
    if report_context is not None:
        if report_context.get("incident_id") != run_manifest.get("incident_id"):
            issues.append(_issue("report.incident_match", "report context incident_id differs from run manifest"))
        if report_context.get("run_id") != run_manifest.get("run_id"):
            issues.append(_issue("report.run_match", "report context run_id differs from run manifest"))
        try:
            context_as_of = _parse_utc(report_context.get("as_of_utc"), "report_context.as_of_utc")
            if as_of is not None and context_as_of != _parse_utc(as_of, "IncidentSpec.as_of_utc"):
                issues.append(_issue("report.as_of_match", "report context as_of_utc differs from IncidentSpec"))
        except ValueError as exc:
            issues.append(_issue("report.as_of_utc", str(exc)))
        if (findings is not None or evidence_records is not None or verification_results is not None
                or (audience_aggregate is not None and audience_aggregate_valid)):
            try:
                with tempfile.TemporaryDirectory(prefix="social-eval-report-") as temporary:
                    build_report_bundle(findings, evidence_records, verification_results, report_context,
                                        Path(temporary) / "report",
                                        audience_aggregate=audience_aggregate if audience_aggregate_valid else None)
            except (ReportValidationError, OSError, TypeError, ValueError) as exc:
                issues.append(_issue("report.structural_integrity", str(exc)))
    elif report_inputs_supplied:
        issues.extend(_verification_evidence_as_of_issues(evidence_records, verification_results))
        warnings.append("report_inputs_not_checked_without_report_context")

    if event_reconstruction is not None:
        try:
            validate_document(event_reconstruction, "event_reconstruction.schema.json")
            if event_reconstruction.get("provenance", {}).get("run_id") not in (None, run_manifest.get("run_id")):
                issues.append(_issue("events.run_match", "event reconstruction run_id differs from manifest"))
            if event_reconstruction.get("provenance", {}).get("incident_id") not in (None, run_manifest.get("incident_id")):
                issues.append(_issue("events.incident_match", "event reconstruction incident_id differs from manifest"))
            reconstruction_as_of = event_reconstruction.get("as_of_utc")
            if reconstruction_as_of is None:
                issues.append(_issue("events.as_of_required", "event reconstruction must declare an as_of_utc"))
            else:
                reconstruction_cutoff = _parse_utc(reconstruction_as_of, "events.as_of_utc")
                if as_of and reconstruction_cutoff != _parse_utc(as_of, "IncidentSpec.as_of_utc"):
                    issues.append(_issue("events.as_of_match", "event reconstruction as_of_utc differs from IncidentSpec"))
                issues.extend(_event_evidence_as_of_issues(
                    event_reconstruction, evidence_records, reconstruction_cutoff))
            issues.extend(_event_reference_issues(event_reconstruction, evidence_records))
        except (ContractValidationError, TypeError, ValueError, KeyError) as exc:
            issues.append(_issue("schema.event_reconstruction", str(exc)))

    if source_dependency_edges is not None:
        evidence_ids = {row.get("evidence_id") for row in evidence_records
                        if isinstance(row, dict) and isinstance(row.get("evidence_id"), str)}
        if not isinstance(source_dependency_edges, list):
            issues.append(_issue("lineage.edges_list", "source dependency edges must be a list"))
            source_dependency_edges = []
        edge_ids = []
        for index, edge in enumerate(source_dependency_edges):
            if not isinstance(edge, dict):
                issues.append(_issue("lineage.edge_object", "source dependency edge must be an object", path=f"source_dependency_edges[{index}]"))
                continue
            edge_id = edge.get("edge_id")
            if isinstance(edge_id, str):
                edge_ids.append(edge_id)
            refs = edge.get("evidence_ids") or []
            if not isinstance(refs, list):
                issues.append(_issue("lineage.evidence_refs_list", "edge evidence_ids must be a list", path=f"source_dependency_edges[{index}]"))
                continue
            for evidence_id in refs:
                if not isinstance(evidence_id, str) or evidence_id not in evidence_ids:
                    issues.append(_issue("lineage.evidence_refs", f"unknown evidence ID {evidence_id}", path=f"source_dependency_edges[{index}]"))
        if len(edge_ids) != len(set(edge_ids)):
            issues.append(_issue("lineage.unique_edge_ids", "source dependency edge IDs are not unique"))

    issues.extend(_verification_reference_issues(evidence_records, verification_results))
    lineage_issues, lineage_warnings = _lineage_issues(evidence_records, verification_results)
    issues.extend(lineage_issues)
    warnings.extend(lineage_warnings)

    report_receipt = None
    if report_bundle_dir is not None:
        report_receipt = verify_report_bundle(report_bundle_dir)
        issues.extend(report_receipt["issues"])

    return {
        "schema_version": "1.0",
        "status": "failed" if issues else "passed_with_warnings" if warnings else "passed",
        "hard_gate_status": "failed" if issues else "passed",
        "hard_gate_failures": issues,
        "warnings": sorted(set(warnings)),
        "counts": {
            "source_snapshots": len(snapshots),
            "task_receipts": len(receipts),
            "outputs": len(run_manifest.get("outputs", [])) if isinstance(run_manifest.get("outputs", []), list) else 0,
            "evidence_records": len(evidence_records),
            "verification_results": len(verification_results),
            "findings": len(findings),
            "events": len(event_reconstruction.get("events", [])) if event_reconstruction else 0,
            "audience_groups": len(audience_aggregate.get("groups", [])) if isinstance(audience_aggregate, dict) and isinstance(audience_aggregate.get("groups"), list) else 0,
        },
        "report_bundle": report_receipt,
        "semantic_evaluation": {
            "status": "not_evaluated",
            "unsupported_assertion_rate": None,
            "citation_entailment": "not_evaluated",
            "event_truth": "not_evaluated",
            "source_authenticity": "not_evaluated",
            "audience_label_accuracy": "not_evaluated",
            "reason": "Hard gates validate structure, references, provenance and timestamps, not semantic truth or entailment.",
        },
    }


def evaluate_frozen_scenario(scenario: dict) -> dict:
    """Exercise one named synthetic invariant; used by tests and release suite."""
    scenario_id = scenario.get("scenario_id", "unknown")
    kind = scenario.get("kind")
    failures: list[dict] = []
    try:
        if kind == "timeline_order":
            result = reconstruct_timeline(scenario["event_mentions"], run_context=scenario["run_context"])
            observed = [row["event_mention_id"] for row in result["timelines"]["physical"]]
            if observed != scenario["expected_order"]:
                failures.append(_issue("scenario.timeline_order", f"expected {scenario['expected_order']}, got {observed}"))
        elif kind == "source_root_fanout":
            roots = [family.get("root_id") for family in scenario.get("copy_families", [])
                     if isinstance(family, dict) and type(family.get("copy_count")) is int and family["copy_count"] > 0]
            if any(not isinstance(root, str) or not root for root in roots):
                failures.append(_issue("scenario.root_ids", "every copied evidence family must have a known root ID"))
            actual_roots = len(set(roots))
            if scenario.get("reported_independent_roots") != actual_roots:
                failures.append(_issue("scenario.root_fanout", f"copy fan-out changed roots: expected {actual_roots}"))
            if (scenario.get("verification_status") == "supported_within_scope"
                    and actual_roots < scenario.get("minimum_independent_roots", 1)):
                failures.append(_issue("scenario.root_minimum", "support status exceeds independent-root evidence"))
        elif kind == "contradiction_preserved":
            support = set(scenario.get("supporting_evidence_ids", []))
            contradict = set(scenario.get("contradicting_evidence_ids", []))
            available = set(scenario.get("available_evidence_ids", []))
            if scenario.get("verification_status") != "conflicting_evidence" or not support or not contradict:
                failures.append(_issue("scenario.contradictions", "conflicting status must preserve evidence on both sides"))
            if (support | contradict) - available or support & contradict:
                failures.append(_issue("scenario.contradiction_refs", "contradiction evidence references are missing or overlap"))
        elif kind == "as_of_cutoff":
            cutoff = _parse_utc(scenario.get("as_of_utc"), "scenario.as_of_utc")
            rows = {row.get("evidence_id"): row for row in scenario.get("evidence", []) if isinstance(row, dict)}
            cited = scenario.get("cited_evidence_ids", [])
            if len(cited) != len(set(cited)) or set(cited) - set(rows):
                failures.append(_issue("scenario.as_of_refs", "cited evidence is duplicated or missing"))
            for evidence_id in cited:
                if _parse_utc(rows[evidence_id].get("captured_at_utc"), f"evidence.{evidence_id}.captured_at_utc") > cutoff:
                    failures.append(_issue("scenario.as_of_future_evidence", f"future evidence {evidence_id} leaked into as-of view"))
        elif kind == "missing_corpus_abstention":
            statuses = set(scenario.get("source_access_statuses", []))
            if (statuses and statuses <= _SOURCE_UNAVAILABLE and not scenario.get("supporting_evidence_ids")
                    and not scenario.get("contradicting_evidence_ids")
                    and scenario.get("verification_status") not in _UNRESOLVED):
                failures.append(_issue("scenario.missing_corpus", "absence from unavailable sources must remain unresolved"))
        elif kind == "reused_video_not_support":
            reused = scenario.get("reused_from_prior_event") is True
            time_verified = scenario.get("recording_time_verified") is True
            earlier = False
            if time_verified:
                earlier = (_parse_utc(scenario.get("verified_recording_time_utc"), "recording_time")
                           < _parse_utc(scenario.get("incident_start_utc"), "incident_start"))
            if (reused and earlier and (scenario.get("claim_status") == "supported_within_scope"
                                        or scenario.get("supporting_evidence_ids"))):
                failures.append(_issue("scenario.reused_video", "verified pre-incident footage cannot support this incident solely by reuse"))
        else:
            failures.append(_issue("scenario.kind", f"unsupported scenario kind: {kind}"))
    except (KeyError, TypeError, ValueError) as exc:
        failures.append(_issue("scenario.valid_input", str(exc)))
    return {"scenario_id": scenario_id, "status": "failed" if failures else "passed", "failures": failures}


def run_frozen_suite(fixture_dir: str | os.PathLike[str] = _SCENARIO_DIR) -> dict:
    """Run the frozen synthetic/adversarial regression scenarios deterministically."""
    directory = Path(fixture_dir)
    scenarios = []
    for path in sorted(directory.glob("*.json")):
        try:
            scenario = json.loads(path.read_text(encoding="utf-8"))
            result = evaluate_frozen_scenario(scenario)
            result["fixture"] = path.name
        except (OSError, json.JSONDecodeError) as exc:
            result = {"scenario_id": path.stem, "fixture": path.name, "status": "failed",
                      "failures": [_issue("scenario.fixture_read", str(exc))]}
        scenarios.append(result)
    if not scenarios:
        scenarios.append({"scenario_id": "suite", "status": "failed", "fixture": None,
                          "failures": [_issue("scenario.fixtures_present", "no frozen scenario fixtures found")]})
    return {"schema_version": "1.0", "status": "passed" if all(row["status"] == "passed" for row in scenarios) else "failed",
            "scenario_count": len(scenarios), "passed_count": sum(row["status"] == "passed" for row in scenarios),
            "failed_count": sum(row["status"] == "failed" for row in scenarios), "scenarios": scenarios,
            "semantic_evaluation": "not_evaluated; scenarios verify deterministic invariants only"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frozen-suite", action="store_true", help="run the checked-in synthetic regression suite")
    parser.add_argument("--fixtures", type=Path, default=_SCENARIO_DIR)
    args = parser.parse_args(argv)
    if not args.frozen_suite:
        parser.error("currently supports --frozen-suite; use evaluate_run_bundle() for run payloads")
    result = run_frozen_suite(args.fixtures)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
