import hashlib
import json
from pathlib import Path

import pytest

from src.analysis.incident_ops import IncidentOpsError, create_snapshot, list_snapshots, main


def valid_spec(as_of="2026-11-07T18:00:00Z"):
    return {
        "schema_version": "1.0", "incident_id": "case_demo_2026", "working_title": "Demo",
        "scope": {"description": "public records", "included": [], "excluded": []},
        "location_candidates": [],
        "time_window_utc": ["2026-11-02T00:00:00Z", "2026-11-05T23:59:59Z"],
        "languages": ["uk"], "seed_urls": [], "platforms": ["telegram"],
        "collection_policy": {"public_only": True, "allow_private_sources": False,
                              "access_methods": ["public_api"]},
        "privacy_policy": "local_only_by_default", "allow_external_ai": False,
        "models": {"text_provider": "disabled", "video_provider": "disabled"},
        "budgets": {"max_queries_per_claim": 0, "max_new_sources_per_run": 10,
                    "max_video_minutes_to_analyze": 0, "max_cost_usd": None},
        "as_of_utc": as_of, "report": {"allow_unresolved_sections": True},
    }


def valid_run_manifest():
    return {
        "schema_version": "1.0", "run_id": "upstream-run-1", "incident_id": "case_demo_2026",
        "state": "completed_bounded", "started_at_utc": "2026-11-07T17:00:00Z",
        "finished_at_utc": "2026-11-07T17:10:00Z", "producer": "test/1.0",
        "git_commit": "abcdef1234567", "policy_version": "1.0", "source_snapshots": [],
        "task_receipts": [], "outputs": [],
    }


def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def write_inputs(tmp_path, records, dependencies=None, as_of="2026-11-07T18:00:00Z"):
    spec = write_json(tmp_path / "spec.json", valid_spec(as_of))
    manifest = write_json(tmp_path / "manifest.json", valid_run_manifest())
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in records), encoding="utf-8")
    deps = write_json(tmp_path / "dependencies.json", dependencies or {"edges": []})
    return spec, manifest, ledger, deps


def ledger_record(record_id, created_at, **extra):
    return {"record_type": "Source", "schema_version": 1, "record_id": record_id,
            "created_at": created_at, "uri": f"https://example.test/{record_id}", "title": record_id,
            **extra}


def test_snapshot_rerun_is_noop_and_run_id_is_stable(tmp_path):
    files = write_inputs(tmp_path, [ledger_record("e1", "2026-11-07T17:30:00Z")])
    store = tmp_path / "store"
    first = create_snapshot(store, *files[:3], dependencies_path=files[3])
    payload_before = (Path(first["snapshot_path"])).read_bytes()
    second = create_snapshot(store, *files[:3], dependencies_path=files[3])

    assert first["run_id"] == second["run_id"]
    assert first["reused"] is False
    assert second["reused"] is True
    assert Path(second["snapshot_path"]).read_bytes() == payload_before


def test_snapshot_rerun_rejects_modified_snapshot_contents(tmp_path):
    files = write_inputs(tmp_path, [ledger_record("e1", "2026-11-07T17:30:00Z")])
    result = create_snapshot(tmp_path / "store", *files[:3], dependencies_path=files[3])
    snapshot_path = Path(result["snapshot_path"])
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["ledger_records"][0]["title"] = "silently changed"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(IncidentOpsError, match="fingerprint"):
        create_snapshot(tmp_path / "store", *files[:3], dependencies_path=files[3])


def test_snapshot_listing_rejects_modified_derived_snapshot_fields(tmp_path):
    files = write_inputs(tmp_path, [])
    result = create_snapshot(tmp_path / "store", *files[:3], dependencies_path=files[3])
    snapshot_path = Path(result["snapshot_path"])
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["change_summary"]["added_record_ids"] = ["invented"]
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(IncidentOpsError, match="content fingerprint"):
        list_snapshots(tmp_path / "store")


def test_snapshot_rejects_as_of_metadata_detached_from_spec(tmp_path):
    files = write_inputs(tmp_path, [])
    result = create_snapshot(tmp_path / "store", *files[:3], dependencies_path=files[3])
    snapshot_path = Path(result["snapshot_path"])
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["as_of_utc"] = "2026-11-08T18:00:00Z"
    content = dict(snapshot)
    content.pop("snapshot_content_sha256", None)
    snapshot["snapshot_content_sha256"] = hashlib.sha256(json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")).hexdigest()
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(IncidentOpsError, match="as_of_utc does not match"):
        list_snapshots(tmp_path / "store")


def test_run_id_ignores_only_upstream_lifecycle_timestamps(tmp_path):
    files = write_inputs(tmp_path, [])
    store = tmp_path / "store"
    first = create_snapshot(store, *files[:3], dependencies_path=files[3])
    manifest = json.loads(files[1].read_text(encoding="utf-8"))
    manifest["started_at_utc"] = "2026-11-07T17:01:00Z"
    manifest["finished_at_utc"] = "2026-11-07T17:11:00Z"
    write_json(files[1], manifest)
    second = create_snapshot(store, *files[:3], dependencies_path=files[3])
    assert first["run_id"] == second["run_id"]
    assert second["reused"] is True


def test_new_evidence_and_correction_create_snapshot_and_explicit_invalidation_plan(tmp_path):
    prior = [
        ledger_record("s1", "2026-11-07T17:30:00Z"),
        {"record_type": "Artifact", "schema_version": 1, "record_id": "a1",
         "created_at": "2026-11-07T17:30:00Z", "source_id": "s1", "path": "archive/a1",
         "sha256": "a" * 64, "media_type": "text/plain", "captured_at": "2026-11-07T17:30:00Z",
         "producer": "test/1.0", "run_id": "upstream-run-1", "limitations": []},
        {"record_type": "Claim", "schema_version": 1, "record_id": "c1",
         "created_at": "2026-11-07T17:30:00Z", "statement": "A source published a claim",
         "claim_type": "published_claim", "source_id": "s1"},
        {"record_type": "EvidenceAssessment", "schema_version": 1, "record_id": "assessment-1",
         "created_at": "2026-11-07T17:30:00Z", "claim_id": "c1", "status": "supported_within_scope",
         "evidence_refs": ["a1"], "producer": "test/1.0", "run_id": "upstream-run-1", "limitations": []},
    ]
    files = write_inputs(tmp_path, prior, {"edges": [{"from": "assessment-2", "to": "c1"},
                                                       {"from": "c1", "to": "finding-1"}]})
    store = tmp_path / "store"
    first = create_snapshot(store, *files[:3], dependencies_path=files[3])
    with files[2].open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"record_type": "EvidenceAssessment", "schema_version": 1,
                                 "record_id": "assessment-2", "created_at": "2026-11-07T17:45:00Z",
                                 "claim_id": "c1", "status": "conflicting_evidence",
                                 "evidence_refs": ["a1"], "supersedes": "assessment-1",
                                 "producer": "test/1.0", "run_id": "upstream-run-1", "limitations": []}) + "\n")

    second = create_snapshot(store, *files[:3], dependencies_path=files[3])
    snapshot = json.loads(Path(second["snapshot_path"]).read_text(encoding="utf-8"))
    assert first["run_id"] != second["run_id"]
    assert second["parent_run_id"] == first["run_id"]
    assert snapshot["correction_history"] == [{
        "correction_record_id": "assessment-2", "supersedes_record_id": "assessment-1",
        "known_at_utc": "2026-11-07T17:45:00Z",
    }]
    assert snapshot["invalidation_plan"]["status"] == "planned_not_executed"
    assert snapshot["invalidation_plan"]["changed_node_ids"] == ["assessment-1", "assessment-2"]
    assert snapshot["invalidation_plan"]["invalidated_node_ids"] == ["c1", "finding-1"]
    assert "not executed" in snapshot["invalidation_plan"]["note"].lower()


def test_snapshot_rejects_later_append_only_export_missing_parent_records(tmp_path):
    files = write_inputs(tmp_path, [ledger_record("old-1", "2026-11-07T17:00:00Z"),
                                    ledger_record("old-2", "2026-11-07T17:05:00Z")])
    store = tmp_path / "store"
    first = create_snapshot(store, *files[:3], dependencies_path=files[3])
    files[2].write_text(json.dumps(ledger_record("old-1", "2026-11-07T17:00:00Z")) + "\n", encoding="utf-8")

    with pytest.raises(IncidentOpsError, match="append-only.*missing"):
        create_snapshot(store, *files[:3], dependencies_path=files[3])
    assert list_snapshots(store)[0]["run_id"] == first["run_id"]


def test_post_cutoff_run_lifecycle_is_capture_metadata_not_as_of_provenance(tmp_path):
    files = write_inputs(tmp_path, [], as_of="2026-11-07T18:00:00Z")
    manifest = json.loads(files[1].read_text(encoding="utf-8"))
    manifest["started_at_utc"] = "2026-11-07T18:30:00Z"
    manifest["finished_at_utc"] = "2026-11-07T18:45:00Z"
    write_json(files[1], manifest)

    result = create_snapshot(tmp_path / "store", *files[:3], dependencies_path=files[3])
    snapshot = json.loads(Path(result["snapshot_path"]).read_text(encoding="utf-8"))
    assert "state" not in snapshot["run_provenance"]
    assert snapshot["capture_metadata"]["temporal_scope"] == "snapshot_capture_context_not_historical_evidence"
    assert snapshot["capture_metadata"]["upstream_run_state"] == "completed_bounded"
    assert snapshot["capture_metadata"]["upstream_run_manifest_sha256"]
    assert "upstream_run_id" not in snapshot


def test_as_of_snapshot_excludes_ledger_records_not_yet_known(tmp_path):
    old = ledger_record("old", "2026-11-07T16:00:00Z")
    future = ledger_record("future", "2026-11-07T19:00:00Z")
    files = write_inputs(tmp_path, [old, future], as_of="2026-11-07T18:00:00Z")
    result = create_snapshot(tmp_path / "store", *files[:3], dependencies_path=files[3])
    snapshot = json.loads(Path(result["snapshot_path"]).read_text(encoding="utf-8"))
    assert [row["record_id"] for row in snapshot["ledger_records"]] == ["old"]
    assert snapshot["as_of_exclusion_counts"]["future_ledger_records"] == 1
    assert "https://example.test/future" not in Path(result["snapshot_path"]).read_text(encoding="utf-8")


def test_rejects_symlink_input_and_store_component(tmp_path):
    files = write_inputs(tmp_path, [])
    link = tmp_path / "spec-link.json"
    link.symlink_to(files[0])
    with pytest.raises(IncidentOpsError, match="symlink"):
        create_snapshot(tmp_path / "store", link, files[1], files[2], dependencies_path=files[3])

    store_target = tmp_path / "target"
    store_target.mkdir()
    store_link = tmp_path / "store-link"
    store_link.symlink_to(store_target, target_is_directory=True)
    with pytest.raises(IncidentOpsError, match="symlink"):
        create_snapshot(store_link, *files[:3], dependencies_path=files[3])


def test_rejects_path_traversal_incident_id(tmp_path):
    files = write_inputs(tmp_path, [])
    spec = json.loads(files[0].read_text(encoding="utf-8"))
    spec["incident_id"] = "../escape"
    write_json(files[0], spec)
    with pytest.raises(IncidentOpsError):
        create_snapshot(tmp_path / "store", *files[:3], dependencies_path=files[3])
    assert not (tmp_path / "escape").exists()


def test_cli_help_and_usage(capsys):
    assert main(["--help"]) == 0
    help_text = capsys.readouterr().out
    assert "snapshot" in help_text and "list" in help_text

    assert main(["snapshot", "--help"]) == 0
    assert "--store" in capsys.readouterr().out

