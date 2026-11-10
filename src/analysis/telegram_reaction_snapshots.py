"""Bounded aggregate-only refresh of Telegram discussion comment reactions."""

from datetime import datetime, timezone

from telethon import errors, functions, types, utils

from src.tg_comments_collect import serialize_reaction_snapshot


MAX_MESSAGE_IDS = 10


def _error_code(exc: Exception) -> str:
    """Map Telegram failures to safe codes; never expose RPC error details."""
    if isinstance(exc, (
        errors.FloodWaitError,
        errors.FloodPremiumWaitError,
        errors.SlowModeWaitError,
        errors.FloodTestPhoneWaitError,
    )):
        return "flood_wait"
    if isinstance(exc, errors.ChannelPrivateError):
        return "channel_private"
    if isinstance(exc, errors.ChannelInvalidError):
        return "channel_invalid"
    if isinstance(exc, (errors.MsgIdInvalidError, errors.MessageIdInvalidError)):
        return "message_invalid"
    if isinstance(exc, errors.PeerIdInvalidError):
        return "peer_invalid"
    if isinstance(exc, (errors.ChatAdminRequiredError, errors.UserBannedInChannelError)):
        return "access_denied"
    return "request_failed"


def _result_row(
    comment_id: int,
    request_status: str,
    reaction_state: str | None,
    reaction_breakdown: list[dict[str, object]] | None,
    attempted_at_utc: str,
    error_code: str | None = None,
) -> dict[str, object]:
    row: dict[str, object] = {
        "comment_id": comment_id,
        "request_status": request_status,
        "reaction_state": reaction_state,
        "reaction_breakdown": reaction_breakdown,
        "request_attempted_at_utc": attempted_at_utc,
    }
    if request_status == "ok":
        row["reaction_observed_at"] = attempted_at_utc
    if error_code is not None:
        row["error_code"] = error_code
    return row


async def refresh_min_reactions(client, discussion_peer, message_ids: list[int]) -> list[dict[str, object]]:
    """Refresh aggregate reactions for at most ten selected comment IDs.

    ``discussion_peer`` must be the linked discussion group. This operation
    issues one ``messages.getMessagesReactions`` request, with no retries,
    sleeps, polling, reactor-list queries, collection, or output writes.
    """
    if not isinstance(message_ids, list):
        raise ValueError("message_ids must be a list of unique positive integers")
    if any(type(message_id) is not int or message_id <= 0 for message_id in message_ids):
        raise ValueError("message_ids must contain only positive integers")
    if len(set(message_ids)) != len(message_ids):
        raise ValueError("message_ids must not contain duplicates")
    if len(message_ids) > MAX_MESSAGE_IDS:
        raise ValueError(f"at most {MAX_MESSAGE_IDS} message IDs may be refreshed per request")
    if not message_ids:
        return []

    attempted_at_utc = datetime.now(timezone.utc).isoformat()
    request = functions.messages.GetMessagesReactionsRequest(
        peer=discussion_peer,
        id=list(message_ids),
    )

    # Telethon normally sleeps on short FloodWaits and retries transient RPC
    # failures. Temporarily disable those client policies for this bounded call.
    original_client_settings = {}
    for attribute in ("flood_sleep_threshold", "_request_retries"):
        if hasattr(client, attribute):
            original_client_settings[attribute] = getattr(client, attribute)
            setattr(client, attribute, 0)
    try:
        response = await client(request)
    except Exception as exc:
        code = _error_code(exc)
        return [
            _result_row(
                comment_id,
                "request_failed",
                None,
                None,
                attempted_at_utc,
                code,
            )
            for comment_id in message_ids
        ]
    finally:
        for attribute, value in original_client_settings.items():
            setattr(client, attribute, value)

    expected_peer_id = utils.get_peer_id(discussion_peer)
    reactions_by_id = {}
    wrong_peer_ids = set()
    for update in getattr(response, "updates", ()) or ():
        if not isinstance(update, types.UpdateMessageReactions) or update.msg_id not in message_ids:
            continue
        if utils.get_peer_id(update.peer) != expected_peer_id:
            wrong_peer_ids.add(update.msg_id)
            continue
        reactions_by_id[update.msg_id] = update.reactions

    rows = []
    for comment_id in message_ids:
        if comment_id in reactions_by_id:
            snapshot = serialize_reaction_snapshot(reactions_by_id[comment_id], attempted_at_utc)
            rows.append(_result_row(
                comment_id,
                "ok",
                snapshot["reaction_state"],
                snapshot["reaction_breakdown"],
                attempted_at_utc,
            ))
        else:
            request_status = "wrong_peer_update" if comment_id in wrong_peer_ids else "no_update"
            rows.append(_result_row(comment_id, request_status, None, None, attempted_at_utc))
    return rows
