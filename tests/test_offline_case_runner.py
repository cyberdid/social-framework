from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.analysis.offline_case_runner import run_offline_case


FIXTURES = Path(__file__).parent / "fixtures"


def _spec() -> dict:
    return json.loads((FIXTURES / "incident_spec.valid.json").read_text(encoding="utf-8"))


def _jsonl(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return path


def _report_input(path: Path, spec: dict) -> Path:
    quote = "Поліція оприлюднила заяву."
    evidence = {
        "evidence_id": "evidence-1",
        "source_id": "source-1",
        "source_title": "Синтетичне джерело",
        "source_uri": "https://example.org/report",
        "artifact_id": "artifact-1",
        "artifact_sha256": hashlib.sha256(b"synthetic source bytes").hexdigest(),
        "captured_at_utc": "2026-11-01T20:00:00Z",
        "text": quote,
        "locator": {"kind": "text_span", "start_offset": 0, "end_offset": len(quote),
                    "offset_unit": "unicode_codepoint", "quote": quote},
        "independent_root_id": "root-1",
    }
    scope = spec["scope"]
    return Path(path).write_text(json.dumps({
        "findings": [{
            "finding_id": "finding-1", "kind": "supported_within_scope", "claim_id": "claim-report",
            "statement": quote, "scope": scope, "verification_result_id": "vr-1",
            "evidence_refs": [{"evidence_id": "evidence-1", "role": "supporting"}],
        }],
        "evidence_records": [evidence],
        "verification_results": [{
            "verification_result_id": "vr-1", "claim_id": "claim-report",
            "status": "supported_within_scope", "scope": scope,
            "as_of_utc": spec["as_of_utc"], "supporting_evidence_ids": ["evidence-1"],
            "contradicting_evidence_ids": [], "supporting_root_count": 1,
            "contradicting_root_count": 0, "minimum_independent_roots": 1,
        }],
    }, ensure_ascii=False), encoding="utf-8") and path


def test_mass_copy_lineage_does_not_create_independent_roots(tmp_path: Path):
    rows = [{
        "evidence_id": f"copy-{index}", "claim_id": "claim-1", "source_id": f"source-{index}",
        "text": "same copied statement", "media_sha256": "a" * 64,
    } for index in range(100)]
    result = run_offline_case(
        _spec(), tmp_path / "run", git_commit="abcdef0123456",
        inputs={"lineage_records": _jsonl(tmp_path / "lineage.jsonl", rows)}, execute=True,
    )
    lineage = json.loads((tmp_path / "run" / "source_lineage.json").read_text())
    summary = lineage["claim_summaries"]["claim-1"]
    assert summary["evidence_count"] == 100
    assert summary["confirmed_dependency_family_count"] == 1
    assert summary["independent_roots_lower_bound"] == 0
    assert result["analysis_status"] == "partial"


def test_retrieved_conflicting_passages_remain_unverified_without_adjudication(tmp_path: Path):
    spec = _spec()
    spec["budgets"]["max_queries_per_claim"] = 1
    claims = [{"claim_id": "claim-1", "statement": "людей утримували у приміщенні", "scope": {"event": "x"}}]
    passages = [
        {"passage_id": "p1", "source_id": "s1", "captured_at_utc": "2026-11-01T20:00:00Z",
         "text": "Людей утримували у приміщенні."},
        {"passage_id": "p2", "source_id": "s2", "captured_at_utc": "2026-11-01T21:00:00Z",
         "text": "Спростування: людей не утримували у приміщенні."},
    ]
    run_offline_case(spec, tmp_path / "run", git_commit="abcdef0123456", inputs={
        "claims": _jsonl(tmp_path / "claims.jsonl", claims),
        "passages": _jsonl(tmp_path / "passages.jsonl", passages),
    }, execute=True)
    results = [json.loads(line) for line in (tmp_path / "run" / "verification_results.jsonl").read_text().splitlines()]
    queue = [json.loads(line) for line in (tmp_path / "run" / "unresolved_queue.jsonl").read_text().splitlines()]
    assert results[0]["status"] == "unverified"
    assert results[0]["supporting_evidence_ids"] == []
    assert results[0]["contradicting_evidence_ids"] == []
    assert any(row.get("claim_id") == "claim-1" and row["status"] == "unverified" for row in queue)


def test_zero_query_budget_skips_retrieval_and_records_unresolved_reason(tmp_path: Path, monkeypatch):
    import src.analysis.offline_case_runner as runner

    calls = []
    retrieve = runner.retrieve_evidence_candidates

    def track_retrieval(*args, **kwargs):
        calls.append(args[0]["claim_id"])
        return retrieve(*args, **kwargs)

    monkeypatch.setattr(runner, "retrieve_evidence_candidates", track_retrieval)
    claims = [{"claim_id": "claim-1", "statement": "local statement", "scope": {"event": "x"}}]
    passages = [{"passage_id": "p1", "source_id": "s1", "captured_at_utc": "2026-11-01T20:00:00Z",
                 "text": "local statement"}]
    run_offline_case(_spec(), tmp_path / "run", git_commit="abcdef0123456", inputs={
        "claims": _jsonl(tmp_path / "claims.jsonl", claims),
        "passages": _jsonl(tmp_path / "passages.jsonl", passages),
        "event_mentions": _jsonl(tmp_path / "events.jsonl", []),
    }, execute=True)
    assert calls == []
    assert (tmp_path / "run" / "evidence_candidates.jsonl").read_text() == ""
    assert (tmp_path / "run" / "verification_results.jsonl").read_text() == ""
    receipts = [json.loads(line) for line in (tmp_path / "run" / "task_receipts.jsonl").read_text().splitlines()]
    receipt = next(row for row in receipts if row["task_id"] == "claim_retrieval")
    assert receipt["status"] == "not_run"
    assert receipt["reason"] == "query_budget_zero"
    assert receipt["executed"] is False
    assert next(row for row in receipts if row["task_id"] == "event_reconstruction")["status"] == "complete"
    manifest = json.loads((tmp_path / "run" / "input_manifest.json").read_text())
    assert manifest["inputs"]["claims"]["record_count"] == 1
    assert manifest["inputs"]["passages"]["record_count"] == 1
    for name in ("claims", "passages"):
        assert manifest["inputs"][name]["sha256"] == hashlib.sha256(
            (tmp_path / f"{name}.jsonl").read_bytes()).hexdigest()
    queue = [json.loads(line) for line in (tmp_path / "run" / "unresolved_queue.jsonl").read_text().splitlines()]
    assert any(row["stage"] == "claim_retrieval" and row["status"] == "not_run"
               and row["reason"] == "query_budget_zero" for row in queue)


@pytest.mark.parametrize("budget", [1, 2, 3])
def test_positive_query_budget_runs_once_per_claim_without_limiting_candidate_count(tmp_path: Path, monkeypatch, budget: int):
    import src.analysis.offline_case_runner as runner

    spec = _spec()
    spec["budgets"]["max_queries_per_claim"] = budget
    calls = []
    retrieve = runner.retrieve_evidence_candidates

    def track_retrieval(*args, **kwargs):
        calls.append(args[0]["claim_id"])
        return retrieve(*args, **kwargs)

    monkeypatch.setattr(runner, "retrieve_evidence_candidates", track_retrieval)
    claims = [{"claim_id": f"claim-{index}", "statement": "local statement", "scope": {"event": "x"}}
              for index in range(2)]
    passages = [{"passage_id": f"p{index}", "source_id": f"s{index}",
                 "captured_at_utc": "2026-11-01T20:00:00Z", "text": "local statement"}
                for index in range(25)]
    run_offline_case(spec, tmp_path / "run", git_commit="abcdef0123456", inputs={
        "claims": _jsonl(tmp_path / "claims.jsonl", claims),
        "passages": _jsonl(tmp_path / "passages.jsonl", passages),
    }, execute=True)
    assert calls == ["claim-0", "claim-1"]
    candidates = [json.loads(line) for line in (tmp_path / "run" / "evidence_candidates.jsonl").read_text().splitlines()]
    assert len(candidates) == 2
    assert all(len(row["candidates"]) == 20 for row in candidates)
    assert all(row["search_receipt"]["top_k_per_direction"] == 20 for row in candidates)
    assert all(row["search_receipt"]["executed_query_count"] == min(budget, 2) for row in candidates)
    assert all(row["search_receipt"]["search_truncated"] is (budget < 2) for row in candidates)
    assert all(candidate["relation"] == "unassessed" for row in candidates for candidate in row["candidates"])
    verification = [json.loads(line) for line in (tmp_path / "run" / "verification_results.jsonl").read_text().splitlines()]
    assert len(verification) == 2
    assert all(row["status"] == "unverified" and row["supporting_evidence_ids"] == []
               and row["contradicting_evidence_ids"] == [] for row in verification)


def test_event_reconstruction_keeps_physical_event_time_separate_from_communication_time(tmp_path: Path):
    mentions = [
        {"event_mention_id": "physical", "event_kind": "physical", "predicate": "заявлена дія",
         "actor_entity_ids": ["actor-1"], "object_entity_ids": [], "place_id": "place-1",
         "time": {"kind": "claimed_event_time", "earliest_utc": "2026-11-01T10:00:00Z",
                  "latest_utc": "2026-11-01T10:05:00Z", "precision": "interval",
                  "anchor_evidence_ids": ["e1"]},
         "publication_time_utc": "2026-11-01T10:30:00Z", "evidence_ids": ["e1"]},
        {"event_mention_id": "statement", "event_kind": "communication", "predicate": "оприлюднено заяву",
         "actor_entity_ids": ["actor-2"], "object_entity_ids": [], "place_id": None,
         "time": None, "publication_time_utc": "2026-11-01T10:45:00Z", "evidence_ids": ["e2"]},
    ]
    run_offline_case(_spec(), tmp_path / "run", git_commit="abcdef0123456", inputs={
        "event_mentions": _jsonl(tmp_path / "events.jsonl", mentions),
    }, execute=True)
    output = json.loads((tmp_path / "run" / "event_reconstruction.json").read_text())
    physical = output["timelines"]["physical"][0]
    communication = output["timelines"]["communication"][0]
    assert physical["earliest_utc"] == "2026-11-01T10:00:00Z"
    assert physical["time_kind"] == "event_time"
    assert communication["earliest_utc"] == "2026-11-01T10:45:00Z"
    assert communication["time_kind"] == "publication_time"


def test_report_bundle_preserves_exact_citation_locator_and_output_hashes(tmp_path: Path):
    spec = _spec()
    report_input = _report_input(tmp_path / "report-input.json", spec)
    run_offline_case(spec, tmp_path / "run", git_commit="abcdef0123456",
                     inputs={"report_bundle": report_input}, execute=True)
    report_dir = tmp_path / "run" / "report"
    receipt = json.loads((report_dir / "run_receipt.json").read_text())
    report_bytes = (report_dir / "incident_report.md").read_bytes()
    findings = [json.loads(line) for line in (report_dir / "findings.jsonl").read_text().splitlines()]
    citation = findings[0]["citations"][0]
    assert receipt["output_sha256"]["incident_report.md"] == hashlib.sha256(report_bytes).hexdigest()
    assert citation["locator"]["quote"] == "Поліція оприлюднила заяву."
    assert citation["artifact_sha256"] == hashlib.sha256(b"synthetic source bytes").hexdigest()
    evaluation = json.loads((tmp_path / "run" / "evaluation.json").read_text())
    assert evaluation["hard_gate_status"] == "passed"
    assert evaluation["semantic_evaluation"]["status"] == "not_evaluated"
    manifest = json.loads((tmp_path / "run" / "run_manifest.json").read_text())
    assert any(row["path"] == "report/incident_report.md" and
               row["sha256"] == hashlib.sha256(report_bytes).hexdigest() for row in manifest["outputs"])


def test_conflict_information_and_uncertainty_modules_are_composed_without_mixing_metrics(tmp_path: Path):
    episode = "episode-1"
    conflict = {
        "episode_id": episode,
        "physical_event_records": [{
            "event_id": "event-1", "episode_id": episode, "event_kind": "physical",
            "action_category": "assembly", "status": "reported", "evidence_ids": ["ev-1"],
        }],
        "position_records": [{
            "statement_id": "statement-1", "episode_id": episode, "category": "stated_grievance",
            "text": "Демонстраційна вимога мовця.", "evidence_ids": ["ev-2"],
        }],
        "information_attention_records": [{
            "episode_id": episode, "interval_start_utc": "2026-11-01T10:00:00Z",
            "interval_end_utc": "2026-11-01T11:00:00Z", "source_refs": ["post-1"],
            "metrics": {"post_count": 25},
        }],
    }
    information_posts = [{
        "post_id": "post-1", "platform": "telegram", "channel_id": "channel-1",
        "publication_time": "2026-11-01T10:15:00Z", "observed_at": "2026-11-01T10:20:00Z",
        "view_count_snapshot": 25,
    }]
    uncertainty_records = [{
        "post_id": "post-1", "channel_id": "channel-1", "target_label_state": "unknown",
        "channel_group": None, "views_snapshot": 25,
    }]
    conflict_path = tmp_path / "conflict.json"
    conflict_path.write_text(json.dumps(conflict), encoding="utf-8")
    run_offline_case(_spec(), tmp_path / "run", git_commit="abcdef0123456", inputs={
        "conflict_input": conflict_path,
        "information_posts": _jsonl(tmp_path / "posts.jsonl", information_posts),
        "uncertainty_records": _jsonl(tmp_path / "uncertainty.jsonl", uncertainty_records),
    }, execute=True)
    conflict_output = json.loads((tmp_path / "run" / "conflict_analysis.json").read_text())
    information_output = json.loads((tmp_path / "run" / "information_dynamics.json").read_text())
    uncertainty_output = json.loads((tmp_path / "run" / "uncertainty_scenarios.json").read_text())
    assert conflict_output["escalation_indicators"]["assembly"]["event_record_count"] == 1
    assert len(conflict_output["information_attention_series"]) == 1
    assert information_output["platform_summary"]["telegram"]["unique_post_count"] == 1
    assert uncertainty_output["sample_design"] == "targeted_nonprobability_panel_unless_separately_documented"


def test_dry_run_records_frozen_suite_failure_as_failed(monkeypatch, tmp_path: Path):
    import src.analysis.offline_case_runner as runner

    monkeypatch.setattr(runner, "run_frozen_suite", lambda: {
        "status": "failed", "hard_gate_status": "failed", "scenarios": [],
    })
    result = run_offline_case(_spec(), tmp_path / "run", git_commit="abcdef0123456",
                             inputs={}, execute=False)
    summary = json.loads((tmp_path / "run" / "case_summary.json").read_text())
    receipts = [json.loads(line) for line in (tmp_path / "run" / "task_receipts.jsonl").read_text().splitlines()]
    assert result["analysis_status"] == "failed"
    assert summary["stage_status"]["evaluation"] == "failed"
    assert next(row for row in receipts if row["task_id"] == "evaluation")["status"] == "failed"


def test_future_observation_is_rejected_from_as_of_information_run(tmp_path: Path):
    spec = _spec()
    posts = [{
        "post_id": "post-future", "platform": "telegram", "channel_id": "channel-1",
        "publication_time": "2026-11-01T10:15:00Z", "observed_at": "2026-11-02T00:01:00Z",
        "view_count_snapshot": 25, "view_count_observed_at": "2026-11-02T00:01:00Z",
    }]
    result = run_offline_case(spec, tmp_path / "run", git_commit="abcdef0123456", inputs={
        "information_posts": _jsonl(tmp_path / "posts.jsonl", posts),
    }, execute=True)
    summary = json.loads((tmp_path / "run" / "case_summary.json").read_text())
    queue = [json.loads(line) for line in (tmp_path / "run" / "unresolved_queue.jsonl").read_text().splitlines()]
    assert result["analysis_status"] == "failed"
    assert summary["stage_status"]["information_dynamics"] == "failed"
    assert not (tmp_path / "run" / "information_dynamics.json").exists()
    assert any(row.get("stage") == "information_dynamics" and row.get("reason") == "time_scope_contract_mismatch"
               for row in queue)


def test_publication_outside_incident_window_is_rejected(tmp_path: Path):
    spec = _spec()
    posts = [{
        "post_id": "post-outside", "platform": "telegram", "channel_id": "channel-1",
        "publication_time": "2026-10-31T23:59:00Z", "observed_at": "2026-11-01T00:10:00Z",
    }]
    result = run_offline_case(spec, tmp_path / "run", git_commit="abcdef0123456", inputs={
        "information_posts": _jsonl(tmp_path / "posts.jsonl", posts),
    }, execute=True)
    summary = json.loads((tmp_path / "run" / "case_summary.json").read_text())
    assert result["analysis_status"] == "failed"
    assert summary["stage_status"]["information_dynamics"] == "failed"
    assert not (tmp_path / "run" / "information_dynamics.json").exists()


def test_evaluation_exception_is_recorded_and_finalized(tmp_path: Path, monkeypatch):
    import src.analysis.offline_case_runner as runner

    def raise_evaluation():
        raise RuntimeError("synthetic harness error")

    monkeypatch.setattr(runner, "run_frozen_suite", raise_evaluation)
    posts = [{
        "post_id": "post-1", "platform": "telegram", "channel_id": "channel-1",
        "publication_time": "2026-11-01T10:15:00Z", "observed_at": "2026-11-01T10:20:00Z",
        "view_count_snapshot": 25,
    }]
    result = run_offline_case(_spec(), tmp_path / "run", git_commit="abcdef0123456", inputs={
        "information_posts": _jsonl(tmp_path / "posts.jsonl", posts),
    }, execute=True)
    summary = json.loads((tmp_path / "run" / "case_summary.json").read_text())
    evaluation = json.loads((tmp_path / "run" / "evaluation.json").read_text())
    receipts = [json.loads(line) for line in (tmp_path / "run" / "task_receipts.jsonl").read_text().splitlines()]
    manifest = json.loads((tmp_path / "run" / "run_manifest.json").read_text())
    assert result["analysis_status"] == "failed"
    assert summary["stage_status"]["evaluation"] == "failed"
    assert evaluation["error_type"] == "RuntimeError"
    assert next(row for row in receipts if row["task_id"] == "evaluation")["status"] == "failed"
    assert manifest["finished_at_utc"] is not None


def test_dry_run_does_not_execute_analytical_stages_and_existing_output_is_refused(tmp_path: Path):
    source = _jsonl(tmp_path / "events.jsonl", [])
    result = run_offline_case(_spec(), tmp_path / "run", git_commit="abcdef0123456",
                             inputs={"event_mentions": source}, execute=False)
    assert result["analysis_status"] == "not_run"
    assert not (tmp_path / "run" / "event_reconstruction.json").exists()
    try:
        run_offline_case(_spec(), tmp_path / "run", git_commit="abcdef0123456", inputs={}, execute=True)
    except FileExistsError:
        pass
    else:
        raise AssertionError("existing run directory should be refused")
