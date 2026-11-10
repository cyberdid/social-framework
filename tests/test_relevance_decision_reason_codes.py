from __future__ import annotations

from datetime import datetime

from src.analysis import build_continuation, prep


CASES = [
    ("2026-11-02T10:59:59+02:00", "Петропавлівська Борщагівка ТЦК", False, "outside_time_window"),
    ("2026-11-02T11:00:00+02:00", "Петропавлівська Борщагівка", True, "explicit_village_name"),
    ("2026-11-02T11:00:00+02:00", "Борщагівка: протест біля ТЦК", True, "place_plus_topic"),
    ("2026-11-02T11:00:01+02:00", "Борщагівка, район Києва", False, "place_without_topic"),
    ("2026-11-02T11:00:01+02:00", "Протест біля ТЦК", False, "topic_without_place"),
    ("2026-11-02T11:00:01+02:00", "Новини дня", False, "neither"),
]


def legacy_decision(text: str, t_kyiv: str) -> bool:
    place = bool(prep.PLACE.search(prep.KYIV_PLACES.sub(" ", text)))
    return t_kyiv >= prep.INCIDENT_FROM and (
        bool(prep.VILLAGE.search(text)) or (place and bool(prep.LEX.search(text)))
    )


def test_shared_helper_preserves_legacy_boolean_and_explains_each_branch():
    for t_kyiv, text, expected, reason in CASES:
        relevant, actual_reason = prep.relevance_decision(text, t_kyiv)
        assert relevant is expected
        assert relevant == legacy_decision(text, t_kyiv)
        assert actual_reason == reason


def test_prep_and_continuation_annotators_emit_same_reason_without_changing_place_or_language():
    for t_kyiv, text, expected, reason in CASES:
        prep_relevant, prep_reason = prep.relevance_decision(text, t_kyiv)
        place = bool(prep.PLACE.search(prep.KYIV_PLACES.sub(" ", text)))
        lang = prep.lang_of(text)

        post = {"channel": "synthetic", "date": t_kyiv, "text": text}
        build_continuation.annotate(post, {})

        assert post["place"] is place
        assert post["relevant"] is expected is prep_relevant
        assert post["relevance_reason_code"] == reason == prep_reason
        assert post["lang"] == lang
        assert post["t_kyiv"] == t_kyiv


def test_reason_codes_are_stable_machine_readable_values():
    assert {reason for _, _, _, reason in CASES} == {
        "outside_time_window", "explicit_village_name", "place_plus_topic",
        "place_without_topic", "topic_without_place", "neither",
    }
