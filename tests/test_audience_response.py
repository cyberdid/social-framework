import pytest

from src.analysis.audience_response import (
    InterpretationValidationError,
    normalize_interpretations,
)


def row(target_id="claim:police-action", dimension="stance", label="support"):
    return {
        "target_id": target_id,
        "dimension": dimension,
        "label": label,
        "language": "uk",
        "model_id": "local/xlm-r",
        "model_version": "sha256:abc",
        "context_ids": [],
        "evidence_span": {"start": 0, "end": len("Підтримую цю дію"), "text": "Підтримую цю дію"},
        "score_kind": "uncalibrated_score",
        "abstention_reason": None,
    }


def normalize(rows, text="Підтримую цю дію", targets=None, contexts=None, context_texts=None):
    options = {
        "comment_id": "comment:1",
        "comment_text": text,
        "known_target_ids": targets or {"claim:police-action", "entity:police", "event:protest"},
        "known_context_ids": contexts or {"post:1", "comment:parent"},
    }
    if context_texts is not None:
        options["known_context_texts"] = context_texts
    return normalize_interpretations(
        rows,
        **options,
    )


def test_one_comment_can_have_interpretations_for_multiple_targets():
    first = row()
    second = row("entity:police", "sentiment", "negative")
    second["evidence_span"] = {"start": 0, "end": len("Підтримую цю дію"), "text": "Підтримую цю дію"}
    result = normalize([first, second])
    assert {item["target_id"] for item in result} == {"claim:police-action", "entity:police"}
    assert all(item["comment_id"] == "comment:1" for item in result)
    assert len({item["interpretation_id"] for item in result}) == 2


def test_context_only_reply_may_have_no_text_span_but_keeps_parent_reference():
    item = row(label="support")
    item["context_ids"] = ["post:1", "comment:parent"]
    item["evidence_span"] = None
    result = normalize([item], text="Повністю згоден")
    assert result[0]["evidence_span"] is None
    assert result[0]["context_ids"] == ["post:1", "comment:parent"]


@pytest.mark.parametrize("reason", ["sarcasm_or_irony", "stance_unclear"])
def test_ambiguous_or_sarcastic_stance_is_explicitly_abstained(reason):
    item = row(label="abstain")
    item["abstention_reason"] = reason
    result = normalize([item])
    assert result[0]["label"] == "abstain"
    assert result[0]["abstention_reason"] == reason


def test_emoji_only_or_empty_text_cannot_be_given_inferred_stance():
    item = row(label="support")
    item["evidence_span"] = None
    with pytest.raises(InterpretationValidationError, match="emoji-only"):
        normalize([item], text="😂")


@pytest.mark.parametrize("dimension", ["sentiment", "emotion"])
def test_emoji_only_comment_cannot_receive_sentiment_or_emotion_from_parent_context(dimension):
    context_text = "Я дуже засмучений цією дією."
    item = row(dimension=dimension, label="negative")
    item["evidence_span"] = None
    item["context_ids"] = ["comment:parent"]
    item["context_evidence_span"] = {
        "context_id": "comment:parent",
        "start": 0,
        "end": len(context_text),
        "text": context_text,
    }
    with pytest.raises(InterpretationValidationError, match="emoji-only"):
        normalize(
            [item], text="😂", context_texts={"comment:parent": context_text}
        )


@pytest.mark.parametrize(("dimension", "label"), [
    ("stance", "support"),
    ("topic", "police_action"),
])
def test_emoji_only_comment_can_use_a_text_bearing_context_for_stance_or_topic(dimension, label):
    context_text = "Підтримую дію поліції"
    item = row(dimension=dimension, label=label)
    item["evidence_span"] = None
    item["context_ids"] = ["comment:parent"]
    item["context_evidence_span"] = {
        "context_id": "comment:parent",
        "start": 0,
        "end": len(context_text),
        "text": context_text,
    }
    result = normalize(
        [item], text="😂", context_texts={"comment:parent": context_text}
    )
    assert result[0]["label"] == label
    assert result[0]["context_evidence_span"]["text"] == context_text


def test_emoji_only_context_span_does_not_qualify_as_text_bearing_evidence():
    item = row(dimension="emotion", label="joy")
    item["evidence_span"] = None
    item["context_ids"] = ["comment:parent"]
    item["context_evidence_span"] = {
        "context_id": "comment:parent", "start": 0, "end": 1, "text": "😂",
    }
    with pytest.raises(InterpretationValidationError, match="text-bearing"):
        normalize([item], text="😂", context_texts={"comment:parent": "😂"})


def test_mixed_ukrainian_russian_language_is_preserved():
    item = row()
    item["language"] = "uk_ru"
    text = "Та ну, поддерживаю"
    item["evidence_span"] = {"start": 0, "end": len(text), "text": text}
    assert normalize([item], text=text)[0]["language"] == "uk_ru"


def test_unknown_target_and_context_ids_are_rejected():
    with pytest.raises(InterpretationValidationError, match="target"):
        normalize([row("unregistered:police")])
    item = row()
    item["context_ids"] = ["comment:not-collected"]
    with pytest.raises(InterpretationValidationError, match="context"):
        normalize([item])


def test_span_offsets_are_unicode_codepoint_offsets_and_must_match_text():
    text = "👍 дія"
    item = row()
    item["evidence_span"] = {"start": 0, "end": 2, "text": "👍 "}
    result = normalize([item], text=text)
    assert text[result[0]["evidence_span"]["start"]:result[0]["evidence_span"]["end"]] == "👍 "
    item["evidence_span"] = {"start": 0, "end": 2, "text": "👍"}
    with pytest.raises(InterpretationValidationError, match="span"):
        normalize([item], text=text)


def test_duplicate_interpretation_rows_are_rejected():
    item = row()
    with pytest.raises(InterpretationValidationError, match="duplicate"):
        normalize([item, item])
