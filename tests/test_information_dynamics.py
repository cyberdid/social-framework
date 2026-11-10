from __future__ import annotations

from src.analysis.information_dynamics import summarize_information_dynamics


def post(post_id, platform, channel, published, observed, **extra):
    return {"post_id": post_id, "platform": platform, "channel_id": channel,
            "publication_time": published, "observed_at": observed, **extra}


def test_separates_publication_and_observation_time_and_reports_coverage_gaps():
    posts = [
        post("p1", "telegram", "a", "2026-11-02T10:15:00+02:00", "2026-11-02T13:00:00+02:00"),
        post("p2", "youtube", "b", "2026-11-02T10:45:00+02:00", "2026-11-03T08:00:00+02:00"),
    ]
    coverage = [
        {"platform": "telegram", "status": "partial", "reason": "target channel panel"},
        {"platform": "youtube", "status": "partial", "reason": "search-limited"},
        {"platform": "tiktok", "status": "not_available", "reason": "no API export"},
    ]

    result = summarize_information_dynamics(posts, coverage=coverage, bin_hours=6)

    assert sum(row["post_count"] for row in result["publication_series"]) == 2
    assert sum(row["post_count"] for row in result["observation_series"]) == 2
    assert result["coverage"]["platforms"]["tiktok"]["status"] == "not_available"
    assert "does_not_establish_absence" in result["coverage"]["limitations"]
    assert result["temporal_scope"]["publication_time_field"] == "publication_time"
    assert result["temporal_scope"]["observation_time_field"] == "observed_at"


def test_confirmed_copy_dependencies_do_not_inflate_independent_roots():
    posts = [
        post("p1", "telegram", "a", "2026-11-02T10:00:00+02:00", "2026-11-02T11:00:00+02:00", evidence_id="e1"),
        post("p2", "telegram", "b", "2026-11-02T10:15:00+02:00", "2026-11-02T11:00:00+02:00", evidence_id="e2"),
        post("p3", "youtube", "c", "2026-11-02T10:20:00+02:00", "2026-11-02T11:00:00+02:00", evidence_id="e3"),
        post("p4", "tiktok", "d", "2026-11-02T10:25:00+02:00", "2026-11-02T11:00:00+02:00", evidence_id="e4"),
    ]
    lineage = {"edges": [
        {"claim_id": "c1", "evidence_ids": ["e1", "e2"], "certainty": "confirmed_by_explicit_reference"},
        {"claim_id": "c1", "evidence_ids": ["e2", "e3"], "certainty": "identical_asset_bytes"},
        {"claim_id": "c1", "evidence_ids": ["e3", "e4"], "certainty": "candidate"},
    ]}

    summary = summarize_information_dynamics(posts, source_lineage=lineage)["source_lineage"]

    assert summary["confirmed_dependency_family_count"] == 1
    assert summary["confirmed_dependency_families"][0]["post_count"] == 3
    assert summary["candidate_dependency_edge_count"] == 1
    assert summary["independent_roots_lower_bound"] == 0
    assert summary["unlinked_post_count"] == 1


def test_group_sensitivity_remaps_automatic_candidates_and_excludes_them_in_manual_case():
    posts = [
        post("p1", "telegram", "manual-channel", "2026-11-02T10:00:00+02:00", "2026-11-02T11:00:00+02:00",
             group_label="C", group_label_status="manual", view_count_snapshot=100),
        post("p2", "telegram", "auto-channel", "2026-11-02T10:05:00+02:00", "2026-11-02T11:00:00+02:00",
             group_label="C", group_label_status="automatic_candidate", view_count_snapshot=900),
        post("p3", "telegram", "unknown-channel", "2026-11-02T10:10:00+02:00", "2026-11-02T11:00:00+02:00",
             group_label="U", group_label_status="unknown", view_count_snapshot=50),
    ]

    scenarios = summarize_information_dynamics(posts)["group_sensitivity"]
    by_id = {item["scenario_id"]: item for item in scenarios}

    assert by_id["all_labels_as_recorded"]["groups"]["C"]["post_count"] == 2
    assert by_id["automatic_candidates_unknown"]["groups"]["C"]["post_count"] == 1
    assert by_id["manual_and_evidence_supported_only"]["included_post_count"] == 1
    assert by_id["manual_and_evidence_supported_only"]["excluded_post_count"] == 2
    assert all(item["label_semantics"] == "descriptive_candidate_group_only" for item in scenarios)


def test_top_k_concentration_uses_latest_counter_snapshot_and_never_calls_it_reach():
    posts = [
        post("p1", "telegram", "a", "2026-11-02T10:00:00+02:00", "2026-11-02T13:00:00+02:00",
             view_count_snapshot=100, view_count_observed_at="2026-11-02T11:00:00+02:00"),
        post("p1", "telegram", "a", "2026-11-02T10:00:00+02:00", "2026-11-02T14:00:00+02:00",
             view_count_snapshot=150, view_count_observed_at="2026-11-02T14:00:00+02:00"),
        post("p2", "telegram", "b", "2026-11-02T10:30:00+02:00", "2026-11-02T14:00:00+02:00",
             view_count_snapshot=50, view_count_observed_at="2026-11-02T14:00:00+02:00"),
    ]

    result = summarize_information_dynamics(posts, top_k=(1, 2))
    view_k1 = next(item for item in result["top_k_concentration"]
                   if item["metric"] == "snapshot_view_counter_sum" and item["k"] == 1)

    assert result["corpus"]["unique_post_count"] == 2
    assert view_k1["total"] == 200
    assert view_k1["top_value"] == 150
    assert view_k1["share"] == 0.75
    assert view_k1["unique_audience"] is False
    assert "snapshot_view_counters_are_not_unique_audience_or_verified_reach" in result["limitations"]


def test_missing_or_naive_publication_time_is_not_silently_imputed():
    posts = [post("p1", "telegram", "a", "2026-11-02T10:00:00", "2026-11-02T11:00:00+02:00")]

    try:
        summarize_information_dynamics(posts)
    except ValueError as error:
        assert "timezone-aware" in str(error)
    else:
        raise AssertionError("naive publication time should be rejected")


def test_physical_event_time_is_preserved_as_reported_interval_not_causal_claim():
    posts = [post("p1", "telegram", "a", "2026-11-02T10:00:00+02:00", "2026-11-02T11:00:00+02:00",
                  reported_event_time_start="2026-11-02T08:00:00+02:00",
                  reported_event_time_end="2026-11-02T09:30:00+02:00")]

    result = summarize_information_dynamics(posts)

    assert result["reported_event_time_references"][0]["event_time_start"] == "2026-11-02T06:00:00+00:00"
    assert result["reported_event_time_references"][0]["event_time_end"] == "2026-11-02T07:30:00+00:00"
    assert result["reported_event_time_references"][0]["status"] == "reported_time_reference"
    assert result["reported_event_time_references"][0]["post_ref"] == "telegram:p1"
    assert "does_not_establish_causality" in result["limitations"]


def test_equal_time_conflicting_view_snapshots_are_excluded_and_order_invariant():
    rows = [
        post("p1", "telegram", "a", "2026-11-02T10:00:00+02:00", "2026-11-02T11:00:00+02:00",
             view_count_snapshot=100, view_count_observed_at="2026-11-02T11:00:00+02:00"),
        post("p1", "telegram", "a", "2026-11-02T10:00:00+02:00", "2026-11-02T11:00:00+02:00",
             view_count_snapshot=200, view_count_observed_at="2026-11-02T11:00:00+02:00"),
    ]

    forward = summarize_information_dynamics(rows)
    reverse = summarize_information_dynamics(list(reversed(rows)))

    assert forward == reverse
    assert forward["corpus"]["view_snapshot_conflict_count"] == 1
    assert forward["corpus"]["view_counter_post_count"] == 0
    assert next(item for item in forward["top_k_concentration"]
                if item["metric"] == "snapshot_view_counter_sum" and item["k"] == 1)["total"] == 0


def test_identical_native_ids_on_different_platforms_remain_distinct():
    rows = [
        post("123", "telegram", "tg-channel", "2026-11-02T10:00:00+02:00",
             "2026-11-02T11:00:00+02:00", view_count_snapshot=10),
        post("123", "tiktok", "tt-channel", "2026-11-02T10:05:00+02:00",
             "2026-11-02T11:00:00+02:00", view_count_snapshot=20),
    ]
    result = summarize_information_dynamics(rows, top_k=(2,))
    assert result["corpus"]["unique_post_count"] == 2
    assert sum(row["post_count"] for row in result["publication_series"]) == 2
    assert next(item for item in result["top_k_concentration"]
                if item["metric"] == "post_count" and item["k"] == 2)["top_channels"] == [
                    "telegram:tg-channel", "tiktok:tt-channel"]


def test_same_channel_id_on_two_platforms_is_two_channel_entities():
    rows = [
        post("p1", "telegram", "news", "2026-11-02T10:00:00+02:00", "2026-11-02T11:00:00+02:00"),
        post("p2", "tiktok", "news", "2026-11-02T10:05:00+02:00", "2026-11-02T11:00:00+02:00"),
    ]
    result = summarize_information_dynamics(rows, top_k=(1, 2))
    top_two = next(item for item in result["top_k_concentration"]
                   if item["metric"] == "post_count" and item["k"] == 2)
    assert top_two["top_channels"] == ["telegram:news", "tiktok:news"]
    assert top_two["share"] == 1


def test_unmatched_candidate_lineage_edges_do_not_inflate_candidate_count():
    posts = [post("p1", "telegram", "a", "2026-11-02T10:00:00+02:00",
                  "2026-11-02T11:00:00+02:00", evidence_id="e1")]
    result = summarize_information_dynamics(posts, source_lineage={"edges": [
        {"evidence_ids": ["external-a", "external-b"], "certainty": "candidate"},
    ]})
    assert result["source_lineage"]["candidate_dependency_edge_count"] == 0
    assert result["source_lineage"]["unmatched_dependency_edge_count"] == 1
