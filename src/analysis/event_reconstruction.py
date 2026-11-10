"""Conservative, evidence-linked event and timeline reconstruction primitives.

This module preserves event mentions as separate records. It does not infer
real-world truth, merge people/events from fuzzy similarity, or infer causation
from temporal order.
"""
from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime, timezone
from itertools import combinations
from typing import Any


EVENT_KINDS = {"physical", "communication"}


def _norm(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _string_list(value: Any, field: str, *, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{field} must be a list of non-empty strings")
    return sorted(set(item.strip() for item in value))


def _utc(value: Any, field: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be an ISO-8601 timestamp with timezone")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp with timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} must be UTC")
    parsed = parsed.astimezone(timezone.utc)
    normalized = parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds").replace("+00:00", "Z")
    return parsed, normalized


def _time_interval(value: Any, mention_id: str) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"time for {mention_id} must be an object or null")
    for field in ("kind", "precision", "raw_value", "timezone_basis"):
        item = value.get(field)
        if item is not None and not isinstance(item, str):
            raise ValueError(f"{mention_id}.time.{field} must be a string or null")
    earliest = value.get("earliest_utc")
    latest = value.get("latest_utc")
    start = start_text = None
    end = end_text = None
    if earliest is not None:
        start, start_text = _utc(earliest, f"{mention_id}.time.earliest_utc")
    if latest is not None:
        end, end_text = _utc(latest, f"{mention_id}.time.latest_utc")
    if start is not None and end is not None and start > end:
        raise ValueError(f"time interval for {mention_id} must start at or before its end")
    anchors = value.get("anchor_evidence_ids") or []
    if not isinstance(anchors, list) or any(not isinstance(item, str) or not item for item in anchors):
        raise ValueError(f"time anchors for {mention_id} must be non-empty evidence IDs")
    return {
        "kind": str(value.get("kind") or "claimed_event_time"),
        "earliest_utc": start_text,
        "latest_utc": end_text,
        "precision": str(value.get("precision") or "unknown"),
        "anchor_evidence_ids": sorted(set(anchors)),
        "timezone_basis": value.get("timezone_basis"),
        "raw_value": value.get("raw_value"),
        "status": "bounded" if start is not None and end is not None else "partially_bounded" if start is not None or end is not None else "unresolved",
    }


def _signature(event: dict) -> tuple:
    return (
        event["event_kind"],
        _norm(event["predicate"]),
        tuple(sorted(event["actor_entity_ids"])),
        tuple(sorted(event["object_entity_ids"])),
        event["place_id"],
    )


def _candidate_id(left: str, right: str) -> str:
    material = "\x1f".join(sorted((left, right)))
    return "event-link:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _timeline_bounds(event: dict) -> tuple[datetime, datetime, str] | None:
    interval = event["event_time"]
    kind = "event_time"
    if event["event_kind"] == "communication" and event["publication_time_utc"]:
        point, _ = _utc(event["publication_time_utc"], f"{event['event_id']}.publication_time_utc")
        return point, point, "publication_time"
    if interval is None or interval["earliest_utc"] is None or interval["latest_utc"] is None:
        return None
    start, _ = _utc(interval["earliest_utc"], f"{event['event_id']}.time.earliest_utc")
    end, _ = _utc(interval["latest_utc"], f"{event['event_id']}.time.latest_utc")
    return start, end, kind


def _relation(left: tuple[datetime, datetime, str], right: tuple[datetime, datetime, str]) -> str:
    if left[1] < right[0]:
        return "before"
    if right[1] < left[0]:
        return "after"
    return "overlaps"


def reconstruct_timeline(event_mentions: list[dict], *, run_context: dict | None = None) -> dict:
    """Create separate event candidates and two partial-order timelines.

    A ``before`` edge is descriptive temporal order only. Event candidates are
    never collapsed by this function; exact attribute agreement only produces a
    reviewable ``possible_same`` link. Communication publication time is used
    for the communication timeline and never substituted for physical event
    time.
    """
    if not isinstance(event_mentions, list):
        raise ValueError("event_mentions must be a list")
    if run_context is not None and not isinstance(run_context, dict):
        raise ValueError("run_context must be an object")
    run_context = dict(run_context or {})
    for field in ("incident_id", "run_id", "git_commit", "parameters_sha256"):
        if run_context.get(field) is not None and not isinstance(run_context[field], str):
            raise ValueError(f"run_context.{field} must be a string or null")
    as_of_utc = run_context.get("as_of_utc")
    if as_of_utc is not None:
        as_of_utc = _utc(as_of_utc, "run_context.as_of_utc")[1]
    events = []
    seen_ids = set()
    for raw in event_mentions:
        if not isinstance(raw, dict):
            raise ValueError("each event mention must be an object")
        mention_id_value = raw.get("event_mention_id")
        if not isinstance(mention_id_value, str):
            raise ValueError("event_mention_id must be a non-empty string")
        mention_id = mention_id_value.strip()
        if not mention_id or mention_id in seen_ids:
            raise ValueError("each event mention requires a unique event_mention_id")
        seen_ids.add(mention_id)
        kind = raw.get("event_kind")
        if kind not in EVENT_KINDS:
            raise ValueError(f"unsupported event_kind for {mention_id}: {kind}")
        predicate_value = raw.get("predicate")
        if not isinstance(predicate_value, str):
            raise ValueError(f"predicate must be a non-empty string for {mention_id}")
        predicate = predicate_value.strip()
        if not predicate:
            raise ValueError(f"predicate is required for {mention_id}")
        evidence_ids = _string_list(raw.get("evidence_ids"), f"{mention_id}.evidence_ids", required=True)
        if not evidence_ids:
            raise ValueError(f"at least one evidence_id is required for {mention_id}")
        source_refs = _string_list(raw.get("source_refs"), f"{mention_id}.source_refs")
        actors = _string_list(raw.get("actor_entity_ids"), f"{mention_id}.actor_entity_ids")
        objects = _string_list(raw.get("object_entity_ids"), f"{mention_id}.object_entity_ids")
        place_id = raw.get("place_id")
        if place_id is not None and (not isinstance(place_id, str) or not place_id.strip()):
            raise ValueError(f"{mention_id}.place_id must be a non-empty string or null")
        place_id = place_id.strip() if isinstance(place_id, str) else None
        publication = raw.get("publication_time_utc")
        if publication is not None and not isinstance(publication, str):
            raise ValueError(f"{mention_id}.publication_time_utc must be a string or null")
        publication_text = _utc(publication, f"{mention_id}.publication_time_utc")[1] if publication else None
        event_time = _time_interval(raw.get("time"), mention_id)
        cannot_link = _string_list(raw.get("cannot_link_event_mention_ids"), f"{mention_id}.cannot_link_event_mention_ids")
        attributed_relations = raw.get("attributed_relations") or []
        if not isinstance(attributed_relations, list) or any(not isinstance(item, dict) for item in attributed_relations):
            raise ValueError(f"{mention_id}.attributed_relations must be a list of objects")
        event = {
            "schema_version": "1.0",
            "event_id": f"event:{mention_id}",
            "incident_id": run_context.get("incident_id"),
            "run_id": run_context.get("run_id"),
            "producer": "social.analysis.event_reconstruction@1.0",
            "event_mention_id": mention_id,
            "event_kind": kind,
            "predicate": predicate,
            "actor_entity_ids": actors,
            "object_entity_ids": objects,
            "place_id": place_id,
            "event_time": event_time,
            "publication_time_utc": publication_text,
            "timeline_time_kind": None,
            "evidence_ids": sorted(set(evidence_ids)),
            "source_refs": source_refs,
            "cannot_link_event_mention_ids": cannot_link,
            "attributed_relations": attributed_relations,
            "merge_status": "unresolved_event_mention",
            "limitations": [
                "An event mention records what a source reports; it does not by itself establish that the real-world event occurred.",
                "Publication time is not substituted for physical event time.",
                "Temporal order does not establish causation.",
            ],
        }
        bounds = _timeline_bounds(event)
        if bounds:
            event["timeline_time_kind"] = bounds[2]
        events.append(event)

    by_mention = {event["event_mention_id"]: event for event in events}
    unresolved_constraints = []
    for event in events:
        for target_id in event["cannot_link_event_mention_ids"]:
            if target_id not in by_mention:
                unresolved_constraints.append({
                    "event_mention_id": event["event_mention_id"],
                    "target_event_mention_id": target_id,
                    "relation": "cannot_link",
                    "status": "target_not_in_input",
                })
    candidate_links = []
    for left, right in combinations(events, 2):
        if _signature(left) != _signature(right):
            continue
        if not left["actor_entity_ids"] or not left["place_id"]:
            continue
        if right["event_mention_id"] in left["cannot_link_event_mention_ids"] or left["event_mention_id"] in right["cannot_link_event_mention_ids"]:
            continue
        left_bounds = _timeline_bounds(left)
        right_bounds = _timeline_bounds(right)
        if (left_bounds and right_bounds and left_bounds[2] == right_bounds[2]
                and _relation(left_bounds, right_bounds) != "overlaps"):
            continue
        time_basis = "incomparable_or_missing_time"
        if left_bounds and right_bounds and left_bounds[2] == right_bounds[2]:
            time_basis = "overlapping_time_intervals"
        ids = sorted((left["event_id"], right["event_id"]))
        candidate_links.append({
            "link_id": _candidate_id(*ids),
            "event_ids": ids,
            "status": "possible_same",
            "basis": ["exact_predicate", "exact_actor_ids", "exact_object_ids", "exact_place_id", time_basis],
            "limitations": ["Candidate link only; events are not merged.", "Repeated actions may share the same attributes."],
        })

    timelines = {"physical": [], "communication": []}
    for event in events:
        bounds = _timeline_bounds(event)
        if not bounds:
            continue
        start, end, time_kind = bounds
        timelines[event["event_kind"]].append({
            "event_id": event["event_id"],
            "event_mention_id": event["event_mention_id"],
            "time_kind": time_kind,
            "earliest_utc": start.isoformat().replace("+00:00", "Z"),
            "latest_utc": end.isoformat().replace("+00:00", "Z"),
            "evidence_ids": event["evidence_ids"],
        })
    for rows in timelines.values():
        rows.sort(key=lambda row: (
            _utc(row["earliest_utc"], "timeline.earliest_utc")[0],
            _utc(row["latest_utc"], "timeline.latest_utc")[0],
            row["event_id"],
        ))

    temporal_relations = []
    for kind, rows in timelines.items():
        indexed = [(by_mention[row["event_mention_id"]], _timeline_bounds(by_mention[row["event_mention_id"]])) for row in rows]
        for (left_event, left_bounds), (right_event, right_bounds) in combinations(indexed, 2):
            same_time_basis = left_bounds[2] == right_bounds[2]
            relation = _relation(left_bounds, right_bounds) if same_time_basis else "unknown"
            temporal_relations.append({
                "event_ids": [left_event["event_id"], right_event["event_id"]],
                "relation": relation,
                "causal": False,
                "basis": "incomparable_time_bases" if not same_time_basis else "non_overlapping_time_intervals" if relation in {"before", "after"} else "overlapping_time_intervals",
                "time_kind": left_bounds[2] if same_time_basis else "mixed",
            })
    temporal_relations.sort(key=lambda row: (row["event_ids"], row["relation"]))

    return {
        "schema_version": "1.0",
        "as_of_utc": as_of_utc,
        "provenance": {
            "producer": "social.analysis.event_reconstruction@1.0",
            "run_id": run_context.get("run_id"),
            "incident_id": run_context.get("incident_id"),
            "git_commit": run_context.get("git_commit"),
            "parameters_sha256": run_context.get("parameters_sha256"),
        },
        "events": events,
        "candidate_links": sorted(candidate_links, key=lambda row: row["link_id"]),
        "timelines": timelines,
        "temporal_relations": temporal_relations,
        "unresolved_constraints": sorted(unresolved_constraints, key=lambda row: (row["event_mention_id"], row["target_event_mention_id"])),
        "unresolved": [
            {"event_id": event["event_id"], "reason": "event mention is preserved without real-world verification or merge"}
            for event in events
        ],
        "limitations": [
            "This deterministic module organizes supplied event mentions; it does not extract events from raw text.",
            "Exact entity identifiers, not names or fuzzy similarity, are required for candidate matching.",
            "No causal relation is inferred from temporal order.",
        ],
    }


def resolve_entity_alias(alias: str, entities: list[dict]) -> dict:
    """Resolve an alias only on exact normalized equality; preserve ambiguity."""
    if not isinstance(alias, str):
        return {"status": "unresolved", "entity_ids": [], "basis": "alias_not_string"}
    if not isinstance(entities, list):
        raise ValueError("entities must be a list")
    query = _norm(alias)
    if not query:
        return {"status": "unresolved", "entity_ids": [], "basis": "empty_alias"}
    matches = []
    for entity in entities:
        if not isinstance(entity, dict):
            raise ValueError("each entity must be an object")
        entity_id = entity.get("entity_id")
        if not isinstance(entity_id, str):
            continue
        entity_id = entity_id.strip()
        if not entity_id:
            continue
        aliases = entity.get("aliases") or []
        if not isinstance(aliases, list):
            raise ValueError(f"aliases for {entity_id} must be a list")
        if any(isinstance(candidate, str) and query == _norm(candidate) for candidate in aliases):
            matches.append(entity_id)
    matches = sorted(set(matches))
    if len(matches) == 1:
        return {"status": "resolved_exact_alias", "entity_ids": matches, "basis": "exact_normalized_alias"}
    if matches:
        return {"status": "ambiguous", "entity_ids": matches, "basis": "alias_matches_multiple_entities"}
    return {"status": "unresolved", "entity_ids": [], "basis": "no_exact_alias_match"}
