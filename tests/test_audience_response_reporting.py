import json

import pytest

from src.analysis.audience_response_reporting import (
    AudienceReportValidationError,
    render_audience_response_markdown,
    validate_audience_aggregate,
)
from src.analysis.report_generator import build_report_bundle
from src.analysis.evaluation_harness import evaluate_run_bundle


def group(label="support", *, platform="telegram", target_id="claim:police-action",
          numerator=1, denominator=2, dimension="stance"):
    return {
        "platform": platform,
        "period_start_utc": None,
        "target_id": target_id,
        "dimension": dimension,
        "label": label,
        "numerator": numerator,
        "denominator": denominator,
        "proportion": numerator / denominator,
        "proportion_basis": "units with label divided by units with any output; abstentions remain in denominator",
        "labels_may_overlap": True,
        "comments_n": denominator,
        "authors_n": denominator,
        "threads_n": 1,
        "channels_n": 1,
        "reaction_state_counts": {
            "not_collected": denominator, "not_reported": 0, "empty": 0,
            "available": 0, "current_user_excluded": 0,
        },
        "reaction_counts_by_state": {"available": {}, "current_user_excluded": {}},
        "reaction_comments_n": denominator,
        "reaction_metrics_unit": "comment",
        "reaction_request_attempts_n": 0,
        "reaction_request_status_counts": {"ok": 0, "request_failed": 0, "no_update": 0, "wrong_peer_update": 0},
        "reaction_comments_not_requested_n": denominator,
        "reaction_observed_at_min_utc": None,
        "reaction_observed_at_max_utc": None,
        "included_records_n": numerator,
        "frame_excluded_records_n": 0,
        "frame_excluded_records_scope": "whole_input_frame",
        "unit_missing_excluded_n": 0,
        "sampling_frame_limitation": "Observed selected comments only; not a population estimate.",
    }


def aggregate(groups=None):
    groups = [group()] if groups is None else groups
    return {
        "schema_version": "1.1",
        "frame": {
            "sampling_frame": "observed_selected_comments",
            "platforms": sorted({row["platform"] for row in groups}) or ["telegram"],
            "comments_input": 2,
            "comments_included": 2,
            "comments_excluded": {"exact_duplicate": 0, "candidate_coordination": 0},
            "author_cap_dropped_group_memberships": 0,
            "interpretation_rows_input": 2,
            "model_id": "local/xlm-r",
            "model_version": "frozen-1",
        },
        "unit": "comment",
        "time_bins": None,
        "groups": groups,
        "views": {
            "target_stance_distribution": [],
            "topic_emotion_cooccurrence": [],
            "largest_available_reaction_comments": [],
        },
        "sensitivity": {"deduplicate_exact": False, "cap_author_contribution": False,
                        "exclude_candidate_coordination": False},
        "limitations": ["Observed selected comments only; not a population estimate."],
        "semantic_quality_status": "not_evaluated",
    }


def test_denominator_free_or_inconsistent_percentage_is_rejected():
    item = aggregate()
    item["groups"][0]["denominator"] = 0
    with pytest.raises(AudienceReportValidationError, match="denominator"):
        validate_audience_aggregate(item)
    item = aggregate()
    item["groups"][0]["proportion"] = 0.9
    with pytest.raises(AudienceReportValidationError, match="proportion"):
        validate_audience_aggregate(item)


def test_stance_without_resolved_target_is_rejected():
    item = aggregate([group(target_id="")])
    with pytest.raises(AudienceReportValidationError, match="target_id"):
        validate_audience_aggregate(item)


def test_platform_reactions_cannot_be_collapsed_into_cross_platform_group():
    item = aggregate([group(platform="all")])
    with pytest.raises(AudienceReportValidationError, match="platform"):
        validate_audience_aggregate(item)
    item = aggregate()
    item["cross_platform_reaction_comparison"] = {"likes": 10}
    with pytest.raises(AudienceReportValidationError, match="fields"):
        validate_audience_aggregate(item)


def test_model_provenance_is_required_for_nonempty_analysis():
    item = aggregate()
    item["frame"]["model_version"] = None
    with pytest.raises(AudienceReportValidationError, match="provenance"):
        validate_audience_aggregate(item)


def test_unqualified_population_support_language_is_rejected():
    item = aggregate([group(label="62% of Ukrainians support the police")])
    with pytest.raises(AudienceReportValidationError, match="label"):
        validate_audience_aggregate(item)


@pytest.mark.parametrize("label", [
    "більшість українців підтримує поліцію",
    "большинство россиян поддерживает полицию",
])
def test_ukrainian_and_russian_population_claims_are_rejected(label):
    item = aggregate([group(label=label)])
    with pytest.raises(AudienceReportValidationError, match="label"):
        validate_audience_aggregate(item)


def test_cross_platform_rows_are_kept_separate():
    item = aggregate([group(platform="telegram"), group(platform="youtube")])
    validated = validate_audience_aggregate(item)
    assert {row["platform"] for row in validated["groups"]} == {"telegram", "youtube"}


def test_report_bundle_writes_structured_audience_section_and_quality_status(tmp_path):
    audience = aggregate([
        group("support", numerator=1, denominator=2),
        group("abstain", numerator=1, denominator=2),
    ])
    context = {
        "incident_id": "incident:test", "run_id": "run:test", "working_title": "test",
        "as_of_utc": "2026-11-08T12:00:00Z", "scope": {"frame": "synthetic"}, "coverage": {},
        "git_commit": None,
    }
    result = build_report_bundle([], [], [], context, tmp_path / "report", audience_aggregate=audience)
    markdown = (tmp_path / "report" / "audience_response.md").read_text(encoding="utf-8")
    structured = json.loads((tmp_path / "report" / "audience_response.json").read_text(encoding="utf-8"))
    quality = json.loads((tmp_path / "report" / "report_quality.json").read_text(encoding="utf-8"))
    assert "stance" in markdown and "claim:police-action" in markdown
    assert "не вимірюють громадську думку населення" in markdown.lower()
    assert any(row["label"] == "abstain" for row in structured["groups"])
    assert quality["audience_semantic_status"] == "not_evaluated"
    assert result["receipt"]["output_sha256"]["audience_response.json"]


def test_markdown_exposes_failed_reaction_refreshes_and_partial_count_semantics():
    row = group("support")
    row["reaction_state_counts"] = {
        "not_collected": 1, "not_reported": 0, "empty": 0,
        "available": 0, "current_user_excluded": 1,
    }
    row["reaction_counts_by_state"] = {"available": {}, "current_user_excluded": {"emoji:👍": 3}}
    row["reaction_request_attempts_n"] = 1
    row["reaction_request_status_counts"] = {"ok": 0, "request_failed": 1, "no_update": 0, "wrong_peer_update": 0}
    row["reaction_comments_not_requested_n"] = 1
    markdown = render_audience_response_markdown(aggregate([row]))
    assert "request_failed=1" in markdown
    assert "current_user_excluded" in markdown
    assert "неповний" in markdown.lower()
    assert "означає помилку запиту; це не означає" in markdown.lower()


def test_markdown_distinguishes_no_update_from_a_message_without_reactions():
    row = group("support")
    row["reaction_request_attempts_n"] = 1
    row["reaction_request_status_counts"] = {"ok": 0, "request_failed": 0, "no_update": 1, "wrong_peer_update": 0}
    row["reaction_comments_not_requested_n"] = 1

    markdown = render_audience_response_markdown(aggregate([row]))

    assert "no_update=1" in markdown
    assert "не є спостереженням реакцій" in markdown.lower()


def test_legacy_1_0_aggregate_with_original_request_statuses_remains_readable():
    item = aggregate()
    item["schema_version"] = "1.0"
    item["groups"][0]["reaction_request_status_counts"] = {"ok": 0, "request_failed": 0}

    validated = validate_audience_aggregate(item)
    markdown = render_audience_response_markdown(validated)

    assert validated["schema_version"] == "1.0"
    assert "no_update=not_recorded" in markdown
    assert "wrong_peer_update=not_recorded" in markdown


def test_report_bundle_cannot_accept_audience_claim_without_denominator(tmp_path):
    context = {
        "incident_id": "incident:test", "run_id": "run:test", "working_title": "test",
        "as_of_utc": "2026-11-08T12:00:00Z", "scope": {"frame": "synthetic"}, "coverage": {},
        "git_commit": None,
    }
    audience = aggregate()
    audience["groups"][0]["denominator"] = 0
    with pytest.raises(AudienceReportValidationError, match="denominator"):
        build_report_bundle([], [], [], context, tmp_path / "bad", audience_aggregate=audience)


def test_release_gate_validates_audience_aggregate_and_keeps_semantics_unmeasured():
    manifest = {
        "schema_version": "1.0", "run_id": "run:test", "incident_id": "incident:test",
        "state": "completed_bounded", "started_at_utc": "2026-11-08T12:00:00Z",
        "finished_at_utc": "2026-11-08T12:00:00Z", "producer": "test", "git_commit": "abcdef0123456789",
        "policy_version": "test-v1", "model_config": {}, "source_snapshots": [], "task_receipts": [], "outputs": [],
    }
    context = {
        "incident_id": "incident:test", "run_id": "run:test", "working_title": "test",
        "as_of_utc": "2026-11-08T12:00:00Z", "scope": {"frame": "synthetic"}, "coverage": {},
        "git_commit": "abcdef0123456789",
    }
    valid = evaluate_run_bundle(run_manifest=manifest, report_context=context, audience_aggregate=aggregate())
    assert valid["hard_gate_status"] == "passed"
    assert valid["semantic_evaluation"]["status"] == "not_evaluated"
    invalid_aggregate = aggregate()
    invalid_aggregate["groups"][0]["denominator"] = 0
    invalid = evaluate_run_bundle(run_manifest=manifest, audience_aggregate=invalid_aggregate)
    assert invalid["hard_gate_status"] == "failed"
    assert any(row["gate_id"] == "audience.schema" for row in invalid["hard_gate_failures"])
