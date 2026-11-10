import pytest

from src.analysis.audience_aggregation import (
    aggregate_audience,
    largest_available_reaction_comments,
    separate_reaction_breakdown,
)
from src.analysis.audience_response_contracts import ContractValidationError
from src.analysis.audience_response_reporting import render_audience_response_markdown


def comment(comment_id, *, author="author:1", thread="thread:1", channel="channel:1",
            text="A sufficiently long repeated comment for sensitivity testing.", date="2026-11-08T12:00:00Z",
            platform="telegram", **extra):
    return {
        "comment_id": comment_id,
        "platform": platform,
        "channel": channel,
        "thread_id": thread,
        "author_pseudonym": author,
        "published_at_utc": date,
        "text": text,
        **extra,
    }


def interpretation(comment_id, target="claim:action", label="support", dimension="stance"):
    return {
        "interpretation_id": f"i:{comment_id}:{target}:{dimension}:{label}",
        "comment_id": comment_id,
        "target_id": target,
        "dimension": dimension,
        "label": label,
        "model_id": "local/xlm-r",
        "model_version": "frozen-1",
    }


def aggregate(comments, interpretations, snapshots=None, *, unit="comment", time_bins=None,
              sensitivity=None):
    return aggregate_audience(
        comments, interpretations, snapshots or [], unit=unit, time_bins=time_bins,
        sensitivity=sensitivity or {
            "deduplicate_exact": False,
            "cap_author_contribution": False,
            "exclude_candidate_coordination": False,
        },
    )


def test_comment_and_author_weighting_use_distinct_estimands():
    comments = [comment(f"c:{i}") for i in range(100)]
    rows = [interpretation(c["comment_id"]) for c in comments]
    comment_group = aggregate(comments, rows)["groups"][0]
    author_group = aggregate(comments, rows, unit="author")["groups"][0]
    assert (comment_group["numerator"], comment_group["denominator"]) == (100, 100)
    assert (author_group["numerator"], author_group["denominator"]) == (1, 1)


def test_new_aggregates_use_schema_1_1_for_explicit_refresh_outcomes():
    result = aggregate([comment("c1")], [interpretation("c1")])
    assert result["schema_version"] == "1.1"


def test_missing_hidden_and_zero_reaction_states_remain_distinct():
    comments = [
        comment("legacy", reactions=0),
        comment("hidden", reaction_state="not_reported", reaction_breakdown=None),
        comment("zero", reaction_state="empty", reaction_breakdown=[]),
    ]
    rows = [interpretation(c["comment_id"]) for c in comments]
    group = aggregate(comments, rows)["groups"][0]
    assert group["reaction_state_counts"] == {
        "not_collected": 1, "not_reported": 1, "empty": 1,
        "available": 0, "current_user_excluded": 0,
    }


def test_multiple_targets_are_aggregated_separately():
    comments = [comment("c1")]
    rows = [interpretation("c1", "claim:action", "support"),
            interpretation("c1", "entity:police", "negative", "sentiment")]
    result = aggregate(comments, rows)
    assert {(g["target_id"], g["dimension"], g["label"]) for g in result["groups"]} == {
        ("claim:action", "stance", "support"), ("entity:police", "sentiment", "negative")
    }


def test_exact_deduplication_is_explicit_sensitivity_scenario():
    text = "Same long comment text copied exactly to test duplicate sensitivity."
    comments = [comment("c1", text=text), comment("c2", text=text)]
    rows = [interpretation("c1"), interpretation("c2")]
    default = aggregate(comments, rows)
    deduped = aggregate(comments, rows, sensitivity={
        "deduplicate_exact": True,
        "cap_author_contribution": False,
        "exclude_candidate_coordination": False,
    })
    assert default["frame"]["comments_included"] == 2
    assert deduped["frame"]["comments_included"] == 1
    assert deduped["frame"]["comments_excluded"]["exact_duplicate"] == 1


def test_exact_deduplication_never_crosses_platform_boundaries():
    text = "Same text repeated in different platform frames should remain separate."
    comments = [comment("tg", text=text, platform="telegram", thread="shared"),
                comment("yt", text=text, platform="youtube", thread="shared")]
    rows = [interpretation("tg"), interpretation("yt")]
    result = aggregate(comments, rows, sensitivity={
        "deduplicate_exact": True,
        "cap_author_contribution": False,
        "exclude_candidate_coordination": False,
    })
    assert result["frame"]["comments_included"] == 2
    assert result["frame"]["comments_excluded"]["exact_duplicate"] == 0


def test_thread_without_replies_does_not_create_synthetic_reaction_or_comments():
    comments = [comment("root", thread="thread:no-replies", is_top_level=True)]
    result = aggregate(comments, [interpretation("root")], unit="thread")
    group = result["groups"][0]
    assert group["threads_n"] == 1
    assert group["comments_n"] == 1
    assert group["reaction_state_counts"]["not_collected"] == 1


def test_latest_snapshot_is_used_without_treating_snapshots_as_independent_comments():
    comments = [comment("c1")]
    rows = [interpretation("c1")]
    snapshots = [
        {"comment_id": "c1", "reaction_state": "available", "reaction_breakdown": [
            {"kind": "emoji", "key": "👍", "count": 2}], "observed_at_utc": "2026-11-08T12:01:00Z"},
        {"comment_id": "c1", "reaction_state": "available", "reaction_breakdown": [
            {"kind": "emoji", "key": "👍", "count": 5}], "observed_at_utc": "2026-11-08T12:05:00Z"},
    ]
    group = aggregate(comments, rows, snapshots)["groups"][0]
    assert group["comments_n"] == 1
    assert group["reaction_state_counts"]["available"] == 1
    assert group["reaction_counts_by_state"]["available"]["emoji:👍"] == 5
    assert group["reaction_comments_n"] == 1
    assert group["reaction_metrics_unit"] == "comment"


def test_failed_reaction_request_is_not_counted_as_not_reported_or_zero():
    comments = [comment("c1")]
    snapshots = [{
        "comment_id": "c1", "request_status": "request_failed", "reaction_state": None,
        "reaction_breakdown": None, "request_attempted_at_utc": "2026-11-08T12:05:00Z",
        "error_code": "flood_wait",
    }]
    group = aggregate(comments, [interpretation("c1")], snapshots)["groups"][0]
    assert group["reaction_state_counts"]["not_collected"] == 1
    assert group["reaction_state_counts"]["not_reported"] == 0
    assert group["reaction_request_status_counts"]["request_failed"] == 1
    assert group["reaction_request_attempts_n"] == 1


def test_missing_mtproto_update_is_not_reported_as_no_reactions():
    comments = [comment("c1")]
    snapshots = [{
        "comment_id": "c1", "request_status": "no_update", "reaction_state": None,
        "reaction_breakdown": None, "request_attempted_at_utc": "2026-11-08T12:05:00Z",
    }]

    group = aggregate(comments, [interpretation("c1")], snapshots)["groups"][0]

    assert group["reaction_state_counts"]["not_collected"] == 1
    assert group["reaction_state_counts"]["not_reported"] == 0
    assert group["reaction_state_counts"]["empty"] == 0
    assert group["reaction_request_status_counts"]["no_update"] == 1
    assert group["reaction_request_attempts_n"] == 1


def test_missing_mtproto_update_does_not_replace_previous_observation():
    comments = [comment("c1", reaction_state="available", reaction_breakdown=[
        {"kind": "emoji", "key": "👍", "count": 2}], reaction_observed_at="2026-11-08T12:00:00Z")]
    snapshots = [{"comment_id": "c1", "request_status": "no_update", "reaction_state": None,
                  "reaction_breakdown": None, "request_attempted_at_utc": "2026-11-08T12:05:00Z"}]

    group = aggregate(comments, [interpretation("c1")], snapshots)["groups"][0]

    assert group["reaction_state_counts"]["available"] == 1
    assert group["reaction_counts_by_state"]["available"]["emoji:👍"] == 2
    assert group["reaction_request_status_counts"]["no_update"] == 1


def test_failed_refresh_does_not_overwrite_previous_successful_snapshot():
    comments = [comment("c1", reaction_state="available", reaction_breakdown=[
        {"kind": "emoji", "key": "👍", "count": 2}], reaction_observed_at="2026-11-08T12:00:00Z")]
    snapshots = [{"comment_id": "c1", "request_status": "request_failed", "reaction_state": None,
                  "reaction_breakdown": None, "request_attempted_at_utc": "2026-11-08T12:05:00Z"}]
    group = aggregate(comments, [interpretation("c1")], snapshots)["groups"][0]
    assert group["reaction_state_counts"]["available"] == 1
    assert group["reaction_counts_by_state"]["available"]["emoji:👍"] == 2
    assert group["reaction_request_status_counts"]["request_failed"] == 1


def test_time_bins_are_explicit_utc_and_refuse_nonpositive_width():
    comments = [comment("a", date="2026-11-08T12:01:00+02:00"),
                comment("b", date="2026-11-08T12:02:00+02:00")]
    rows = [interpretation("a"), interpretation("b")]
    result = aggregate(comments, rows, time_bins=3600)
    assert result["time_bins"] == 3600
    assert result["groups"][0]["period_start_utc"] == "2026-11-08T10:00:00Z"
    with pytest.raises(ValueError):
        aggregate(comments, rows, time_bins=0)


def test_candidate_coordination_is_sensitivity_only_not_default_deletion():
    comments = [comment("c1", is_candidate_coordination=True), comment("c2")]
    rows = [interpretation("c1"), interpretation("c2")]
    assert aggregate(comments, rows)["frame"]["comments_included"] == 2
    result = aggregate(comments, rows, sensitivity={
        "deduplicate_exact": False,
        "cap_author_contribution": False,
        "exclude_candidate_coordination": True,
    })
    assert result["frame"]["comments_included"] == 1
    assert result["frame"]["comments_excluded"]["candidate_coordination"] == 1


def test_aggregation_never_emits_population_ci_or_population_claims():
    comments = [comment("c1")]
    result = aggregate(comments, [interpretation("c1")])
    assert "confidence_interval" not in result
    assert any("selected" in value.lower() or "observed" in value.lower()
               for value in result["limitations"])
    assert any("not a population estimate" in value.lower() for value in result["limitations"])


def test_mixed_model_versions_cannot_be_silently_pooled():
    comments = [comment("c1")]
    first, second = interpretation("c1"), interpretation("c1", label="oppose")
    second["model_id"] = "other/model"
    with pytest.raises(ValueError, match="model"):
        aggregate(comments, [first, second])


def test_author_cap_limits_each_author_per_target_dimension_and_period():
    comments = [comment("c1", author="a"), comment("c2", author="a", date="2026-11-08T12:10:00Z")]
    rows = [interpretation("c1"), interpretation("c2")]
    default = aggregate(comments, rows, time_bins=3600)
    capped = aggregate(comments, rows, time_bins=3600, sensitivity={
        "deduplicate_exact": False,
        "cap_author_contribution": True,
        "exclude_candidate_coordination": False,
    })
    assert default["groups"][0]["comments_n"] == 2
    assert capped["groups"][0]["comments_n"] == 1
    assert capped["frame"]["author_cap_dropped_group_memberships"] == 1


def test_author_without_pseudonym_is_not_counted_as_an_author_unit():
    comments = [comment("c1", author=None), comment("c2", author="known")]
    rows = [interpretation("c1"), interpretation("c2")]
    group = aggregate(comments, rows, unit="author")["groups"][0]
    assert group["numerator"] == 1
    assert group["denominator"] == 1
    assert group["unit_missing_excluded_n"] == 1


def test_platforms_remain_separate_and_multilabel_proportions_are_explicit():
    comments = [comment("tg", platform="telegram"), comment("yt", platform="youtube")]
    rows = [interpretation("tg", dimension="emotion", label="anger"),
            interpretation("tg", dimension="emotion", label="fear"),
            interpretation("yt", dimension="emotion", label="anger")]
    result = aggregate(comments, rows)
    assert {group["platform"] for group in result["groups"]} == {"telegram", "youtube"}
    telegram = [group for group in result["groups"] if group["platform"] == "telegram"]
    assert all(group["denominator"] == 1 for group in telegram)
    assert all(group["labels_may_overlap"] is True for group in telegram)


def test_compact_views_are_targeted_conditioned_and_ranked_per_platform_without_identifiers():
    comments = [
        comment("private-comment-a", author="private-author-a", text="RAW PRIVATE COMMENT alpha"),
        comment("private-comment-b", author="private-author-b", platform="telegram", text="RAW PRIVATE COMMENT beta"),
        comment("private-comment-c", author="private-author-c", platform="telegram", text="RAW PRIVATE COMMENT gamma"),
        comment("private-comment-d", author="private-author-d", platform="youtube", text="RAW PRIVATE COMMENT delta"),
    ]
    rows = [
        interpretation("private-comment-a", label="support", dimension="stance"),
        interpretation("private-comment-a", label="oppose", dimension="stance"),
        interpretation("private-comment-a", label="security", dimension="topic"),
        interpretation("private-comment-a", label="anger", dimension="emotion"),
        interpretation("private-comment-a", label="hostile_language", dimension="hostility"),
        interpretation("private-comment-b", label="support", dimension="stance"),
        interpretation("private-comment-b", label="security", dimension="topic"),
        interpretation("private-comment-b", label="anger", dimension="emotion"),
        interpretation("private-comment-c", label="oppose", dimension="stance"),
        interpretation("private-comment-d", label="oppose", dimension="stance"),
        interpretation("private-comment-d", label="security", dimension="topic"),
        interpretation("private-comment-d", label="anger", dimension="emotion"),
    ]
    snapshots = [
        {"comment_id": "private-comment-a", "reaction_state": "available",
         "reaction_breakdown": [{"kind": "emoji", "key": "👍", "count": 5}],
         "observed_at_utc": "2026-11-08T12:01:00Z"},
        {"comment_id": "private-comment-b", "reaction_state": "current_user_excluded",
         "reaction_breakdown": [{"kind": "emoji", "key": "👍", "count": 100}],
         "observed_at_utc": "2026-11-08T12:01:00Z"},
        {"comment_id": "private-comment-c", "reaction_state": "available",
         "reaction_breakdown": [{"kind": "emoji", "key": "🔥", "count": 2}],
         "observed_at_utc": "2026-11-08T12:01:00Z"},
        {"comment_id": "private-comment-d", "reaction_state": "available",
         "reaction_breakdown": [{"kind": "emoji", "key": "👍", "count": 3}],
         "observed_at_utc": "2026-11-08T12:01:00Z"},
    ]

    result = aggregate(comments, rows, snapshots)

    assert result["views"]["target_stance_distribution"] == [
        {
            "platform": "telegram", "period_start_utc": None, "target_id": "claim:action",
            "unit": "comment", "denominator": 3,
            "denominator_basis": "comments with at least one stance output for this target",
            "stance_counts": {"oppose": 2, "support": 2}, "labels_may_overlap": True,
        },
        {
            "platform": "youtube", "period_start_utc": None, "target_id": "claim:action",
            "unit": "comment", "denominator": 1,
            "denominator_basis": "comments with at least one stance output for this target",
            "stance_counts": {"oppose": 1}, "labels_may_overlap": True,
        },
    ]
    topic_rows = result["views"]["topic_emotion_cooccurrence"]
    telegram_pair = next(row for row in topic_rows if row["platform"] == "telegram")
    assert telegram_pair == {
        "platform": "telegram", "target_id": "claim:action", "topic_label": "security",
        "emotion_label": "anger", "unit": "comment", "numerator": 2, "denominator": 2,
        "denominator_basis": "comments with at least one non-abstained topic and emotion output for this same target",
        "labels_may_overlap": True,
    }
    largest = result["views"]["largest_available_reaction_comments"]
    assert [(row["platform"], row["rank"], row["reaction_count"], row["denominator"])
            for row in largest] == [("telegram", 1, 5, 2), ("telegram", 2, 2, 2), ("youtube", 1, 3, 1)]
    encoded_views = str(result["views"])
    assert "RAW PRIVATE COMMENT" not in encoded_views
    assert "private-author-" not in encoded_views
    assert "private-comment-" not in encoded_views
    markdown = render_audience_response_markdown(result)
    assert "### Target-by-stance distribution" in markdown
    assert "### Topic and emotion co-occurrence" in markdown
    assert "### Comments with the largest available reaction counts" in markdown
    assert "## Ворожість і розбіжність позицій" in markdown
    assert "не обчислюється загальний бал поляризації" in markdown
    assert "RAW PRIVATE COMMENT" not in markdown
    assert "private-author-" not in markdown
    assert "private-comment-" not in markdown


def test_separate_reaction_breakdown_decouples_stars_and_unknowns():
    breakdown = [
        {"kind": "emoji", "key": "👍", "count": 12},
        {"kind": "custom_emoji", "key": "document:12345", "count": 3},
        {"kind": "paid", "key": "paid", "count": 150},
        {"reaction_type": "paid", "key": "paid", "count": 50},
        {"kind": "unknown", "key": "future_type", "count": 7},
        {"reaction_type": "unknown", "key": "another_future", "count": 4},
    ]
    counts = separate_reaction_breakdown(breakdown)
    assert counts == {
        "non_paid_reaction_count": 15,
        "paid_star_amount": 200,
        "unknown_reaction_count": 11,
    }


def test_separate_reaction_breakdown_validates_counts():
    with pytest.raises(ContractValidationError):
        separate_reaction_breakdown([])
    with pytest.raises(ContractValidationError):
        separate_reaction_breakdown([{"kind": "emoji", "key": "👍", "count": -1}])
    with pytest.raises(ContractValidationError):
        separate_reaction_breakdown([{"kind": "emoji", "key": "👍", "count": "five"}])


def test_paid_star_reactions_do_not_distort_comment_rankings():
    # c_stars has 500 paid stars but only 2 emojis
    # c_emoji has 10 emojis and 0 paid stars
    # c_paid_only has 1000 paid stars and 0 emojis
    # c_custom has 4 custom emojis, 2 standard emojis, and 50 unknown reactions
    comments = [
        comment("c_stars", reaction_state="available", reaction_breakdown=[
            {"kind": "emoji", "key": "👍", "count": 2},
            {"kind": "paid", "key": "paid", "count": 500},
        ]),
        comment("c_emoji", reaction_state="available", reaction_breakdown=[
            {"kind": "emoji", "key": "🔥", "count": 10},
        ]),
        comment("c_paid_only", reaction_state="available", reaction_breakdown=[
            {"reaction_type": "paid", "key": "paid", "count": 1000},
        ]),
        comment("c_custom", reaction_state="available", reaction_breakdown=[
            {"kind": "custom_emoji", "key": "document:999", "count": 4},
            {"kind": "emoji", "key": "❤️", "count": 2},
            {"kind": "unknown", "key": "future", "count": 50},
        ]),
    ]
    rows = [interpretation(c["comment_id"]) for c in comments]
    result = aggregate(comments, rows)

    largest = result["views"]["largest_available_reaction_comments"]
    # Ranks must be based strictly on non-paid emoji/custom_emoji reaction counts:
    # Rank 1: c_emoji (10 emojis)
    # Rank 2: c_custom (4 custom + 2 standard = 6 emojis; 50 unknown excluded)
    # Rank 3: c_stars (2 emojis; 500 stars excluded)
    # Rank 4: c_paid_only (0 emojis; 1000 stars excluded)
    assert [(row["rank"], row["reaction_count"], row["denominator"]) for row in largest] == [
        (1, 10, 4),
        (2, 6, 4),
        (3, 2, 4),
        (4, 0, 4),
    ]

    # Confirm markdown renders properly with these segregated counts
    markdown = render_audience_response_markdown(result)
    assert "| 1 | 10 | comment | 4 |" in markdown
    assert "| 2 | 6 | comment | 4 |" in markdown
    assert "| 3 | 2 | comment | 4 |" in markdown
    assert "| 4 | 0 | comment | 4 |" in markdown


def test_largest_available_reaction_comments_direct_call_and_include_paid():
    comments = [
        comment("c1", reaction_state="available", reaction_breakdown=[
            {"kind": "emoji", "key": "👍", "count": 3},
            {"kind": "paid", "key": "paid", "count": 100},
        ]),
        comment("c2", reaction_state="available", reaction_breakdown=[
            {"kind": "emoji", "key": "👍", "count": 7},
        ]),
    ]
    # Direct call with default include_paid=False (schema-compliant 6 fields)
    standard_rows = largest_available_reaction_comments(comments, top_k=5)
    assert len(standard_rows) == 2
    assert standard_rows[0]["rank"] == 1
    assert standard_rows[0]["reaction_count"] == 7
    assert standard_rows[1]["rank"] == 2
    assert standard_rows[1]["reaction_count"] == 3
    assert "paid_star_amount" not in standard_rows[0]

    # Direct call with include_paid=True
    paid_rows = largest_available_reaction_comments(comments, top_k=5, include_paid=True)
    assert paid_rows[0]["reaction_count"] == 7
    assert paid_rows[0]["paid_star_amount"] == 0
    assert paid_rows[1]["reaction_count"] == 3
    assert paid_rows[1]["paid_star_amount"] == 100

    # Test top_k validation
    with pytest.raises(ValueError, match="top_k"):
        largest_available_reaction_comments(comments, top_k=0)
    with pytest.raises(ValueError, match="top_k"):
        largest_available_reaction_comments(comments, top_k=-1)

