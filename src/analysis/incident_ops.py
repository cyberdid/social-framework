"""Immutable, resumable local snapshots for multi-case incident analysis.

This module records validated inputs and explicit dependency invalidation plans.
It is intentionally not a workflow runner: it never fetches source material or
claims that downstream analyses have been recomputed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.contracts import ContractValidationError, validate_incident_spec, validate_run_manifest
from src.evidence_ledger import EvidenceLedger, LedgerError


class IncidentOpsError(ValueError):
    """Input, path, or immutable snapshot error."""


_INCIDENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CORRECTION_KEYS = ("supersedes",)


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise IncidentOpsError(f"value is not canonical JSON: {exc}") from exc


def _utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise IncidentOpsError(f"{field} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IncidentOpsError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise IncidentOpsError(f"{field} must be an ISO-8601 UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _path_without_traversal(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if ".." in candidate.parts:
        raise IncidentOpsError(f"path traversal component is not allowed: {path}")
    return candidate.absolute()


def _reject_symlink_components(path: Path) -> None:
    """Fail closed when any existing component of a path is a symbolic link."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise IncidentOpsError(f"symlink path component is not allowed: {current}")


def _read_json(path: str | Path, label: str) -> dict[str, Any]:
    candidate = _path_without_traversal(path)
    _reject_symlink_components(candidate)
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IncidentOpsError(f"cannot read {label} JSON at {candidate}: {exc}") from exc
    if not isinstance(value, dict):
        raise IncidentOpsError(f"{label} must be a JSON object")
    return value


def _read_jsonl(path: str | Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    candidate = _path_without_traversal(path)
    _reject_symlink_components(candidate)
    records = []
    try:
        lines = candidate.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise IncidentOpsError(f"cannot read evidence ledger JSONL at {candidate}: {exc}") from exc
    seen: dict[str, str] = {}
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise IncidentOpsError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise IncidentOpsError(f"ledger record at line {line_number} must be an object")
        record_id = row.get("record_id")
        if not record_id:
            raise IncidentOpsError(f"EvidenceLedger record at line {line_number} requires record_id")
        known_value = row.get("created_at")
        known_at = _utc(known_value, f"ledger record {record_id}.created_at")
        encoded = _canonical(row)
        previous = seen.get(record_id)
        if previous is not None:
            if previous != encoded:
                raise IncidentOpsError(f"duplicate immutable ledger record id has different content: {record_id}")
            raise IncidentOpsError(f"duplicate ledger record id: {record_id}")
        seen[record_id] = encoded
        records.append(row)
    try:
        with EvidenceLedger() as ledger:
            ledger.import_jsonl("".join(_canonical(row) + "\n" for row in records))
            validated = ledger.records()
    except LedgerError as exc:
        raise IncidentOpsError(f"EvidenceLedger validation failed: {exc}") from exc
    return validated


def _read_dependencies(path: str | Path | None) -> list[dict[str, str]]:
    if path is None:
        return []
    payload = _read_json(path, "dependency graph")
    edges = payload.get("edges")
    if not isinstance(edges, list):
        raise IncidentOpsError("dependency graph requires an edges array")
    normalized = []
    seen = set()
    for index, edge in enumerate(edges):
        if not isinstance(edge, dict):
            raise IncidentOpsError(f"dependency edge {index} must be an object")
        source = edge.get("from", edge.get("from_node_id", edge.get("source_id")))
        target = edge.get("to", edge.get("to_node_id", edge.get("target_id")))
        if not isinstance(source, str) or not source or not isinstance(target, str) or not target:
            raise IncidentOpsError(f"dependency edge {index} requires explicit from/to node IDs")
        pair = (source, target)
        if pair not in seen:
            normalized.append({"from": source, "to": target})
            seen.add(pair)
    return sorted(normalized, key=lambda edge: (edge["from"], edge["to"]))


def _dependencies_after(changed_node_ids: list[str], edges: list[dict[str, str]]) -> list[str]:
    adjacency: dict[str, set[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge["from"], set()).add(edge["to"])
    visited = set(changed_node_ids)
    pending = list(changed_node_ids)
    invalidated = set()
    while pending:
        node = pending.pop()
        for target in adjacency.get(node, ()):
            if target not in visited:
                visited.add(target)
                invalidated.add(target)
                pending.append(target)
    return sorted(invalidated)


def _correction_rows(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    corrections = []
    for row in records:
        correction_of = next((row.get(key) for key in _CORRECTION_KEYS if isinstance(row.get(key), str) and row[key]), None)
        if correction_of:
            corrections.append({
                "correction_record_id": _record_id(row),
                "supersedes_record_id": correction_of,
                "known_at_utc": _format_utc(_utc(row["created_at"], "correction record created_at")),
            })
    return sorted(corrections, key=lambda item: (item["known_at_utc"], item["correction_record_id"]))


def _record_id(row: dict[str, Any]) -> str:
    return row["record_id"]


def _validate_snapshot_integrity(snapshot: dict[str, Any]) -> None:
    """Detect accidental edits to immutable input and derived snapshot payloads."""
    if (not isinstance(snapshot.get("incident_spec"), dict)
            or not isinstance(snapshot.get("run_provenance"), dict)
            or not isinstance(snapshot.get("ledger_records"), list)
            or not isinstance(snapshot.get("dependency_edges"), list)):
        raise IncidentOpsError("snapshot fingerprint inputs are missing or malformed")
    try:
        input_payload = {
            "incident_spec": snapshot["incident_spec"],
            "run_provenance": snapshot["run_provenance"],
            "ledger_records": snapshot["ledger_records"],
            "dependency_edges": snapshot["dependency_edges"],
        }
        fingerprint = hashlib.sha256(_canonical(input_payload).encode("utf-8")).hexdigest()
    except (KeyError, TypeError, IncidentOpsError) as exc:
        raise IncidentOpsError(f"snapshot fingerprint inputs are invalid: {exc}") from exc
    if snapshot.get("input_fingerprint") != fingerprint:
        raise IncidentOpsError("snapshot input fingerprint does not match its contents")
    if snapshot.get("run_id") != "run-" + fingerprint[:24]:
        raise IncidentOpsError("snapshot run_id does not match its input fingerprint")
    if not isinstance(snapshot.get("incident_id"), str) or not snapshot["incident_id"]:
        raise IncidentOpsError("snapshot incident_id is missing")
    if snapshot.get("incident_id") != snapshot["incident_spec"].get("incident_id"):
        raise IncidentOpsError("snapshot incident_id does not match its IncidentSpec")
    try:
        snapshot_as_of = _utc(snapshot.get("as_of_utc"), "snapshot.as_of_utc")
        spec_as_of = _utc(snapshot["incident_spec"].get("as_of_utc"), "IncidentSpec.as_of_utc")
    except IncidentOpsError as exc:
        raise IncidentOpsError(f"snapshot as_of_utc is invalid: {exc}") from exc
    if snapshot_as_of != spec_as_of:
        raise IncidentOpsError("snapshot as_of_utc does not match IncidentSpec.as_of_utc")

    content_payload = dict(snapshot)
    content_payload.pop("snapshot_content_sha256", None)
    content_sha = hashlib.sha256(_canonical(content_payload).encode("utf-8")).hexdigest()
    if snapshot.get("snapshot_content_sha256") != content_sha:
        raise IncidentOpsError("snapshot content fingerprint does not match its contents")


def _prior_snapshot_paths(case_dir: Path) -> list[Path]:
    snapshot_dir = case_dir / "snapshots"
    if not snapshot_dir.exists():
        return []
    _reject_symlink_components(snapshot_dir)
    paths = []
    for child in snapshot_dir.iterdir():
        if child.is_symlink():
            raise IncidentOpsError(f"symlink snapshot entry is not allowed: {child}")
        if child.is_dir() and (child / "snapshot.json").is_file():
            _reject_symlink_components(child / "snapshot.json")
            paths.append(child / "snapshot.json")
    return paths


def create_snapshot(
    store_dir: str | Path,
    spec_path: str | Path,
    run_manifest_path: str | Path,
    ledger_jsonl_path: str | Path | None = None,
    *,
    dependencies_path: str | Path | None = None,
    as_of_utc: str | None = None,
) -> dict[str, Any]:
    """Create or resume one immutable case snapshot, with a planned invalidation set."""
    spec = _read_json(spec_path, "IncidentSpec")
    manifest = _read_json(run_manifest_path, "RunManifest")
    try:
        validate_incident_spec(spec)
        validate_run_manifest(manifest)
    except ContractValidationError as exc:
        raise IncidentOpsError(str(exc)) from exc
    if spec["incident_id"] != manifest["incident_id"]:
        raise IncidentOpsError("IncidentSpec and RunManifest incident_id must match")
    incident_id = spec["incident_id"]
    if not _INCIDENT_ID.fullmatch(incident_id) or Path(incident_id).name != incident_id:
        raise IncidentOpsError("incident_id is not a safe case directory name")
    if as_of_utc is not None:
        spec["as_of_utc"] = _format_utc(_utc(as_of_utc, "--as-of"))
        try:
            validate_incident_spec(spec)
        except ContractValidationError as exc:
            raise IncidentOpsError(str(exc)) from exc
    cutoff = _utc(spec["as_of_utc"], "IncidentSpec.as_of_utc")
    spec["as_of_utc"] = _format_utc(cutoff)

    raw_records = _read_jsonl(ledger_jsonl_path)
    eligible = []
    excluded_future = []
    for row in raw_records:
        if _utc(row["created_at"], "ledger created_at") <= cutoff:
            eligible.append(row)
        else:
            excluded_future.append(_record_id(row))
    eligible.sort(key=lambda row: (_utc(row["created_at"], "ledger created_at"), _record_id(row)))
    excluded_future.sort()
    edges = _read_dependencies(dependencies_path)

    eligible_snapshots = []
    excluded_snapshot_count = 0
    for source_snapshot in manifest["source_snapshots"]:
        fetched = source_snapshot.get("fetched_at_utc")
        if fetched and _utc(fetched, "source snapshot fetched_at_utc") <= cutoff:
            eligible_snapshots.append(source_snapshot)
        else:
            excluded_snapshot_count += 1
    eligible_tasks = [row for row in manifest["task_receipts"]
                      if _utc(row["attempted_at_utc"], "task receipt attempted_at_utc") <= cutoff]
    run_provenance = {
        "producer": manifest["producer"],
        "git_commit": manifest["git_commit"], "policy_version": manifest["policy_version"],
        "model_config": manifest.get("model_config", {}),
        "source_snapshots": sorted(eligible_snapshots, key=lambda row: row["snapshot_id"]),
        "task_receipts": sorted(eligible_tasks, key=lambda row: row["task_id"]),
        "limitations": manifest.get("limitations", []),
    }

    fingerprint_payload = {
        "incident_spec": spec,
        "run_provenance": run_provenance,
        "ledger_records": eligible,
        "dependency_edges": edges,
    }
    fingerprint = hashlib.sha256(_canonical(fingerprint_payload).encode("utf-8")).hexdigest()
    stable_run_id = "run-" + fingerprint[:24]

    store = _path_without_traversal(store_dir)
    _reject_symlink_components(store)
    store.mkdir(parents=True, exist_ok=True)
    case_dir = store / incident_id
    case_dir.mkdir(exist_ok=True)
    _reject_symlink_components(case_dir)
    snapshot_dir = case_dir / "snapshots"
    snapshot_dir.mkdir(exist_ok=True)
    _reject_symlink_components(snapshot_dir)
    target_dir = snapshot_dir / stable_run_id
    target_file = target_dir / "snapshot.json"
    if target_file.exists():
        prior = _read_json(target_file, "existing immutable snapshot")
        _validate_snapshot_integrity(prior)
        if prior.get("input_fingerprint") != fingerprint:
            raise IncidentOpsError(f"stable run ID collision or modified snapshot: {stable_run_id}")
        return {"incident_id": incident_id, "run_id": stable_run_id,
                "snapshot_path": str(target_file), "reused": True,
                "parent_run_id": prior.get("parent_run_id"),
                "invalidation_plan": prior.get("invalidation_plan")}
    if target_dir.exists():
        raise IncidentOpsError(f"snapshot directory exists without immutable snapshot: {target_dir}")

    prior_snapshots = []
    cutoff_text = _format_utc(cutoff)
    for path in _prior_snapshot_paths(case_dir):
        try:
            prior = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(prior, dict):
                raise IncidentOpsError("snapshot must be a JSON object")
            _validate_snapshot_integrity(prior)
            prior_cutoff = _utc(prior.get("as_of_utc"), "prior snapshot as_of_utc")
        except (OSError, json.JSONDecodeError, IncidentOpsError) as exc:
            raise IncidentOpsError(f"invalid existing snapshot {path}: {exc}") from exc
        if prior_cutoff <= cutoff:
            prior_snapshots.append((prior_cutoff, prior.get("run_id", ""), prior))
    prior_snapshots.sort(key=lambda item: (item[0], item[1]))
    parent = prior_snapshots[-1][2] if prior_snapshots else None
    parent_records = {_record_id(row): row for row in (parent or {}).get("ledger_records", [])}
    current_records = {_record_id(row): row for row in eligible}
    previously_seen_records: dict[str, dict[str, Any]] = {}
    for _, _, previous_snapshot in prior_snapshots:
        for record in previous_snapshot.get("ledger_records", []):
            record_id = _record_id(record)
            previous_record = previously_seen_records.get(record_id)
            if previous_record is not None and _canonical(previous_record) != _canonical(record):
                raise IncidentOpsError(f"immutable ledger record differs across prior snapshots: {record_id}")
            previously_seen_records[record_id] = record
    missing_ids = sorted(previously_seen_records.keys() - current_records.keys())
    if missing_ids:
        raise IncidentOpsError(
            "append-only ledger snapshot is missing records from prior snapshots: " + ", ".join(missing_ids)
        )
    for record_id in previously_seen_records.keys() & current_records.keys():
        if _canonical(previously_seen_records[record_id]) != _canonical(current_records[record_id]):
            raise IncidentOpsError(f"immutable ledger record changed across snapshots: {record_id}")
    added_ids = sorted(current_records.keys() - parent_records.keys())
    changed_ids = set(added_ids)
    for correction in _correction_rows(eligible):
        if correction["correction_record_id"] in added_ids:
            changed_ids.add(correction["supersedes_record_id"])
    changed_nodes = sorted(changed_ids)
    invalidated = _dependencies_after(changed_nodes, edges)
    invalidation_plan = {
        "status": "planned_not_executed",
        "changed_node_ids": changed_nodes,
        "invalidated_node_ids": invalidated,
        "dependency_edge_count": len(edges),
        "note": "This snapshot exposes explicit downstream dependencies; recomputation was not executed.",
    }
    snapshot = {
        "schema_version": "1.0",
        "snapshot_mode": "retrospective_as_of_filter",
        "incident_id": incident_id,
        "run_id": stable_run_id,
        "parent_run_id": parent.get("run_id") if parent else None,
        "as_of_utc": cutoff_text,
        "input_fingerprint": fingerprint,
        "incident_spec": spec,
        "capture_metadata": {
            "captured_at_utc": _format_utc(datetime.now(timezone.utc)),
            "temporal_scope": "snapshot_capture_context_not_historical_evidence",
            "upstream_run_id": manifest["run_id"],
            "upstream_run_state": manifest["state"],
            "upstream_started_at_utc": manifest["started_at_utc"],
            "upstream_finished_at_utc": manifest.get("finished_at_utc"),
            "upstream_run_manifest_sha256": hashlib.sha256(_canonical(manifest).encode("utf-8")).hexdigest(),
        },
        "run_provenance": run_provenance,
        "ledger_records": eligible,
        "as_of_exclusion_counts": {"future_ledger_records": len(excluded_future),
                                   "unknown_or_future_source_snapshots": excluded_snapshot_count},
        "correction_history": _correction_rows(eligible),
        "dependency_edges": edges,
        "change_summary": {"added_record_ids": added_ids},
        "invalidation_plan": invalidation_plan,
    }
    snapshot["snapshot_content_sha256"] = hashlib.sha256(_canonical(snapshot).encode("utf-8")).hexdigest()
    encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    staging_name = None
    try:
        staging_name = tempfile.mkdtemp(prefix=f".{stable_run_id}.staging-", dir=snapshot_dir)
        staging_dir = Path(staging_name)
        staging_file = staging_dir / "snapshot.json"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(staging_file, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.rename(staging_dir, target_dir)
        staging_name = None
        directory_fd = os.open(snapshot_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if staging_name:
            import shutil
            shutil.rmtree(staging_name, ignore_errors=True)
        raise IncidentOpsError(f"cannot write immutable snapshot {target_file}: {exc}") from exc
    return {"incident_id": incident_id, "run_id": stable_run_id,
            "snapshot_path": str(target_file), "reused": False,
            "parent_run_id": snapshot["parent_run_id"],
            "invalidation_plan": invalidation_plan}


def list_snapshots(store_dir: str | Path, incident_id: str | None = None) -> list[dict[str, Any]]:
    store = _path_without_traversal(store_dir)
    _reject_symlink_components(store)
    if not store.exists():
        return []
    if incident_id is not None and (not _INCIDENT_ID.fullmatch(incident_id) or Path(incident_id).name != incident_id):
        raise IncidentOpsError("incident_id is not a safe case directory name")
    cases = [store / incident_id] if incident_id else sorted(path for path in store.iterdir() if path.is_dir())
    results = []
    for case_dir in cases:
        if case_dir.is_symlink():
            raise IncidentOpsError(f"symlink case entry is not allowed: {case_dir}")
        if not case_dir.is_dir():
            continue
        for path in _prior_snapshot_paths(case_dir):
            snapshot = _read_json(path, "snapshot")
            _validate_snapshot_integrity(snapshot)
            results.append({"incident_id": snapshot["incident_id"], "run_id": snapshot["run_id"],
                            "as_of_utc": snapshot["as_of_utc"], "snapshot_path": str(path)})
    return sorted(results, key=lambda row: (row["incident_id"], row["as_of_utc"], row["run_id"]))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create and inspect immutable Social incident snapshots.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    snapshot = subparsers.add_parser("snapshot", help="create or resume a deterministic as-of snapshot")
    snapshot.add_argument("--store", required=True, help="local snapshot registry directory")
    snapshot.add_argument("--spec", required=True, help="IncidentSpec JSON")
    snapshot.add_argument("--run-manifest", required=True, help="RunManifest JSON")
    snapshot.add_argument("--ledger-jsonl", help="append-only EvidenceLedger JSONL export")
    snapshot.add_argument("--dependencies", help="explicit dependency graph JSON with edges [{from,to}]")
    snapshot.add_argument("--as-of", help="UTC cutoff override; defaults to IncidentSpec.as_of_utc")
    listing = subparsers.add_parser("list", help="list immutable snapshots across one or all cases")
    listing.add_argument("--store", required=True)
    listing.add_argument("--incident-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        try:
            args = parser.parse_args(argv)
        except SystemExit as exc:
            if exc.code == 0:
                return 0
            raise
        if args.command == "snapshot":
            result = create_snapshot(args.store, args.spec, args.run_manifest, args.ledger_jsonl,
                                     dependencies_path=args.dependencies, as_of_utc=args.as_of)
        else:
            result = {"snapshots": list_snapshots(args.store, args.incident_id)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (IncidentOpsError, OSError) as exc:
        print(f"incident-ops: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
