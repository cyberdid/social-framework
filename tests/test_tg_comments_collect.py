from types import SimpleNamespace

import pytest
from telethon.tl import types

import src.tg_comments_collect as comments_collect


OBSERVED_AT = "2026-11-08T12:00:00+00:00"


def _serialize(reactions):
    serializer = getattr(comments_collect, "serialize_reaction_snapshot", None)
    assert callable(serializer), "comment collector must serialize typed reaction snapshots"
    return serializer(reactions, OBSERVED_AT)


def test_missing_reaction_object_is_not_reported_not_analytical_zero():
    assert _serialize(None) == {
        "legacy_total": 0,
        "reaction_breakdown": None,
        "reaction_state": "not_reported",
        "reaction_observed_at": OBSERVED_AT,
    }


def test_present_empty_reaction_object_is_distinct_from_missing_object():
    reactions = types.MessageReactions(results=[], min=False)

    assert _serialize(reactions) == {
        "legacy_total": 0,
        "reaction_breakdown": [],
        "reaction_state": "empty",
        "reaction_observed_at": OBSERVED_AT,
    }


def test_standard_custom_and_paid_reactions_keep_separate_typed_counts():
    reactions = types.MessageReactions(
        results=[
            types.ReactionCount(types.ReactionEmoji("👍"), 12),
            types.ReactionCount(types.ReactionCustomEmoji(2**60 + 7), 3),
            types.ReactionCount(types.ReactionPaid(), 5),
        ],
        min=False,
        recent_reactions=[SimpleNamespace(peer_id=987654321)],
    )

    result = _serialize(reactions)

    assert result == {
        "legacy_total": 20,
        "reaction_breakdown": [
            {"kind": "emoji", "key": "👍", "count": 12},
            {"kind": "custom_emoji", "key": "document:1152921504606846983", "count": 3},
            {"kind": "paid", "key": "paid", "count": 5},
        ],
        "reaction_state": "available",
        "reaction_observed_at": OBSERVED_AT,
    }
    assert "peer_id" not in repr(result)


def test_min_reaction_object_is_marked_current_user_excluded():
    reactions = types.MessageReactions(
        results=[types.ReactionCount(types.ReactionEmoji("❤️"), 2)],
        min=True,
    )

    assert _serialize(reactions) == {
        "legacy_total": 2,
        "reaction_breakdown": [{"kind": "emoji", "key": "❤️", "count": 2}],
        "reaction_state": "current_user_excluded",
        "reaction_observed_at": OBSERVED_AT,
    }


def test_unknown_future_constructor_is_preserved_without_crashing():
    class FutureReaction:
        pass

    reactions = SimpleNamespace(
        results=[SimpleNamespace(reaction=FutureReaction(), count=4)],
        min=False,
    )

    assert _serialize(reactions)["reaction_breakdown"] == [
        {"kind": "unknown", "key": "FutureReaction", "count": 4}
    ]


@pytest.mark.parametrize("invalid_count", [1.5, "2"])
def test_malformed_reaction_count_is_rejected_without_coercion(invalid_count):
    reactions = SimpleNamespace(
        results=[SimpleNamespace(reaction=types.ReactionEmoji("👍"), count=invalid_count)],
        min=False,
    )

    with pytest.raises(ValueError, match="non-negative integer"):
        _serialize(reactions)
