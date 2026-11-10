from __future__ import annotations

import pytest

from src.analysis.conflict_analysis import analyze_conflict_episode


def event(
    event_id: str,
    *,
    category: str = "assembly",
    evidence_ids: list[str] | None = None,
    status: str = "reported",
    time: str = "2026-11-02T16:00:00Z",
) -> dict:
    return {
        "episode_id": "episode_1",
        "event_id": event_id,
        "event_kind": "physical",
        "action_category": category,
        "status": status,
        "time_utc": time,
        "evidence_ids": evidence_ids if evidence_ids is not None else [f"evidence_{event_id}"],
        "limitations": ["source reports this action"],
    }


def position(
    statement_id: str,
    *,
    category: str = "public_demand",
    speaker_id: str = "speaker_1",
    role: str = "resident",
    evidence_ids: list[str] | None = None,
    represented_group: str | None = None,
    representation_evidence_ids: list[str] | None = None,
) -> dict:
    return {
        "episode_id": "episode_1",
        "statement_id": statement_id,
        "category": category,
        "text": "Source-attributed statement",
        "speaker_entity_id": speaker_id,
        "speaker_role": role,
        "source_id": f"source_{statement_id}",
        "evidence_ids": evidence_ids if evidence_ids is not None else [f"evidence_{statement_id}"],
        "represented_group_id": represented_group,
        "representation_evidence_ids": representation_evidence_ids or [],
        "status": "reported",
        "limitations": ["reported position; not a population estimate"],
    }


def test_information_volume_does_not_change_physical_escalation_vector():
    physical = [event("one", category="assembly")]
    low = [{"interval_start_utc": "2026-11-02T16:00:00Z", "interval_end_utc": "2026-11-02T17:00:00Z", "source_id": "snapshot_1", "source_refs": ["snapshot_1"], "metrics": {"posts": 10, "view_counters": 20}}]
    high = [{"interval_start_utc": "2026-11-02T16:00:00Z", "interval_end_utc": "2026-11-02T17:00:00Z", "source_id": "snapshot_1", "source_refs": ["snapshot_1"], "metrics": {"posts": 1010, "view_counters": 100000}}]

    low_result = analyze_conflict_episode("episode_1", physical, [], low)
    high_result = analyze_conflict_episode("episode_1", physical, [], high)

    assert low_result["escalation_indicators"] == high_result["escalation_indicators"]
    assert high_result["information_attention_series"][0]["metrics"]["posts"] == 1010
    assert high_result["physical_event_series"][0]["event_id"] == "one"


def test_single_speaker_demand_is_not_promoted_to_group_position():
    result = analyze_conflict_episode("episode_1", [], [position("demand_1", represented_group="residents")], [])
    demand = result["public_demands"][0]

    assert demand["speaker_entity_id"] == "speaker_1"
    assert demand["represented_group_id"] == "residents"
    assert demand["group_representation_status"] == "unestablished"
    assert "group_position" not in demand


def test_representation_evidence_is_preserved_without_claiming_independent_validation():
    result = analyze_conflict_episode(
        "episode_1",
        [],
        [position("demand_2", represented_group="residents", representation_evidence_ids=["meeting_minutes_1"])],
        [],
    )
    demand = result["public_demands"][0]
    assert demand["representation_evidence_ids"] == ["meeting_minutes_1"]
    assert demand["group_representation_status"] == "evidence_recorded_not_independently_validated"
    assert "meeting_minutes_1" in demand["source_refs"]


def test_unknown_participant_role_remains_unknown():
    result = analyze_conflict_episode(
        "episode_1", [], [position("role_1", category="participant_role", role="unknown")], []
    )
    assert result["participants_and_roles"][0]["speaker_role"] == "unknown"
    assert result["participants_and_roles"][0]["role_status"] == "unknown"


def test_records_without_evidence_are_unresolved_and_not_used_as_actions_or_demands():
    result = analyze_conflict_episode(
        "episode_1",
        [event("missing_event", evidence_ids=[])],
        [position("missing_statement", evidence_ids=[])],
        [],
    )
    assert result["physical_event_series"] == []
    assert result["public_demands"] == []
    assert {item["record_id"] for item in result["unresolved"]} == {"missing_event", "missing_statement"}


def test_statement_categories_remain_distinct_and_hypotheses_are_marked():
    records = [
        position("grievance", category="stated_grievance"),
        position("hypothesis", category="structural_hypothesis"),
        position("interpretation", category="information_interpretation"),
    ]
    result = analyze_conflict_episode("episode_1", [], records, [])
    assert len(result["stated_grievances"]) == 1
    assert result["structural_hypotheses"][0]["epistemic_status"] == "hypothesis"
    assert len(result["information_interpretations"]) == 1


def test_unrelated_episode_records_are_rejected():
    with __import__("pytest").raises(ValueError, match="episode_id"):
        analyze_conflict_episode("episode_1", [event("wrong") | {"episode_id": "episode_2"}], [], [])


def test_non_physical_event_cannot_enter_physical_series():
    with __import__("pytest").raises(ValueError, match="physical_event_records"):
        analyze_conflict_episode("episode_1", [event("statement") | {"event_kind": "communication"}], [], [])


def test_information_attention_requires_source_receipt():
    row = {
        "interval_start_utc": "2026-11-02T16:00:00Z",
        "interval_end_utc": "2026-11-02T17:00:00Z",
        "metrics": {"posts": 12},
    }
    with pytest.raises(ValueError, match="source_refs"):
        analyze_conflict_episode("episode_1", [], [], [row])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_information_attention_rejects_non_finite_metrics(value: float):
    row = {
        "interval_start_utc": "2026-11-02T16:00:00Z",
        "interval_end_utc": "2026-11-02T17:00:00Z",
        "source_refs": ["snapshot_1"],
        "metrics": {"view_counters": value},
    }
    with pytest.raises(ValueError, match="finite"):
        analyze_conflict_episode("episode_1", [], [], [row])


def test_physical_and_observable_action_lists_do_not_alias_mutable_records():
    result = analyze_conflict_episode("episode_1", [event("one")], [], [])

    assert result["physical_event_series"] is not result["observable_actions"]
    result["observable_actions"].append({"event_id": "extra"})
    assert len(result["physical_event_series"]) == 1
    result["physical_event_series"][0]["status"] = "changed"
    assert result["observable_actions"][0]["status"] == "reported"
