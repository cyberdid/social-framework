"""Evidence-bounded aggregation of social conflict episode records.

This module organizes previously extracted, sourced statements and events. It
does not identify motives, infer causality, or estimate the views of a group.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from math import isfinite
from typing import Any


STATEMENT_CATEGORIES = {
    "trigger",
    "public_demand",
    "stated_grievance",
    "institutional_context",
    "structural_hypothesis",
    "information_interpretation",
    "participant_role",
    "institutional_response",
    "alternative_explanation",
}
ACTION_CATEGORIES = {
    "assembly",
    "blocking",
    "physical_contact",
    "detention",
    "medical_aid",
    "negotiation",
    "dispersal",
    "other",
    "unclassified",
}
VERIFIED_STATUSES = {"supported_within_scope", "verified_within_scope", "confirmed_within_scope"}
REFUTED_STATUSES = {"refuted_within_scope", "superseded"}


def _validate_utc(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a timezone-aware UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} must be UTC")
    return parsed.isoformat().replace("+00:00", "Z")


def _validate_refs(record: dict, record_id: str, unresolved: list[dict]) -> list[str]:
    evidence_ids = record.get("evidence_ids") or []
    if not isinstance(evidence_ids, list) or any(not isinstance(ref, str) or not ref.strip() for ref in evidence_ids):
        raise ValueError(f"evidence_ids for {record_id} must be a list of non-empty strings")
    refs = sorted(set(evidence_ids))
    if not refs:
        unresolved.append({
            "record_id": record_id,
            "record_type": record.get("event_kind", record.get("category", "unknown")),
            "reason": "missing_evidence_references",
            "source_refs": [],
        })
    return refs


def _copy_statement(record: dict, episode_id: str, unresolved: list[dict]) -> dict | None:
    statement_id = str(record.get("statement_id") or "").strip()
    if not statement_id:
        raise ValueError("statement_id is required")
    if record.get("episode_id") != episode_id:
        raise ValueError(f"episode_id mismatch for statement {statement_id}")
    category = record.get("category")
    if category not in STATEMENT_CATEGORIES:
        raise ValueError(f"unsupported statement category for {statement_id}: {category}")
    refs = _validate_refs(record, statement_id, unresolved)
    if not refs:
        return None
    if not isinstance(record.get("text"), str) or not record["text"].strip():
        unresolved.append({
            "record_id": statement_id,
            "record_type": category,
            "reason": "empty_statement_text",
            "source_refs": refs,
        })
        return None
    represented_group = record.get("represented_group_id")
    raw_representation_refs = record.get("representation_evidence_ids") or []
    if not isinstance(raw_representation_refs, list) or any(not isinstance(ref, str) or not ref.strip() for ref in raw_representation_refs):
        raise ValueError(f"representation_evidence_ids for {statement_id} must be non-empty strings")
    representation_refs = sorted(set(raw_representation_refs))
    representation_status = (
        "evidence_recorded_not_independently_validated"
        if represented_group and representation_refs
        else "unestablished"
    )
    role = record.get("speaker_role") or "unknown"
    if not isinstance(role, str):
        raise ValueError(f"speaker_role for {statement_id} must be text")
    result = {
        "statement_id": statement_id,
        "episode_id": episode_id,
        "category": category,
        "text": record["text"].strip(),
        "speaker_entity_id": record.get("speaker_entity_id"),
        "speaker_role": role,
        "role_status": "unknown" if role == "unknown" else "source_attributed",
        "source_id": record.get("source_id"),
        "status": record.get("status") or "reported",
        "source_refs": sorted(set(refs) | set(representation_refs)),
        "limitations": sorted(set(record.get("limitations") or [])),
        "represented_group_id": represented_group,
        "representation_evidence_ids": representation_refs,
        "group_representation_status": representation_status,
        "alternatives": list(record.get("alternatives") or []),
    }
    if category == "structural_hypothesis":
        result["epistemic_status"] = "hypothesis"
    return result


def _copy_event(record: dict, episode_id: str, unresolved: list[dict]) -> dict | None:
    event_id = str(record.get("event_id") or "").strip()
    if not event_id:
        raise ValueError("event_id is required")
    if record.get("episode_id") != episode_id:
        raise ValueError(f"episode_id mismatch for event {event_id}")
    if record.get("event_kind") != "physical":
        raise ValueError(f"physical_event_records contains non-physical event {event_id}")
    refs = _validate_refs(record, event_id, unresolved)
    if not refs:
        return None
    status = record.get("status") or "reported"
    allowed_statuses = {
        "reported", "unverified", "supported_within_scope", "verified_within_scope",
        "confirmed_within_scope", "refuted_within_scope", "conflicting_evidence",
        "insufficient_evidence", "not_verifiable", "superseded",
    }
    if status not in allowed_statuses:
        raise ValueError(f"unsupported evidence status for event {event_id}: {status}")
    action = record.get("action_category") or "unclassified"
    if action not in ACTION_CATEGORIES:
        action = "unclassified"
    time_value = record.get("time_utc")
    time_utc = _validate_utc(time_value, f"event {event_id}.time_utc") if time_value else None
    return {
        "event_id": event_id,
        "episode_id": episode_id,
        "event_kind": "physical",
        "action_category": action,
        "status": status,
        "time_utc": time_utc,
        "actor_entity_ids": sorted(set(record.get("actor_entity_ids") or [])),
        "place_id": record.get("place_id"),
        "source_refs": refs,
        "provenance_family_id": record.get("provenance_family_id"),
        "limitations": sorted(set(record.get("limitations") or [])),
    }


def _information_row(record: dict, episode_id: str) -> dict:
    if record.get("episode_id") not in (None, episode_id):
        raise ValueError("information attention record has mismatched episode_id")
    start = _validate_utc(record.get("interval_start_utc"), "information.interval_start_utc")
    end = _validate_utc(record.get("interval_end_utc"), "information.interval_end_utc")
    if datetime.fromisoformat(start.replace("Z", "+00:00")) > datetime.fromisoformat(end.replace("Z", "+00:00")):
        raise ValueError("information interval must start at or before its end")
    refs = record.get("source_refs") or []
    if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError("information source_refs must contain at least one non-empty reference")
    metrics = record.get("metrics") or {}
    if not isinstance(metrics, dict):
        raise ValueError("information metrics must be an object")
    for name, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"information metric {name} must be a non-negative number")
        if not isfinite(value):
            raise ValueError(f"information metric {name} must be finite")
        if value < 0:
            raise ValueError(f"information metric {name} must be non-negative")
    return {
        "episode_id": episode_id,
        "interval_start_utc": start,
        "interval_end_utc": end,
        "source_id": record.get("source_id"),
        "source_refs": sorted(set(refs)),
        "metrics": dict(sorted(metrics.items())),
        "view_counter_semantics": "snapshot_counter_not_unique_people",
        "limitations": sorted(set(record.get("limitations") or [])),
    }


def analyze_conflict_episode(
    episode_id: str,
    physical_event_records: list[dict],
    position_records: list[dict],
    information_attention_records: list[dict],
    *,
    context: dict | None = None,
) -> dict:
    """Aggregate evidence-linked records without inferring group opinion/motive.

    The physical action vector is computed solely from supplied physical event
    records. Information-attention counts and views are carried on a separate
    series and cannot affect the vector.
    """
    episode_id = str(episode_id).strip()
    if not episode_id:
        raise ValueError("episode_id is required")
    context = dict(context or {})
    unresolved: list[dict] = []
    events = []
    seen_event_ids = set()
    for record in physical_event_records:
        raw_event_id = str(record.get("event_id") or "").strip()
        if raw_event_id and raw_event_id in seen_event_ids:
            raise ValueError(f"duplicate event_id: {raw_event_id}")
        if raw_event_id:
            seen_event_ids.add(raw_event_id)
        copied = _copy_event(record, episode_id, unresolved)
        if copied is None:
            continue
        events.append(copied)
    events.sort(key=lambda item: (
        datetime.fromisoformat(item["time_utc"].replace("Z", "+00:00")).timestamp() if item["time_utc"] else float("inf"),
        item["event_id"],
    ))

    statements = []
    seen_statement_ids = set()
    for record in position_records:
        raw_statement_id = str(record.get("statement_id") or "").strip()
        if raw_statement_id and raw_statement_id in seen_statement_ids:
            raise ValueError(f"duplicate statement_id: {raw_statement_id}")
        if raw_statement_id:
            seen_statement_ids.add(raw_statement_id)
        copied = _copy_statement(record, episode_id, unresolved)
        if copied is None:
            continue
        statements.append(copied)
    statements.sort(key=lambda item: item["statement_id"])

    attention = sorted(
        (_information_row(row, episode_id) for row in information_attention_records),
        key=lambda row: (
            datetime.fromisoformat(row["interval_start_utc"].replace("Z", "+00:00")),
            datetime.fromisoformat(row["interval_end_utc"].replace("Z", "+00:00")),
            row.get("source_id") or "",
        ),
    )

    indicator_vector = {}
    for category in sorted(ACTION_CATEGORIES):
        category_events = [row for row in events if row["action_category"] == category]
        indicator_vector[category] = {
            "event_record_count": len(category_events),
            "reported_record_count": sum(row["status"] not in (VERIFIED_STATUSES | REFUTED_STATUSES) for row in category_events),
            "supported_within_scope_record_count": sum(row["status"] in VERIFIED_STATUSES for row in category_events),
            "refuted_or_superseded_record_count": sum(row["status"] in REFUTED_STATUSES for row in category_events),
            "event_ids": [row["event_id"] for row in category_events],
            "provenance_family_ids": sorted({row["provenance_family_id"] for row in category_events if row["provenance_family_id"]}),
            "limitations": [
                "Counts describe evidence-linked event records in the supplied corpus, not real-world event frequency or severity.",
                "Unknown lineage is not assumed independent.",
            ],
        }

    grouped = {category: [] for category in STATEMENT_CATEGORIES}
    for statement in statements:
        grouped[statement["category"]].append(statement)

    return {
        "schema_version": "1.0",
        "episode_id": episode_id,
        "incident_id": context.get("incident_id"),
        "run_id": context.get("run_id"),
        "producer": "social.analysis.conflict_analysis@1.0",
        "trigger": grouped["trigger"],
        "public_demands": grouped["public_demand"],
        "stated_grievances": grouped["stated_grievance"],
        "institutional_context": grouped["institutional_context"],
        "structural_hypotheses": grouped["structural_hypothesis"],
        "information_interpretations": grouped["information_interpretation"],
        "participants_and_roles": grouped["participant_role"],
        "institutional_response": grouped["institutional_response"],
        "alternative_explanations": grouped["alternative_explanation"],
        "observable_actions": deepcopy(events),
        "escalation_indicators": indicator_vector,
        "physical_event_series": deepcopy(events),
        "information_attention_series": attention,
        "unresolved": sorted(unresolved, key=lambda row: (row["record_id"], row["reason"])),
        "limitations": [
            "The output is bounded by supplied, evidence-linked records and their coverage.",
            "A source-attributed statement is never generalized to a group position by this module; representation evidence is only recorded and is not independently validated here.",
            "A reported event is not promoted to a verified real-world event by this aggregation.",
            "Information activity and view counters do not change physical-action indicators.",
            "No hidden motive or causal relationship is inferred.",
        ],
        "source_refs": sorted(
            {ref for row in events for ref in row["source_refs"]}
            | {ref for row in statements for ref in row["source_refs"]}
            | {ref for row in attention for ref in row["source_refs"]}
        ),
    }
