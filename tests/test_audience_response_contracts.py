import pytest

from src.analysis.audience_response_contracts import (
    ContractValidationError,
    comment_reaction_state,
    validate_reaction_snapshot,
)


def snapshot(state="available", breakdown=None):
    if breakdown is None and state == "available":
        breakdown = [{"kind": "emoji", "key": "👍", "count": 2}]
    elif breakdown is None and state == "empty":
        breakdown = []
    elif breakdown is None and state == "not_reported":
        breakdown = None
    return {
        "schema_version": "1.0",
        "snapshot_id": "tg:comment:42@2026-11-08T12:00:00Z",
        "platform": "telegram",
        "comment_id": "42",
        "source_post_id": "9001",
        "thread_id": "discussion:17",
        "parent_comment_id": None,
        "author_pseudonym": "hmac:abc123",
        "published_at_utc": "2026-11-08T11:45:00Z",
        "observed_at_utc": "2026-11-08T12:00:00Z",
        "reaction_state": state,
        "reaction_breakdown": breakdown,
        "source_snapshot_ref": "snapshot:2026-11-08T12:00:00Z",
    }


@pytest.mark.parametrize("state", ["available", "empty", "not_reported", "current_user_excluded"])
def test_valid_reaction_snapshot_preserves_state_and_shape(state):
    payload = snapshot(state)
    if state == "current_user_excluded":
        payload["reaction_breakdown"] = [{"kind": "emoji", "key": "❤️", "count": 1}]
    assert validate_reaction_snapshot(payload) == state


def test_current_user_excluded_snapshot_can_have_an_explicit_empty_vector():
    payload = snapshot("current_user_excluded", [])
    assert validate_reaction_snapshot(payload) == "current_user_excluded"
    assert comment_reaction_state({
        "reaction_state": "current_user_excluded",
        "reaction_breakdown": [],
    }) == "current_user_excluded"


def test_available_snapshot_still_requires_a_nonempty_vector():
    payload = snapshot("available", [])
    with pytest.raises(ContractValidationError):
        validate_reaction_snapshot(payload)


def test_legacy_row_without_new_reaction_field_is_not_collected_not_zero():
    row = {"reactions": 0}
    assert comment_reaction_state(row) == "not_collected"


@pytest.mark.parametrize("explicit", [None, "not_reported", "empty"])
def test_legacy_state_is_not_inferred_when_field_is_present(explicit):
    row = {"reactions": 0, "reaction_state": explicit}
    with pytest.raises(ContractValidationError):
        comment_reaction_state(row)


@pytest.mark.parametrize("bad_count", [-1, 1.5, "2", True])
def test_reaction_counts_must_be_nonnegative_json_integers(bad_count):
    payload = snapshot()
    payload["reaction_breakdown"][0]["count"] = bad_count
    with pytest.raises(ContractValidationError):
        validate_reaction_snapshot(payload)


def test_unavailable_reaction_states_do_not_accept_zero_vector():
    payload = snapshot("not_reported")
    payload["reaction_breakdown"] = []
    with pytest.raises(ContractValidationError):
        validate_reaction_snapshot(payload)


def test_empty_state_requires_explicit_empty_vector():
    payload = snapshot("empty")
    payload["reaction_breakdown"] = None
    with pytest.raises(ContractValidationError):
        validate_reaction_snapshot(payload)


def test_custom_emoji_identifier_is_string_to_preserve_integer_precision():
    payload = snapshot()
    payload["reaction_breakdown"] = [{"kind": "custom_emoji", "key": "document:9223372036854775807", "count": 3}]
    assert validate_reaction_snapshot(payload) == "available"


def test_platform_time_fields_must_be_utc():
    payload = snapshot()
    payload["observed_at_utc"] = "2026-11-08T14:00:00+02:00"
    with pytest.raises(ContractValidationError):
        validate_reaction_snapshot(payload)
