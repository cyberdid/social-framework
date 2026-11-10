from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.analysis.evaluation_harness import (
    evaluate_frozen_scenario,
    evaluate_run_bundle,
    run_frozen_suite,
    verify_report_bundle,
)
from src.analysis.event_reconstruction import reconstruct_timeline


FIXTURES = Path(__file__).parent / "fixtures" / "evaluation_harness"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_all_frozen_synthetic_and_adversarial_controls_pass():
    result = run_frozen_suite()
    assert result["status"] == "passed"
    assert result["scenario_count"] == 6
    assert result["failed_count"] == 0
    assert "not_evaluated" in result["semantic_evaluation"]


def test_known_microcase_reconstructs_separate_physical_timeline():
    result = evaluate_frozen_scenario(fixture("known_microcase.json"))
    assert result["status"] == "passed"


def test_thousand_copies_cannot_claim_two_independent_roots():
    scenario = fixture("mass_copy_fanout.json")
    scenario["verification_status"] = "supported_within_scope"
    scenario["minimum_independent_roots"] = 2
    result = evaluate_frozen_scenario(scenario)
    assert result["status"] == "failed"
    assert any(row["gate_id"] == "scenario.root_minimum" for row in result["failures"])


def test_conflicting_result_cannot_drop_one_evidence_side():
    scenario = fixture("contradictory_testimony.json")
    scenario["contradicting_evidence_ids"] = []
    result = evaluate_frozen_scenario(scenario)
    assert result["status"] == "failed"


def test_correction_after_cutoff_is_rejected_if_cited_in_as_of_view():
    scenario = fixture("corrected_statement_as_of.json")
    scenario["cited_evidence_ids"].append("ev-correction")
    result = evaluate_frozen_scenario(scenario)
    assert result["status"] == "failed"
    assert any(row["gate_id"] == "scenario.as_of_future_evidence" for row in result["failures"])


def test_missing_corpus_cannot_be_used_as_refutation():
    scenario = fixture("missing_corpus.json")
    scenario["verification_status"] = "refuted_within_scope"
    result = evaluate_frozen_scenario(scenario)
    assert result["status"] == "failed"


def test_verified_pre_incident_video_reuse_cannot_support_current_incident():
    scenario = fixture("reused_video_mismatch.json")
    scenario["claim_status"] = "supported_within_scope"
    scenario["supporting_evidence_ids"] = ["old-video"]
    result = evaluate_frozen_scenario(scenario)
    assert result["status"] == "failed"


def make_run_manifest(tmp_path: Path, *, outputs: list[dict] | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "run_id": "run-demo",
        "incident_id": "demo_case_2026",
        "state": "completed_bounded",
        "started_at_utc": "2026-11-01T00:00:00Z",
        "finished_at_utc": "2026-11-02T00:00:00Z",
        "producer": "evaluation-test",
        "git_commit": "abcdef0123456789",
        "policy_version": "test-policy-v1",
        "model_config": {},
        "source_snapshots": [],
        "task_receipts": [],
        "outputs": outputs or [],
    }


def make_report_context() -> dict:
    return {
        "incident_id": "demo_case_2026",
        "run_id": "run-demo",
        "as_of_utc": "2026-11-02T00:00:00Z",
        "working_title": "Synthetic test",
        "git_commit": "abcdef0123456789",
        "scope": {"description": "synthetic", "time_window": ["2026-11-01", "2026-11-02"]},
        "coverage": {"web": {"status": "partial"}},
    }


def test_release_gates_accept_valid_run_contract_and_mark_semantics_unmeasured(tmp_path: Path):
    incident = json.loads((Path(__file__).parent / "fixtures" / "incident_spec.valid.json").read_text())
    manifest = make_run_manifest(tmp_path)
    result = evaluate_run_bundle(run_manifest=manifest, incident_spec=incident,
                                 report_context=make_report_context(), findings=[], evidence_records=[],
                                 verification_results=[])
    assert result["hard_gate_status"] == "passed"
    assert result["semantic_evaluation"]["status"] == "not_evaluated"
    assert result["semantic_evaluation"]["unsupported_assertion_rate"] is None


def test_release_gates_check_output_hashes_and_artifact_root(tmp_path: Path):
    output = tmp_path / "findings.jsonl"
    output.write_text('{"finding_id":"f1"}\n', encoding="utf-8")
    manifest = make_run_manifest(tmp_path, outputs=[{"path": output.name,
                                                    "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}])
    result = evaluate_run_bundle(run_manifest=manifest, artifact_root=tmp_path)
    assert result["status"] == "passed"

    manifest["outputs"][0]["sha256"] = "0" * 64
    result = evaluate_run_bundle(run_manifest=manifest, artifact_root=tmp_path)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "manifest.output_sha256" for row in result["hard_gate_failures"])


def test_release_gates_reject_evidence_snapshots_captured_after_as_of(tmp_path: Path):
    incident = json.loads((Path(__file__).parent / "fixtures" / "incident_spec.valid.json").read_text())
    cutoff = incident["as_of_utc"]
    manifest = make_run_manifest(tmp_path)
    manifest["started_at_utc"] = cutoff
    manifest["finished_at_utc"] = cutoff
    evidence = [{
        "evidence_id": "ev-future", "source_id": "source-1", "artifact_id": "artifact-1",
        "captured_at_utc": "2026-11-03T00:00:00Z", "independent_root_id": "root-1",
    }]
    mentions = [{
        "event_mention_id": "event-1", "event_kind": "physical", "predicate": "заявлено",
        "actor_entity_ids": [], "object_entity_ids": [], "place_id": None,
        "time": None, "publication_time_utc": None, "evidence_ids": ["ev-future"],
    }]
    reconstruction = reconstruct_timeline(mentions, run_context={
        "incident_id": incident["incident_id"], "run_id": manifest["run_id"],
        "as_of_utc": cutoff, "git_commit": manifest["git_commit"],
        "parameters_sha256": "a" * 64,
    })
    result = evaluate_run_bundle(run_manifest=manifest, incident_spec=incident,
                                 evidence_records=evidence, event_reconstruction=reconstruction)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "events.evidence_as_of" for row in result["hard_gate_failures"])


def test_release_gates_reject_missing_reconstruction_as_of(tmp_path: Path):
    incident = json.loads((Path(__file__).parent / "fixtures" / "incident_spec.valid.json").read_text())
    manifest = make_run_manifest(tmp_path)
    reconstruction = reconstruct_timeline([], run_context={
        "incident_id": incident["incident_id"], "run_id": manifest["run_id"],
        "as_of_utc": incident["as_of_utc"], "git_commit": manifest["git_commit"],
        "parameters_sha256": "a" * 64,
    })
    reconstruction["as_of_utc"] = None
    result = evaluate_run_bundle(run_manifest=manifest, incident_spec=incident,
                                 event_reconstruction=reconstruction)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "events.as_of_required" for row in result["hard_gate_failures"])


def test_release_gates_apply_reconstruction_as_of_without_incident_spec(tmp_path: Path):
    manifest = make_run_manifest(tmp_path)
    reconstruction = reconstruct_timeline([{
        "event_mention_id": "event-1", "event_kind": "physical", "predicate": "заявлено",
        "actor_entity_ids": [], "object_entity_ids": [], "place_id": None,
        "time": None, "publication_time_utc": None, "evidence_ids": ["ev-future"],
    }], run_context={
        "incident_id": manifest["incident_id"], "run_id": manifest["run_id"],
        "as_of_utc": "2026-11-02T00:00:00Z", "git_commit": manifest["git_commit"],
        "parameters_sha256": "a" * 64,
    })
    evidence = [{"evidence_id": "ev-future", "captured_at_utc": "2026-11-03T00:00:00Z"}]
    result = evaluate_run_bundle(run_manifest=manifest, evidence_records=evidence,
                                 event_reconstruction=reconstruction)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "events.evidence_as_of" for row in result["hard_gate_failures"])


def test_release_gates_reject_dangling_verification_refs_without_report_context(tmp_path: Path):
    result = evaluate_run_bundle(
        run_manifest=make_run_manifest(tmp_path),
        verification_results=[{
            "verification_result_id": "vr1", "claim_id": "claim1",
            "status": "supported_within_scope", "supporting_evidence_ids": ["missing"],
            "contradicting_evidence_ids": [], "minimum_independent_roots": 1,
        }],
        evidence_records=[],
    )
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "verification.evidence_refs" for row in result["hard_gate_failures"])


def test_release_gates_reject_verification_evidence_newer_than_result_cutoff_without_report_context(tmp_path: Path):
    result = evaluate_run_bundle(
        run_manifest=make_run_manifest(tmp_path),
        evidence_records=[{
            "evidence_id": "ev-late", "captured_at_utc": "2026-11-06T00:00:00Z",
        }],
        verification_results=[{
            "verification_result_id": "vr-old", "claim_id": "claim1",
            "status": "insufficient_evidence", "as_of_utc": "2026-11-03T00:00:00Z",
            "supporting_evidence_ids": ["ev-late"], "contradicting_evidence_ids": [],
        }],
    )

    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "verification.evidence_after_as_of" for row in result["hard_gate_failures"])


def test_release_gates_require_verification_and_evidence_timestamps_without_report_context(tmp_path: Path):
    result = evaluate_run_bundle(
        run_manifest=make_run_manifest(tmp_path),
        evidence_records=[{"evidence_id": "ev-no-time"}],
        verification_results=[{
            "verification_result_id": "vr-no-time", "claim_id": "claim1",
            "status": "insufficient_evidence",
            "supporting_evidence_ids": ["ev-no-time"], "contradicting_evidence_ids": [],
        }],
    )

    assert result["hard_gate_status"] == "failed"
    gates = {row["gate_id"] for row in result["hard_gate_failures"]}
    assert "verification.as_of_utc" in gates
    assert "verification.evidence_capture_time_required" in gates


def test_release_gates_reject_affirmative_verification_without_auditable_roots(tmp_path: Path):
    text = "Окреме повідомлення."
    evidence = [{
        "evidence_id": "e1", "source_id": "source-1", "source_uri": "https://example.org/1",
        "artifact_id": "artifact-1", "artifact_sha256": "a" * 64,
        "captured_at_utc": "2026-11-01T12:00:00Z", "text": text,
        "locator": {"kind": "text_span", "start_offset": 0, "end_offset": len(text),
                    "offset_unit": "unicode_codepoint"},
    }]
    verification = [{
        "verification_result_id": "vr1", "claim_id": "claim1", "status": "supported_within_scope",
        "supporting_evidence_ids": ["e1"], "contradicting_evidence_ids": [],
        "minimum_independent_roots": 1,
    }]
    result = evaluate_run_bundle(run_manifest=make_run_manifest(tmp_path), evidence_records=evidence,
                                 verification_results=verification)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "verification.root_lineage_required" for row in result["hard_gate_failures"])


def test_release_gates_require_both_evidence_sides_for_conflicting_status(tmp_path: Path):
    result = evaluate_run_bundle(run_manifest=make_run_manifest(tmp_path), evidence_records=[],
                                 verification_results=[{
                                     "verification_result_id": "vr-conflict", "claim_id": "claim1",
                                     "status": "conflicting_evidence", "supporting_evidence_ids": [],
                                     "contradicting_evidence_ids": [],
                                 }])
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "verification.conflict_evidence_required" for row in result["hard_gate_failures"])


def test_release_gates_reject_whitespace_only_source_root_ids(tmp_path: Path):
    text = "Публікація."
    evidence = [{
        "evidence_id": "e1", "source_id": "source-1", "captured_at_utc": "2026-11-01T12:00:00Z",
        "independent_root_id": "   ", "text": text,
        "locator": {"kind": "text_span", "start_offset": 0, "end_offset": len(text),
                    "offset_unit": "unicode_codepoint"},
    }]
    verification = [{
        "verification_result_id": "vr1", "claim_id": "claim1", "status": "supported_within_scope",
        "supporting_evidence_ids": ["e1"], "contradicting_evidence_ids": [],
        "minimum_independent_roots": 1,
    }]
    result = evaluate_run_bundle(run_manifest=make_run_manifest(tmp_path), evidence_records=evidence,
                                 verification_results=verification)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "verification.root_lineage_required" for row in result["hard_gate_failures"])


def test_release_gates_report_invalid_minimum_roots_instead_of_crashing(tmp_path: Path):
    evidence = [{
        "evidence_id": "e1", "source_id": "source-1", "captured_at_utc": "2026-11-01T12:00:00Z",
        "independent_root_id": "root-1",
    }]
    verification = [{
        "verification_result_id": "vr1", "claim_id": "claim1", "status": "supported_within_scope",
        "supporting_evidence_ids": ["e1"], "contradicting_evidence_ids": [],
        "minimum_independent_roots": "2",
    }]
    result = evaluate_run_bundle(run_manifest=make_run_manifest(tmp_path), evidence_records=evidence,
                                 verification_results=verification)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "verification.minimum_roots" for row in result["hard_gate_failures"])


def test_release_gates_handle_malformed_verification_status_without_crashing(tmp_path: Path):
    result = evaluate_run_bundle(run_manifest=make_run_manifest(tmp_path), evidence_records=[],
                                 verification_results=[{"claim_id": "claim1", "status": []}])
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "verification.status_invalid" for row in result["hard_gate_failures"])


def test_release_gates_report_malformed_bundle_arrays_without_crashing(tmp_path: Path):
    result = evaluate_run_bundle(run_manifest=make_run_manifest(tmp_path), evidence_records=3,
                                 verification_results=[])  # type: ignore[arg-type]
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "bundle.evidence_records_list" for row in result["hard_gate_failures"])


def test_release_gates_verify_source_snapshot_bytes_when_artifact_root_is_given(tmp_path: Path):
    snapshot = tmp_path / "snapshot.jsonl"
    snapshot.write_text('{"id":"s1"}\n', encoding="utf-8")
    manifest = make_run_manifest(tmp_path)
    manifest["source_snapshots"] = [{
        "snapshot_id": "s1", "sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        "access_status": "complete", "fetched_at_utc": "2026-11-01T12:00:00Z",
        "snapshot_artifact_path": snapshot.name,
    }]
    result = evaluate_run_bundle(run_manifest=manifest, artifact_root=tmp_path)
    assert result["hard_gate_status"] == "passed"

    snapshot.write_text('{"id":"tampered"}\n', encoding="utf-8")
    result = evaluate_run_bundle(run_manifest=manifest, artifact_root=tmp_path)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "manifest.snapshot_sha256" for row in result["hard_gate_failures"])


def test_release_gates_require_snapshot_paths_for_hash_validation(tmp_path: Path):
    manifest = make_run_manifest(tmp_path)
    manifest["source_snapshots"] = [{
        "snapshot_id": "s1", "sha256": "a" * 64, "access_status": "complete",
    }]
    result = evaluate_run_bundle(run_manifest=manifest, artifact_root=tmp_path)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "manifest.snapshot_path_required" for row in result["hard_gate_failures"])


def test_release_gates_convert_nul_output_path_to_hard_gate_failure(tmp_path: Path):
    manifest = make_run_manifest(tmp_path, outputs=[{"path": "bad\x00path", "sha256": "a" * 64}])
    result = evaluate_run_bundle(run_manifest=manifest, artifact_root=tmp_path)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "manifest.output_exists" for row in result["hard_gate_failures"])


def test_report_context_must_match_run_identity_and_as_of(tmp_path: Path):
    incident = json.loads((Path(__file__).parent / "fixtures" / "incident_spec.valid.json").read_text())
    context = make_report_context()
    context["run_id"] = "different-run"
    result = evaluate_run_bundle(run_manifest=make_run_manifest(tmp_path), incident_spec=incident,
                                 report_context=context, findings=[], evidence_records=[], verification_results=[])
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "report.run_match" for row in result["hard_gate_failures"])


def test_hard_gate_recomputes_explicitly_duplicated_source_roots(tmp_path: Path):
    incident = json.loads((Path(__file__).parent / "fixtures" / "incident_spec.valid.json").read_text())
    scope = make_report_context()["scope"]
    evidence = []
    for evidence_id in ("e1", "e2"):
        text = f"Повідомлення {evidence_id}."
        evidence.append({
            "evidence_id": evidence_id,
            "source_id": f"source-{evidence_id}",
            "source_uri": f"https://example.org/{evidence_id}",
            "artifact_id": f"artifact-{evidence_id}",
            "artifact_sha256": "a" * 64,
            "captured_at_utc": "2026-11-01T12:00:00Z",
            "text": text,
            "locator": {"kind": "text_span", "start_offset": 0, "end_offset": len(text),
                        "offset_unit": "unicode_codepoint"},
            "independent_root_id": "same-origin-root",
        })
    verification = [{
        "verification_result_id": "vr1", "claim_id": "claim1", "status": "supported_within_scope",
        "scope": scope, "as_of_utc": "2026-11-02T00:00:00Z",
        "supporting_evidence_ids": ["e1", "e2"], "contradicting_evidence_ids": [],
        "supporting_root_count": 2, "contradicting_root_count": 0,
        "minimum_independent_roots": 2,
    }]
    result = evaluate_run_bundle(run_manifest=make_run_manifest(tmp_path), incident_spec=incident,
                                 report_context=make_report_context(), findings=[], evidence_records=evidence,
                                 verification_results=verification)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "verification.root_count" for row in result["hard_gate_failures"])


def test_incomplete_run_state_cannot_pass_release_gate(tmp_path: Path):
    manifest = make_run_manifest(tmp_path)
    manifest["state"] = "verifying"
    result = evaluate_run_bundle(run_manifest=manifest)
    assert result["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "run.release_state" for row in result["hard_gate_failures"])


def test_malformed_bundle_returns_gates_instead_of_crashing():
    result = evaluate_run_bundle(run_manifest={"source_snapshots": None, "task_receipts": None, "outputs": None},
                                 incident_spec=[])
    assert result["hard_gate_status"] == "failed"
    assert result["hard_gate_failures"]


def test_report_bundle_hash_tampering_is_detected(tmp_path: Path):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    receipt = {"output_sha256": {"incident_report.md": "0" * 64}}
    (report_dir / "run_receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    (report_dir / "incident_report.md").write_text("changed", encoding="utf-8")
    result = verify_report_bundle(report_dir)
    assert result["status"] == "failed"
    assert any(row["gate_id"] == "report.output_sha256" for row in result["issues"])
