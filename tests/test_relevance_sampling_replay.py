from __future__ import annotations

from src.analysis.relevance_sampling_audit_v1 import stratified_relevance_sample
from src.analysis.relevance_sampling_replay import replay_social_sample


def _source_rows():
    return [
        {"channel": "a", "msg_id": 1, "t_kyiv": "2026-11-06T10:00:00+02:00",
         "text_clean": "Подія у Петропавлівській Борщагівці", "links": []},
        {"channel": "b", "msg_id": 2, "t_kyiv": "2026-11-06T10:01:00+02:00",
         "text_clean": "ТЦК і протест", "links": []},
        {"channel": "c", "msg_id": 3, "t_kyiv": "2026-11-06T10:02:00+02:00",
         "text_clean": "Борщагівка", "links": []},
        {"channel": "d", "msg_id": 4, "t_kyiv": "2026-11-06T10:03:00+02:00",
         "text_clean": "звичайне повідомлення", "links": ["https://t.me/news/4"]},
        {"channel": "e", "msg_id": 5, "t_kyiv": "2026-11-06T10:04:00+02:00",
         "text_clean": "звичайний текст", "links": []},
        {"channel": "f", "msg_id": 6, "t_kyiv": "2026-11-01T10:04:00+02:00",
         "text_clean": "Петропавлівська Борщагівка ТЦК", "links": []},
    ]


def test_replay_reconstructs_strata_and_sample_membership_from_social_posts():
    rows = _source_rows()
    prepared = []
    from src.analysis.prep import relevance_decision
    from src.analysis.relevance_sampling_audit_v1 import add_composite_record_ids

    for row in rows:
        relevant, reason = relevance_decision(row["text_clean"], row["t_kyiv"])
        if reason == "outside_time_window":
            continue
        prepared.append({**row, "relevant": relevant, "relevance_reason_code": reason})
    prepared = add_composite_record_ids(prepared, component_keys=("channel", "msg_id"))
    manifest = stratified_relevance_sample(
        prepared,
        sample_sizes={
            "rejected__candidate_no_toponym_context": 1,
            "rejected__candidate_place_only": 1,
            "rejected__candidate_reference_context": 1,
            "rejected__random_remainder": 1,
        },
        seed=20261108,
        id_key="_audit_record_id",
    )

    receipt = replay_social_sample(rows, manifest)

    assert receipt["source_record_count"] == 6
    assert receipt["time_window_excluded_count"] == 1
    assert receipt["in_scope_record_count"] == 5
    assert receipt["selected_record_count"] == 1
    assert receipt["rejected_record_count"] == 4
    assert receipt["strata_match"] is True
    assert receipt["sample_membership_match"] is True
    assert receipt["frame_digest_match"] is True
    assert "sample" not in receipt
    assert "text_clean" not in str(receipt)


def test_replay_reports_frame_digest_mismatch_without_claiming_sample_match():
    rows = _source_rows()
    from src.analysis.prep import relevance_decision
    from src.analysis.relevance_sampling_audit_v1 import add_composite_record_ids

    prepared = []
    for row in rows:
        relevant, reason = relevance_decision(row["text_clean"], row["t_kyiv"])
        if reason == "outside_time_window":
            continue
        prepared.append({**row, "relevant": relevant, "relevance_reason_code": reason})
    prepared = add_composite_record_ids(prepared, component_keys=("channel", "msg_id"))
    manifest = stratified_relevance_sample(
        prepared,
        sample_sizes={
            "rejected__candidate_no_toponym_context": 1,
            "rejected__candidate_place_only": 1,
            "rejected__candidate_reference_context": 1,
            "rejected__random_remainder": 1,
        },
        seed=20261108,
        id_key="_audit_record_id",
    )
    manifest["frame_sha256"] = "0" * 64

    receipt = replay_social_sample(rows, manifest)

    assert receipt["sample_membership_match"] is True
    assert receipt["strata_match"] is True
    assert receipt["frame_digest_match"] is False
