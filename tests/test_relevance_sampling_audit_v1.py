from __future__ import annotations

import json

import pytest

from src.analysis.relevance_sampling_audit_v1 import (
    analyze_relevance_audit,
    conservative_stratified_half_width,
    finite_population_sample_size,
    add_composite_record_ids,
    plan_stratified_allocation,
    rare_miss_detection_sample_size,
    stratified_relevance_sample,
    main,
)


def frame() -> list[dict]:
    return [
        {"post_id": "p1", "relevant": True, "text_clean": "Petropavlivska Borshchahivka event"},
        {"post_id": "p2", "relevant": False, "text_clean": "ТЦК та протест у селі"},
        {"post_id": "p3", "relevant": False, "text_clean": "Петропавлівська Борщагівка: затримання"},
        {"post_id": "p4", "relevant": False, "text_clean": "звичайне повідомлення", "fwd_from_url": "https://t.me/news/1"},
        {"post_id": "p5", "relevant": False, "text_clean": "звичайний допис"},
        {"post_id": "p6", "relevant": False, "text_clean": "інший звичайний допис"},
    ]


def test_stratified_sample_is_deterministic_weighted_and_excludes_selected_posts():
    records = frame()
    first = stratified_relevance_sample(records, sample_sizes={
        "rejected__candidate_no_toponym_context": 1,
        "rejected__candidate_place_only": 1,
        "rejected__candidate_reference_context": 1,
        "rejected__random_remainder": 1,
    }, seed=7)
    second = stratified_relevance_sample(records, sample_sizes={
        "rejected__candidate_no_toponym_context": 1,
        "rejected__candidate_place_only": 1,
        "rejected__candidate_reference_context": 1,
        "rejected__random_remainder": 1,
    }, seed=7)
    assert first == second
    assert all(item["record_id"] != "p1" for item in first["sample"])
    assert len(first["sample"]) == 4
    by_stratum = {item["stratum_id"]: item for item in first["sample"]}
    assert by_stratum["rejected__candidate_no_toponym_context"]["inclusion_probability"] == 1
    assert first["design"]["population_scope"] == "rejected_records_in_supplied_panel_only"
    assert first["design"]["scope_is_platform_wide"] is False


def test_sampling_manifest_does_not_export_post_text_and_hashes_frame():
    result = stratified_relevance_sample(frame(), sample_sizes={
        "rejected__candidate_no_toponym_context": 1,
        "rejected__candidate_place_only": 1,
        "rejected__candidate_reference_context": 1,
        "rejected__random_remainder": 1,
    }, seed=11)
    assert len(result["frame_sha256"]) == 64
    assert "Petropavlivska" not in str(result)
    assert "звичайний допис" not in str(result)


def test_reference_stratum_accepts_native_posts_clean_links_field():
    result = stratified_relevance_sample(
        [{"post_id": "p-links", "relevant": False, "text_clean": "нейтральне повідомлення",
          "links": ["https://t.me/source/123"]}],
        sample_sizes={"rejected__candidate_reference_context": 1},
        seed=19,
    )
    assert result["strata"] == [{
        "stratum_id": "rejected__candidate_reference_context",
        "population_count": 1,
        "sample_count": 1,
        "inclusion_probability": 1.0,
    }]
    assert result["sample"][0]["record_id"] == "p-links"


def test_validation_rejects_duplicate_or_invalid_ids_and_allocation():
    records = frame()
    records.append(dict(records[-1]))
    with pytest.raises(ValueError, match="duplicate"):
        stratified_relevance_sample(records, sample_sizes={}, seed=1)
    with pytest.raises(ValueError, match="boolean"):
        stratified_relevance_sample([{"post_id": "x", "relevant": "false"}], sample_sizes={}, seed=1)
    with pytest.raises(ValueError, match="reason code"):
        stratified_relevance_sample([{"post_id": "x", "relevant": False, "relevance_reason_code": "free text"}],
                                    sample_sizes={"rejected__random_remainder": 0}, seed=1)
    with pytest.raises(ValueError, match="allocation"):
        stratified_relevance_sample(frame(), sample_sizes={"not_a_stratum": 1}, seed=1)
    with pytest.raises(ValueError, match="positive allocation"):
        stratified_relevance_sample(frame(), sample_sizes={
            "rejected__candidate_no_toponym_context": 1,
            "rejected__candidate_place_only": 1,
            "rejected__candidate_reference_context": 1,
            "rejected__random_remainder": 0,
        }, seed=1)


def test_finite_population_sample_size_uses_conservative_proportion_and_fpc():
    n = finite_population_sample_size(population_size=1000, margin_error=0.05, confidence_level=0.95)
    assert n == 278
    assert finite_population_sample_size(population_size=0, margin_error=0.1) == 0
    assert finite_population_sample_size(population_size=10, margin_error=0.01) == 10


def test_stratified_allocation_is_minimal_and_matches_direct_design_variance():
    counts = {"a": 10, "b": 10}
    plan = plan_stratified_allocation(
        counts, target_margin_error=0.32, minimum_sample_per_stratum=2, confidence_level=0.95,
    )
    allocations = {row["stratum_id"]: row["sample_count"] for row in plan["allocations"]}
    direct_half_width = conservative_stratified_half_width(
        counts, allocations, confidence_level=0.95,
    )
    previous = conservative_stratified_half_width(counts, {"a": 3, "b": 3}, confidence_level=0.95)
    assert plan["total_sample_size"] == 7
    assert allocations == {"a": 4, "b": 3}
    assert plan["achieved_conservative_half_width"] == pytest.approx(direct_half_width)
    assert direct_half_width <= 0.32
    assert previous > 0.32
    assert plan["assumptions"]["worst_case_binary_superpopulation_variance"] == 0.25
    assert plan["assumptions"]["finite_population_binary_variance_bound"] == "0.25 * N_h/(N_h-1)"


def test_stratified_worst_case_variance_bounds_exhaustive_small_binary_populations():
    import math

    from src.analysis.relevance_sampling_audit_v1 import _normal_quantile

    confidence = 0.95
    z = _normal_quantile(0.5 + confidence / 2)
    for population_size in range(2, 10):
        for sample_size in range(1, population_size):
            actual_variances = []
            for positives in range(population_size + 1):
                p = positives / population_size
                finite_population_variance = (
                    population_size / (population_size - 1) * p * (1 - p)
                )
                actual_variances.append(
                    (1 - sample_size / population_size)
                    * finite_population_variance / sample_size
                )
            exact_max_half_width = z * math.sqrt(max(actual_variances))
            conservative = conservative_stratified_half_width(
                {"only": population_size}, {"only": sample_size},
                confidence_level=confidence,
            )
            assert conservative + 1e-12 >= exact_max_half_width


def test_stratified_allocation_is_order_invariant_with_lexical_tie_break():
    first = plan_stratified_allocation(
        {"zeta": 10, "alpha": 10}, target_margin_error=0.32, minimum_sample_per_stratum=2,
    )
    second = plan_stratified_allocation(
        {"alpha": 10, "zeta": 10}, target_margin_error=0.32, minimum_sample_per_stratum=2,
    )
    assert first["allocations"] == second["allocations"]
    assert {row["stratum_id"]: row["sample_count"] for row in first["allocations"]} == {
        "alpha": 4, "zeta": 3,
    }


def test_stratified_allocation_censuses_small_strata_and_handles_empty_frame():
    census = plan_stratified_allocation(
        {"small": 2, "large": 3}, target_margin_error=0.01, minimum_sample_per_stratum=3,
    )
    assert census["total_sample_size"] == 5
    assert census["achieved_conservative_half_width"] == 0
    assert all(row["inclusion_probability"] == 1 for row in census["allocations"])
    empty = plan_stratified_allocation({}, target_margin_error=0.1, minimum_sample_per_stratum=2)
    assert empty["total_sample_size"] == 0
    assert empty["achieved_conservative_half_width"] is None
    assert empty["status"] == "empty_frame"


def test_stratified_allocation_rejects_invalid_frame_or_margin():
    with pytest.raises(ValueError, match="positive integer"):
        plan_stratified_allocation({"a": 0}, target_margin_error=0.1)
    with pytest.raises(ValueError, match="positive integer"):
        plan_stratified_allocation({"a": 10}, target_margin_error=0.1, minimum_sample_per_stratum=0)
    with pytest.raises(ValueError, match="margin_error"):
        plan_stratified_allocation({"a": 10}, target_margin_error=0)


def test_rare_miss_helper_uses_exact_hypergeometric_detection_probability():
    n = rare_miss_detection_sample_size(population_size=100, minimum_prevalence=0.05, confidence_level=0.95)
    assert n > 0
    assert rare_miss_detection_sample_size(population_size=100, minimum_prevalence=0.05,
                                          confidence_level=0.95) == n
    # At the returned n the probability of observing no misses is at most 5%; at n-1 it is greater.
    from src.analysis.relevance_sampling_audit_v1 import hypergeometric_zero_probability
    assert hypergeometric_zero_probability(100, 5, n) <= 0.05
    assert hypergeometric_zero_probability(100, 5, n - 1) > 0.05


def test_design_weighted_analysis_uses_stratum_weights_and_fpc():
    manifest = {
        "design": {"confidence_level": 0.95, "population_scope": "rejected_records_in_supplied_panel_only",
                   "selected_record_count": 6},
        "frame_sha256": "a" * 64,
        "strata": [
            {"stratum_id": "a", "population_count": 3, "sample_count": 2},
            {"stratum_id": "b", "population_count": 2, "sample_count": 2},
        ],
        "sample": [
            {"record_id": "a1", "stratum_id": "a", "inclusion_probability": 2 / 3},
            {"record_id": "a2", "stratum_id": "a", "inclusion_probability": 2 / 3},
            {"record_id": "b1", "stratum_id": "b", "inclusion_probability": 1.0},
            {"record_id": "b2", "stratum_id": "b", "inclusion_probability": 1.0},
        ],
    }
    result = analyze_relevance_audit(manifest, [
        {"record_id": "a1", "missed_relevant": True},
        {"record_id": "a2", "missed_relevant": False},
        {"record_id": "b1", "missed_relevant": False},
        {"record_id": "b2", "missed_relevant": False},
    ])
    assert result["estimated_missed_relevant_count"] == pytest.approx(1.5)
    assert result["estimated_missed_relevant_share_of_rejected_frame"] == pytest.approx(0.3)
    assert result["design_based_variance_of_share"] == pytest.approx(0.03)
    assert result["interval_type"] == "approximate_design_based_wald_interval_with_fpc"
    assert result["scope_is_platform_wide"] is False
    assert not any("recall" in key for key in result)


def test_sparse_zero_positive_sample_reports_exact_upper_bound_not_zero_ci():
    manifest = {
        "design": {"confidence_level": 0.95, "population_scope": "rejected_records_in_supplied_panel_only"},
        "frame_sha256": "b" * 64,
        "strata": [{"stratum_id": "a", "population_count": 10, "sample_count": 2}],
        "sample": [
            {"record_id": "a1", "stratum_id": "a", "inclusion_probability": 0.2},
            {"record_id": "a2", "stratum_id": "a", "inclusion_probability": 0.2},
        ],
    }
    result = analyze_relevance_audit(manifest, [
        {"record_id": "a1", "missed_relevant": False},
        {"record_id": "a2", "missed_relevant": False},
    ])
    assert result["observed_missed_relevant_count"] == 0
    assert result["exact_upper_bound_missed_relevant_share"] > 0
    assert result["exact_upper_bound_missed_relevant_share"] <= 1
    assert result["zero_observed_misses"] is True
    assert result["confidence_interval"] is None
    assert result["interval_type"] == "not_reported_degenerate_sparse_sample_exact_upper_bound_provided"


def test_stratified_boundary_samples_do_not_collapse_wald_interval_across_strata():
    strata = [
        {"stratum_id": "all_positive", "population_count": 100, "sample_count": 2},
        {"stratum_id": "all_negative", "population_count": 100, "sample_count": 2},
    ]
    sample = []
    adjudications = []
    for stratum in strata:
        sid = stratum["stratum_id"]
        for index in range(1, 3):
            record_id = f"{sid}-{index}"
            sample.append({"record_id": record_id, "stratum_id": sid, "inclusion_probability": 0.02})
            adjudications.append({"record_id": record_id, "missed_relevant": sid == "all_positive"})
    manifest = {
        "design": {"confidence_level": 0.95, "population_scope": "rejected_records_in_supplied_panel_only"},
        "frame_sha256": "f" * 64,
        "strata": strata,
        "sample": sample,
    }

    result = analyze_relevance_audit(manifest, adjudications)

    assert result["estimated_missed_relevant_share_of_rejected_frame"] == pytest.approx(0.5)
    assert result["confidence_interval"] is None
    assert result["interval_type"] == "not_reported_degenerate_stratum_exact_upper_bound_provided"
    assert result["exact_upper_bound_missed_relevant_share"] > 0.9


def test_census_stratum_boundary_does_not_suppress_wald_interval():
    manifest = {
        "design": {"confidence_level": 0.95, "population_scope": "rejected_records_in_supplied_panel_only"},
        "frame_sha256": "a" * 64,
        "strata": [
            {"stratum_id": "census", "population_count": 2, "sample_count": 2},
            {"stratum_id": "sampled", "population_count": 10, "sample_count": 2},
        ],
        "sample": [
            {"record_id": "c1", "stratum_id": "census", "inclusion_probability": 1.0},
            {"record_id": "c2", "stratum_id": "census", "inclusion_probability": 1.0},
            {"record_id": "s1", "stratum_id": "sampled", "inclusion_probability": 0.2},
            {"record_id": "s2", "stratum_id": "sampled", "inclusion_probability": 0.2},
        ],
    }

    result = analyze_relevance_audit(manifest, [
        {"record_id": "c1", "missed_relevant": True},
        {"record_id": "c2", "missed_relevant": True},
        {"record_id": "s1", "missed_relevant": True},
        {"record_id": "s2", "missed_relevant": False},
    ])

    assert result["confidence_interval"] is not None
    assert result["interval_type"] == "approximate_design_based_wald_interval_with_fpc"


def test_singleton_non_census_stratum_suppresses_unestimable_wald_interval():
    manifest = {
        "design": {"confidence_level": 0.95, "population_scope": "rejected_records_in_supplied_panel_only"},
        "frame_sha256": "e" * 64,
        "strata": [
            {"stratum_id": "a", "population_count": 2, "sample_count": 1},
            {"stratum_id": "b", "population_count": 2, "sample_count": 1},
        ],
        "sample": [
            {"record_id": "a1", "stratum_id": "a", "inclusion_probability": 0.5},
            {"record_id": "b1", "stratum_id": "b", "inclusion_probability": 0.5},
        ],
    }
    result = analyze_relevance_audit(manifest, [
        {"record_id": "a1", "missed_relevant": True}, {"record_id": "b1", "missed_relevant": False},
    ])
    assert result["confidence_interval"] is None
    assert result["interval_type"] == "not_estimable_from_singleton_sample"


def test_analysis_rejects_missing_duplicate_or_nonbinary_adjudications():
    manifest = {
        "design": {"confidence_level": 0.95, "population_scope": "rejected_records_in_supplied_panel_only"}, "frame_sha256": "c" * 64,
        "strata": [{"stratum_id": "a", "population_count": 1, "sample_count": 1}],
        "sample": [{"record_id": "a1", "stratum_id": "a", "inclusion_probability": 1.0}],
    }
    with pytest.raises(ValueError, match="missing adjudication"):
        analyze_relevance_audit(manifest, [])
    with pytest.raises(ValueError, match="boolean"):
        analyze_relevance_audit(manifest, [{"record_id": "a1", "missed_relevant": "yes"}])


def test_analysis_rejects_nonempty_unsampled_stratum_in_a_manifest():
    manifest = {
        "design": {"confidence_level": 0.95, "population_scope": "rejected_records_in_supplied_panel_only"},
        "frame_sha256": "d" * 64,
        "strata": [{"stratum_id": "a", "population_count": 5, "sample_count": 0}],
        "sample": [],
    }
    with pytest.raises(ValueError, match="positive sample count"):
        analyze_relevance_audit(manifest, [])


def test_composite_channel_message_ids_are_unambiguous_and_collision_checked():
    records = [
        {"channel": "ab", "msg_id": "c", "relevant": False, "text_clean": "plain"},
        {"channel": "a", "msg_id": "bc", "relevant": False, "text_clean": "plain"},
    ]
    prepared = add_composite_record_ids(records, component_keys=("channel", "msg_id"))
    ids = [row["_audit_record_id"] for row in prepared]
    assert ids[0] != ids[1]
    assert all(record.get("_audit_record_id") is None for record in records)
    with pytest.raises(ValueError, match="duplicate composite ID"):
        add_composite_record_ids([
            {"channel": "chan", "msg_id": 1}, {"channel": "chan", "msg_id": 1},
        ], component_keys=("channel", "msg_id"))


def test_cli_writes_manifest_with_per_stratum_cap(tmp_path):
    source = tmp_path / "posts.jsonl"
    source.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in frame()) + "\n", encoding="utf-8")
    output = tmp_path / "sampling-manifest.json"
    assert main(["--input", str(source), "--output", str(output), "--seed", "20261108",
                 "--sample-size-per-stratum", "1"]) == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert manifest["design"]["rejected_frame_count"] == 5
    assert len(manifest["sample"]) == 4
    assert manifest["design"]["scope_is_platform_wide"] is False


def test_cli_builds_composite_channel_and_message_id(tmp_path):
    source = tmp_path / "telegram-posts.jsonl"
    records = [
        {"channel": "ab", "msg_id": "c", "relevant": False, "text_clean": "plain one"},
        {"channel": "a", "msg_id": "bc", "relevant": False, "text_clean": "plain two"},
    ]
    source.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    output = tmp_path / "manifest.json"
    assert main(["--input", str(source), "--output", str(output), "--seed", "9",
                 "--composite-id-keys", "channel,msg_id", "--sample-size-per-stratum", "2"]) == 0
    manifest = json.loads(output.read_text(encoding="utf-8"))
    ids = [row["record_id"] for row in manifest["sample"]]
    assert len(ids) == len(set(ids)) == 2
    assert all(value.startswith("composite-id-v1:") for value in ids)


def test_cli_refuses_existing_output_without_overwriting_it(tmp_path):
    source = tmp_path / "posts.jsonl"
    source.write_text(json.dumps({"post_id": "p1", "relevant": False, "text_clean": "plain"}) + "\n",
                      encoding="utf-8")
    output = tmp_path / "manifest.json"
    output.write_text("keep this prior manifest\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="already exists"):
        main(["--input", str(source), "--output", str(output), "--seed", "2",
              "--sample-size-per-stratum", "1"])
    assert output.read_text(encoding="utf-8") == "keep this prior manifest\n"


def test_cli_refuses_input_alias_and_existing_output_symlink(tmp_path):
    source = tmp_path / "posts.jsonl"
    source.write_text(json.dumps({"post_id": "p1", "relevant": False, "text_clean": "plain"}) + "\n",
                      encoding="utf-8")
    with pytest.raises(ValueError, match="different files"):
        main(["--input", str(source), "--output", str(source), "--seed", "2",
              "--sample-size-per-stratum", "1"])
    target = tmp_path / "target.json"
    target.write_text("preserve\n", encoding="utf-8")
    link = tmp_path / "manifest.json"
    link.symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        main(["--input", str(source), "--output", str(link), "--seed", "2",
              "--sample-size-per-stratum", "1"])
    assert target.read_text(encoding="utf-8") == "preserve\n"


def test_empty_rejected_frame_has_explicit_non_estimable_result():
    manifest = stratified_relevance_sample([{"post_id": "p1", "relevant": True}], sample_sizes={}, seed=1)
    result = analyze_relevance_audit(manifest, [])
    assert result["rejected_frame_count"] == 0
    assert result["estimated_missed_relevant_count"] is None
    assert result["interval_type"] == "not_estimable_empty_frame"

