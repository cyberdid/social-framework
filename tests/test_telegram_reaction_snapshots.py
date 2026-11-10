import asyncio
from datetime import datetime, timezone

import pytest
from telethon import errors, functions, types

from src.analysis.telegram_reaction_snapshots import refresh_min_reactions


class FakeAsyncClient:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class RetryConfiguredFakeClient(FakeAsyncClient):
    def __init__(self, result=None, error=None):
        super().__init__(result=result, error=error)
        self.flood_sleep_threshold = 600
        self._request_retries = 5
        self.settings_during_request = None

    async def __call__(self, request):
        self.settings_during_request = (self.flood_sleep_threshold, self._request_retries)
        return await super().__call__(request)


def _updates(*items):
    return types.Updates(
        updates=list(items),
        users=[],
        chats=[],
        date=datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc),
        seq=0,
    )


def _row_by_comment(rows, comment_id):
    return next(row for row in rows if row["comment_id"] == comment_id)


def _refresh(client, discussion_peer, message_ids):
    return asyncio.run(refresh_min_reactions(client, discussion_peer, message_ids))


def test_empty_ids_return_without_sending_a_telegram_request():
    client = FakeAsyncClient(result=_updates())

    assert _refresh(client, types.InputPeerChannel(101, b"a" * 16), []) == []
    assert client.requests == []


def test_multi_id_refresh_sends_one_exact_discussion_peer_request_and_returns_local_rows():
    discussion_peer = types.InputPeerChannel(101, b"a" * 16)
    client = FakeAsyncClient(result=_updates(
        types.UpdateMessageReactions(
            peer=types.PeerChannel(101),
            msg_id=8,
            reactions=types.MessageReactions(
                results=[types.ReactionCount(types.ReactionEmoji("👍"), 3)],
                min=False,
            ),
            top_msg_id=4,
        )
    ))

    rows = _refresh(client, discussion_peer, [8, 9])

    assert len(client.requests) == 1
    request = client.requests[0]
    assert type(request) is functions.messages.GetMessagesReactionsRequest
    assert request.peer == discussion_peer
    assert request.id == [8, 9]
    assert set(_row_by_comment(rows, 8)) == {
        "comment_id", "request_status", "reaction_state", "reaction_breakdown",
        "reaction_observed_at", "request_attempted_at_utc",
    }
    assert _row_by_comment(rows, 8)["request_status"] == "ok"
    assert _row_by_comment(rows, 8)["reaction_state"] == "available"
    assert _row_by_comment(rows, 8)["reaction_breakdown"] == [
        {"kind": "emoji", "key": "👍", "count": 3}
    ]
    assert _row_by_comment(rows, 9)["request_status"] == "no_update"
    assert _row_by_comment(rows, 9)["reaction_state"] is None
    assert _row_by_comment(rows, 9)["reaction_breakdown"] is None
    assert "reaction_observed_at" not in _row_by_comment(rows, 9)
    assert datetime.fromisoformat(_row_by_comment(rows, 8)["reaction_observed_at"]).tzinfo is not None
    for row in rows:
        assert "peer" not in row and "username" not in row and "user_id" not in row


def test_standard_custom_and_paid_aggregates_are_preserved_without_reactor_identity():
    client = FakeAsyncClient(result=_updates(
        types.UpdateMessageReactions(
            peer=types.PeerChannel(101),
            msg_id=8,
            reactions=types.MessageReactions(
                results=[
                    types.ReactionCount(types.ReactionEmoji("❤️"), 4),
                    types.ReactionCount(types.ReactionCustomEmoji(2**60 + 7), 2),
                    types.ReactionCount(types.ReactionPaid(), 6),
                ],
                min=False,
                recent_reactions=[types.MessagePeerReaction(
                    peer_id=types.PeerUser(777),
                    date=datetime(2026, 11, 8, tzinfo=timezone.utc),
                    reaction=types.ReactionEmoji("❤️"),
                )],
                top_reactors=[types.MessageReactor(
                    peer_id=types.PeerUser(888),
                    count=1,
                    my=False,
                )],
            ),
        )
    ))

    rows = _refresh(client, types.InputPeerChannel(101, b"a" * 16), [8])

    assert rows[0]["reaction_breakdown"] == [
        {"kind": "emoji", "key": "❤️", "count": 4},
        {"kind": "custom_emoji", "key": "document:1152921504606846983", "count": 2},
        {"kind": "paid", "key": "paid", "count": 6},
    ]
    assert rows[0]["reaction_state"] == "available"
    serialized = repr(rows)
    assert "777" not in serialized
    assert "888" not in serialized
    assert "peer_id" not in serialized
    assert "recent_reactions" not in serialized


def test_empty_reaction_object_is_not_reported_as_zero_or_missing_update():
    client = FakeAsyncClient(result=_updates(
        types.UpdateMessageReactions(
            peer=types.PeerChannel(101),
            msg_id=8,
            reactions=types.MessageReactions(results=[], min=False),
        )
    ))

    rows = _refresh(client, types.InputPeerChannel(101, b"a" * 16), [8, 9])

    assert _row_by_comment(rows, 8)["reaction_state"] == "empty"
    assert _row_by_comment(rows, 8)["reaction_breakdown"] == []
    assert _row_by_comment(rows, 9)["request_status"] == "no_update"
    assert _row_by_comment(rows, 9)["reaction_state"] is None
    assert _row_by_comment(rows, 9)["reaction_breakdown"] is None


def test_update_for_same_message_id_in_another_peer_is_not_correlated():
    client = FakeAsyncClient(result=_updates(
        types.UpdateMessageReactions(
            peer=types.PeerChannel(202),
            msg_id=8,
            reactions=types.MessageReactions(
                results=[types.ReactionCount(types.ReactionEmoji("👍"), 3)],
                min=False,
            ),
        )
    ))

    rows = _refresh(client, types.InputPeerChannel(101, b"a" * 16), [8])

    assert rows[0]["request_status"] == "wrong_peer_update"
    assert rows[0]["reaction_state"] is None
    assert rows[0]["reaction_breakdown"] is None


def test_matching_update_without_reaction_object_is_not_reported_not_no_update():
    client = FakeAsyncClient(result=_updates(
        types.UpdateMessageReactions(
            peer=types.PeerChannel(101),
            msg_id=8,
            reactions=None,
        )
    ))

    row = _refresh(client, types.InputPeerChannel(101, b"a" * 16), [8])[0]

    assert row["request_status"] == "ok"
    assert row["reaction_state"] == "not_reported"
    assert row["reaction_breakdown"] is None
    assert "reaction_observed_at" in row


def test_min_flag_is_preserved_as_current_user_excluded():
    client = FakeAsyncClient(result=_updates(
        types.UpdateMessageReactions(
            peer=types.PeerChannel(101),
            msg_id=8,
            reactions=types.MessageReactions(
                results=[types.ReactionCount(types.ReactionEmoji("😡"), 1)],
                min=True,
            ),
        )
    ))

    rows = _refresh(client, types.InputPeerChannel(101, b"a" * 16), [8])

    assert rows[0]["reaction_state"] == "current_user_excluded"
    assert rows[0]["reaction_breakdown"] == [{"kind": "emoji", "key": "😡", "count": 1}]


@pytest.mark.parametrize(
    ("error_name", "error_code"),
    [
        ("ChannelPrivateError", "channel_private"),
        ("ChannelInvalidError", "channel_invalid"),
        ("MsgIdInvalidError", "message_invalid"),
        ("MessageIdInvalidError", "message_invalid"),
        ("PeerIdInvalidError", "peer_invalid"),
        ("ChatAdminRequiredError", "access_denied"),
    ],
)
def test_telegram_scope_errors_are_sanitized_per_requested_comment(error_name, error_code):
    request = functions.messages.GetMessagesReactionsRequest(
        peer=types.InputPeerChannel(101, b"a" * 16), id=[8, 9]
    )
    client = FakeAsyncClient(error=getattr(errors, error_name)(request))

    rows = _refresh(client, request.peer, request.id)

    assert len(rows) == 2
    assert [row["request_status"] for row in rows] == ["request_failed", "request_failed"]
    assert [row["reaction_state"] for row in rows] == [None, None]
    assert all("reaction_observed_at" not in row for row in rows)
    assert all("request_attempted_at_utc" in row for row in rows)
    assert [row["error_code"] for row in rows] == [error_code, error_code]
    assert all(set(row) == {
        "comment_id", "request_status", "reaction_state", "reaction_breakdown",
        "request_attempted_at_utc", "error_code",
    } for row in rows)
    assert all(
        datetime.fromisoformat(row["request_attempted_at_utc"]).utcoffset() == timezone.utc.utcoffset(None)
        for row in rows
    )


def test_flood_wait_is_reported_without_retry_or_sleep():
    request = functions.messages.GetMessagesReactionsRequest(
        peer=types.InputPeerChannel(101, b"a" * 16), id=[8]
    )
    client = RetryConfiguredFakeClient(error=errors.FloodWaitError(request, capture=37))

    rows = _refresh(client, request.peer, request.id)

    assert len(client.requests) == 1
    assert client.settings_during_request == (0, 0)
    assert client.flood_sleep_threshold == 600
    assert client._request_retries == 5
    assert rows[0]["request_status"] == "request_failed"
    assert rows[0]["reaction_state"] is None
    assert "reaction_observed_at" not in rows[0]
    assert rows[0]["error_code"] == "flood_wait"
    assert set(rows[0]) == {
        "comment_id", "request_status", "reaction_state", "reaction_breakdown",
        "request_attempted_at_utc", "error_code",
    }
    assert datetime.fromisoformat(rows[0]["request_attempted_at_utc"]).utcoffset() == timezone.utc.utcoffset(None)


def test_unexpected_errors_are_sanitized_and_do_not_echo_private_details():
    client = FakeAsyncClient(error=RuntimeError("session=secret peer=101 message=8 @alice"))

    rows = _refresh(client, types.InputPeerChannel(101, b"a" * 16), [8])

    assert len(rows) == 1
    assert rows[0]["comment_id"] == 8
    assert rows[0]["request_status"] == "request_failed"
    assert rows[0]["reaction_state"] is None
    assert "reaction_observed_at" not in rows[0]
    assert rows[0]["reaction_breakdown"] is None
    assert rows[0]["error_code"] == "request_failed"
    assert "secret" not in repr(rows) and "alice" not in repr(rows)


@pytest.mark.parametrize("message_ids", [[0], [-1], [True], ["8"], [8, 8], list(range(1, 12))])
def test_invalid_duplicate_or_over_limit_comment_ids_are_rejected_before_network_call(message_ids):
    client = FakeAsyncClient(result=_updates())

    with pytest.raises(ValueError):
        _refresh(client, types.InputPeerChannel(101, b"a" * 16), message_ids)

    assert client.requests == []


def test_ten_comment_ids_fit_in_one_bounded_request():
    client = FakeAsyncClient(result=_updates())

    rows = _refresh(client, types.InputPeerChannel(101, b"a" * 16), list(range(1, 11)))

    assert len(rows) == 10
    assert len(client.requests) == 1
    assert client.requests[0].id == list(range(1, 11))
