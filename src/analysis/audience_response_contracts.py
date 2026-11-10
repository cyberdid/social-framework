"""Strict contracts for privacy-safe comment reaction snapshots."""
from __future__ import annotations

from src.contracts import ContractValidationError, validate_document


REACTION_STATES = frozenset({
    "not_reported", "empty", "available", "current_user_excluded"
})


def validate_reaction_snapshot(payload: dict) -> str:
    """Validate a complete v1 reaction snapshot and return its explicit state."""
    validate_document(payload, "comment_reaction_snapshot.schema.json")
    return payload["reaction_state"]


def comment_reaction_state(record: dict) -> str:
    """Return an explicit reaction state, keeping legacy absence distinct from zero.

    Historical comment records predate typed reaction fields. Only complete absence
    of both typed fields means ``not_collected``; null or malformed explicit values
    are invalid, and the legacy numeric ``reactions`` sum is never used to infer state.
    """
    has_state = "reaction_state" in record
    has_breakdown = "reaction_breakdown" in record
    if not has_state:
        if has_breakdown or "reaction_observed_at" in record:
            raise ContractValidationError(
                "typed reaction fields must include reaction_state"
            )
        return "not_collected"

    state = record["reaction_state"]
    if state not in REACTION_STATES:
        raise ContractValidationError("reaction_state must be an explicit supported state")
    if not has_breakdown:
        raise ContractValidationError(
            "typed reaction fields must include reaction_breakdown, including explicit null"
        )
    breakdown = record.get("reaction_breakdown")
    if state == "not_reported" and breakdown is not None:
        raise ContractValidationError("not_reported requires reaction_breakdown=null")
    if state == "empty" and breakdown != []:
        raise ContractValidationError("empty requires reaction_breakdown=[]")
    if state == "available":
        if not isinstance(breakdown, list) or not breakdown:
            raise ContractValidationError("available requires a non-empty reaction_breakdown")
    if state == "current_user_excluded":
        if not isinstance(breakdown, list):
            raise ContractValidationError("current_user_excluded requires a reaction_breakdown list")
    if state in {"available", "current_user_excluded"}:
        for item in breakdown:
            if not isinstance(item, dict):
                raise ContractValidationError("reaction_breakdown entries must be objects")
            count = item.get("count")
            if type(count) is not int or count < 0:
                raise ContractValidationError("reaction counts must be non-negative integers")
    return state
