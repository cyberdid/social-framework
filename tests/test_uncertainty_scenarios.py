from __future__ import annotations

import json

import pytest

from src.analysis.uncertainty_scenarios import analyze_uncertainty_scenarios, main


def rows() -> list[dict]:
    return [
        {"post_id": "p1", "channel_id": "large", "target_label_state": "target",
         "channel_group": "C", "views_snapshot": 900, "source_family_id": "family-a",
         "lineage_status": "confirmed_dependency_family"},
        {"post_id": "p2", "channel_id": "large", "target_label_state": "unknown",
         "channel_group": "C", "views_snapshot": 100, "lineage_status": "unknown"},
        {"post_id": "p3", "channel_id": "small", "target_label_state": "other",
         "channel_group": "B", "views_snapshot": 50, "source_family_id": "family-b",
         "lineage_status": "confirmed_dependency_family"},
        {"post_id": "p4", "channel_id": "uncertain", "target_label_state": "target",
         "channel_group": None, "views_snapshot": 50, "lineage_status": "candidate"},
    ]


def test_reports_unknown_label_bounds_without_population_ci():
    result = analyze_uncertainty_scenarios(rows(), target_label="N3")
    bounds = result["observed_corpus_bounds"]
    assert bounds["target_post_share_lower"] == 0.5
    assert bounds["target_post_share_upper"] == 0.75
    assert bounds["interval_type"].endswith("not_confidence_interval")
    assert result["sample_design"].startswith("targeted_nonprobability")


def test_group_and_unknown_channel_scenarios_are_separate():
    result = analyze_uncertainty_scenarios(rows(), target_label="N3", target_group="C", top_k=(0,))
    by_id = {scenario["scenario_id"]: scenario for scenario in result["scenarios"]}
    strict = by_id["unknown_group_excluded__unknown_labels_as_non_target__exclude_top_0"]
    include_unknown = by_id["unknown_group_as_target__unknown_labels_as_non_target__exclude_top_0"]
    assert strict["metrics"]["post_count"] == 2
    assert include_unknown["metrics"]["post_count"] == 3
    assert strict["metrics"]["target_post_count"] == 1
    assert include_unknown["metrics"]["target_post_count"] == 2


def test_top_k_sensitivity_excludes_by_explicit_counter_basis():
    result = analyze_uncertainty_scenarios(rows(), target_label="N3", top_k=(0, 1), top_k_basis="views_snapshot")
    all_rows = next(item for item in result["scenarios"]
                    if item["scenario_id"] == "all_rows__unknown_labels_as_non_target__exclude_top_0")
    excl = next(item for item in result["scenarios"]
                if item["scenario_id"] == "all_rows__unknown_labels_as_non_target__exclude_top_1")
    assert excl["excluded_channel_ids"] == ["large"]
    assert excl["metrics"]["post_count"] == all_rows["metrics"]["post_count"] - 2


def test_lineage_keeps_dependency_families_distinct_from_independent_roots():
    result = analyze_uncertainty_scenarios(rows(), target_label="N3")
    assert result["lineage"]["confirmed_dependency_family_count"] == 2
    assert result["lineage"]["independent_source_root_count"] is None
    assert result["lineage"]["unknown_lineage_record_count"] == 1
    assert "not independent" in result["lineage"]["interpretation"]


def test_coverage_bounds_are_explicit_scenarios_not_confidence_intervals():
    result = analyze_uncertainty_scenarios(
        rows(), target_label="N3",
        coverage_bounds={"missing_target_min": 0, "missing_target_max": 10,
                         "missing_non_target_min": 0, "missing_non_target_max": 20},
    )
    coverage = result["coverage_partial_identification"]
    assert coverage["interval_type"] == "scenario_partial_identification_bounds_not_confidence_interval"
    assert coverage["target_share_lower"] == pytest.approx(2 / 24)
    assert coverage["target_share_upper"] == pytest.approx(13 / 14)
    assert coverage["population_claim"] is False


def test_view_counter_metric_is_not_unique_audience():
    result = analyze_uncertainty_scenarios(rows(), target_label="N3")
    metrics = result["scenarios"][0]["metrics"]
    assert metrics["target_snapshot_view_counter_sum"] == 950
    assert metrics["all_snapshot_view_counter_sum"] == 1100
    assert metrics["view_counter_interpretation"] == "sum_of_platform_counter_snapshots_not_unique_people_or_reach"


def test_invalid_rows_fail_closed():
    invalid = rows()
    invalid[0]["target_label_state"] = "maybe"
    with pytest.raises(ValueError, match="target_label_state"):
        analyze_uncertainty_scenarios(invalid, target_label="N3")
    invalid = rows()
    invalid[0]["views_snapshot"] = -1
    with pytest.raises(ValueError, match="finite non-negative"):
        analyze_uncertainty_scenarios(invalid, target_label="N3")
    invalid = rows()
    invalid[1]["post_id"] = "p1"
    with pytest.raises(ValueError, match="duplicate post_id"):
        analyze_uncertainty_scenarios(invalid, target_label="N3")


def test_channel_group_must_be_consistent_within_one_snapshot():
    invalid = rows()
    invalid[1]["channel_group"] = "B"
    with pytest.raises(ValueError, match="conflicting channel_group labels"):
        analyze_uncertainty_scenarios(invalid, target_label="N3", target_group="C")


def test_cli_writes_machine_readable_report(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows()) + "\n", encoding="utf-8")
    output = tmp_path / "result.json"
    assert main(["--input", str(source), "--output", str(output), "--target-label", "N3",
                 "--top-k", "0", "1"]) == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["input_record_count"] == 4
    assert len(report["scenarios"]) == 4


def test_cli_refuses_output_alias_and_symlink(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_text(json.dumps(rows()[0]) + "\n", encoding="utf-8")
    original = source.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="different files"):
        main(["--input", str(source), "--output", str(source), "--target-label", "N3"])
    assert source.read_text(encoding="utf-8") == original
    target = tmp_path / "target.json"
    target.write_text("preserve\n", encoding="utf-8")
    link = tmp_path / "result.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        main(["--input", str(source), "--output", str(link), "--target-label", "N3"])
    assert target.read_text(encoding="utf-8") == "preserve\n"
