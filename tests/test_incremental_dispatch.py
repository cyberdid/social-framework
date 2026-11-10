from __future__ import annotations

import pytest

from src.analysis.incremental_dispatch import (
    IncrementalDispatchError,
    execute_recomputations,
    plan_recomputations,
)


def correction_snapshot():
    return {
        "incident_id": "synthetic_case",
        "run_id": "run-synthetic-corrected",
        "invalidation_plan": {
            "status": "planned_not_executed",
            "changed_node_ids": ["assessment-old", "assessment-correction"],
            "invalidated_node_ids": ["claim:synthetic-1", "report:synthetic-1"],
        },
        "dependency_edges": [
            {"from": "assessment-correction", "to": "claim:synthetic-1"},
            {"from": "claim:synthetic-1", "to": "report:synthetic-1"},
        ],
    }


def accepted_assessment(assessment_id, root_id):
    return {
        "assessment_id": assessment_id,
        "claim_id": "claim_1",
        "relation": "supports",
        "decision": "accepted",
        "assessment_source": "human_adjudication",
        "assessor_id": "synthetic-reviewer",
        "scope": {"event": "synthetic"},
        "scope_matches_claim": True,
        "evidence_id": f"evidence-{assessment_id}",
        "citation": {"passage_id": f"evidence-{assessment_id}", "start_offset": 0,
                     "end_offset": 1, "quote": "x"},
        "independent_root_id": root_id,
        "independence_status": "confirmed_independent",
        "asserted_values": {},
        "assessed_at_utc": "2026-11-08T10:00:00Z",
    }


def test_synthetic_correction_dispatches_allowlisted_claim_verification_deterministically():
    snapshot = correction_snapshot()
    bindings = [{
        "node_id": "claim:synthetic-1",
        "adapter_id": "claim_verification.aggregate.v1",
        "input_key": "claim-input",
    }]
    inputs = {"claim-input": {
        "claim": {"claim_id": "claim_1", "statement": "A synthetic claim",
                  "scope": {"event": "synthetic"}, "key_values": {}},
        "assessments": [accepted_assessment("assessment-correction", "root-a"),
                        accepted_assessment("assessment-independent", "root-b")],
        "as_of_utc": "2026-11-08T11:00:00Z",
        "search_complete": True,
        "minimum_independent_roots": 2,
        "supersedes_result_id": "verification-result-before-correction",
    }}

    plan = plan_recomputations(snapshot, bindings)
    first = execute_recomputations(plan, inputs)
    second = execute_recomputations(plan, inputs)

    assert [task["node_id"] for task in plan["tasks"]] == [
        "claim:synthetic-1", "report:synthetic-1"
    ]
    assert plan["tasks"][0]["status"] == "ready"
    assert plan["tasks"][1]["status"] == "not_dispatched"
    assert first["tasks"][0]["status"] == "complete"
    assert first["tasks"][0]["result"]["status"] == "supported_within_scope"
    assert first["tasks"][0]["result"]["supersedes"] == "verification-result-before-correction"
    assert first["tasks"][0]["output_sha256"] == second["tasks"][0]["output_sha256"]
    assert first["tasks"][1]["status"] == "not_dispatched"
    assert "not that source evidence is true or complete" in first["limitations"][3].lower()


def test_dispatch_blocks_task_when_invalidated_upstream_node_has_no_binding():
    snapshot = correction_snapshot()
    bindings = [{
        "node_id": "report:synthetic-1",
        "adapter_id": "source_lineage.v1",
        "input_key": "lineage-input",
    }]

    plan = plan_recomputations(snapshot, bindings)

    report_task = next(task for task in plan["tasks"] if task["node_id"] == "report:synthetic-1")
    assert report_task["status"] == "blocked_missing_dependency_binding"
    assert report_task["dependency_node_ids"] == ["claim:synthetic-1"]


def test_dispatch_rejects_untrusted_module_names_and_stale_bindings():
    snapshot = correction_snapshot()
    with pytest.raises(IncrementalDispatchError, match="not allowlisted"):
        plan_recomputations(snapshot, [{"node_id": "claim:synthetic-1",
                                       "adapter_id": "os.system", "input_key": "x"}])

    with pytest.raises(IncrementalDispatchError, match="not invalidated"):
        plan_recomputations(snapshot, [{"node_id": "source:unchanged",
                                       "adapter_id": "source_lineage.v1", "input_key": "x"}])


def test_dispatch_never_calls_adapter_when_input_is_missing():
    plan = plan_recomputations(correction_snapshot(), [{
        "node_id": "claim:synthetic-1",
        "adapter_id": "claim_verification.aggregate.v1",
        "input_key": "claim-input",
    }])

    receipt = execute_recomputations(plan, {})

    assert receipt["tasks"][0]["status"] == "failed"
    assert receipt["tasks"][0]["reason"] == "input_payload_missing"
    assert "input payload" not in str(receipt["tasks"][0]).lower()


def test_dispatch_plan_fingerprint_rejects_mutated_task_list():
    plan = plan_recomputations(correction_snapshot(), [{
        "node_id": "claim:synthetic-1",
        "adapter_id": "claim_verification.aggregate.v1",
        "input_key": "claim-input",
    }])
    plan["tasks"][0]["adapter_id"] = "os.system"

    with pytest.raises(IncrementalDispatchError, match="fingerprint"):
        execute_recomputations(plan, {})


def test_dispatch_rejects_cycle_in_invalidated_dependency_subgraph():
    snapshot = correction_snapshot()
    snapshot["dependency_edges"].append(
        {"from": "report:synthetic-1", "to": "claim:synthetic-1"}
    )

    with pytest.raises(IncrementalDispatchError, match="cycle"):
        plan_recomputations(snapshot, [])


@pytest.mark.parametrize(
    ("adapter_id", "payload"),
    [
        ("source_lineage.v1", {"records": [{"evidence_id": "e1", "claim_id": "c1", "text": "synthetic"}]}),
        ("event_reconstruction.timeline.v1", {"event_mentions": [{
            "event_mention_id": "m1", "event_kind": "physical", "predicate": "reported assembly",
            "evidence_ids": ["e1"],
        }]}),
        ("conflict_analysis.episode.v1", {"episode_id": "episode-synthetic",
            "physical_event_records": [], "position_records": [], "information_attention_records": []}),
        ("information_dynamics.summary.v1", {"posts": [{"post_id": "p1", "platform": "telegram",
            "channel_id": "channel-synthetic", "publication_time": "2026-11-08T10:00:00Z"}]}),
        ("uncertainty_scenarios.v1", {"records": [{"post_id": "p1", "channel_id": "channel-synthetic",
            "target_label_state": "unknown", "channel_group": None, "views_snapshot": 7}],
            "target_label": "narrative"}),
    ],
)
def test_every_fixed_adapter_dispatches_synthetic_typed_input(adapter_id, payload):
    snapshot = {
        "incident_id": "synthetic_case", "run_id": "run-synthetic-adapter-smoke",
        "invalidation_plan": {"status": "planned_not_executed", "changed_node_ids": ["changed"],
                              "invalidated_node_ids": ["output"]},
        "dependency_edges": [{"from": "changed", "to": "output"}],
    }
    plan = plan_recomputations(snapshot, [{"node_id": "output", "adapter_id": adapter_id,
                                           "input_key": "payload"}])

    receipt = execute_recomputations(plan, {"payload": payload})

    assert receipt["tasks"][0]["status"] == "complete"
    assert receipt["tasks"][0]["adapter_id"] == adapter_id
    assert len(receipt["tasks"][0]["output_sha256"]) == 64
