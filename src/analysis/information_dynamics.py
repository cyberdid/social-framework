"""Descriptive, coverage-aware information-wave summaries.

This module separates publication time from collection time, preserves reported
physical-event time as an attributed interval, and treats source dependencies as
dependency evidence rather than independent corroboration. Its outputs do not
infer causality, automation, state direction, physical escalation, or audience
size.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
import json
from statistics import median
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_CONFIRMED_DEPENDENCY = {"confirmed_by_explicit_reference", "identical_asset_bytes"}
_CANDIDATE_DEPENDENCY = {"candidate"}
_GROUP_SCENARIOS = (
    "all_labels_as_recorded",
    "automatic_candidates_unknown",
    "manual_and_evidence_supported_only",
)


def _parse_time(value: object, field: str, *, required: bool = False) -> datetime | None:
    if value is None or value == "":
        if required:
            raise ValueError(f"{field} is required")
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _channel_ref(row: dict) -> str:
    return f"{row['platform']}:{row['channel_id']}"


def _post_snapshot(rows: Iterable[dict]) -> tuple[list[dict], dict]:
    latest: dict[tuple[str, str], dict] = {}
    view_latest: dict[tuple[str, str], tuple[datetime, int, bool]] = {}
    duplicate_records = 0
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            raise ValueError(f"post row {index + 1} must be an object")
        post_id = str(raw.get("post_id") or "").strip()
        platform = str(raw.get("platform") or "").strip().casefold()
        channel_id = str(raw.get("channel_id") or "").strip()
        if not post_id or not platform or not channel_id:
            raise ValueError(f"post row {index + 1} requires post_id, platform, and channel_id")
        publication = _parse_time(raw.get("publication_time"), "publication_time", required=True)
        observed = _parse_time(raw.get("observed_at"), "observed_at")
        post_key = (platform, post_id)
        candidate = {**raw, "post_id": post_id, "platform": platform, "channel_id": channel_id,
                     "_publication": publication, "_observed": observed,
                     "_snapshot_post_id": f"{platform}:{post_id}"}
        previous = latest.get(post_key)
        if previous:
            duplicate_records += 1
            if (previous["platform"], previous["channel_id"], previous["_publication"]) != (
                    platform, channel_id, publication):
                    raise ValueError(f"conflicting identity/publication fields for platform-qualified post_id {post_key}")
            previous_observed = previous["_observed"]
            if (observed is not None and (previous_observed is None or observed > previous_observed)):
                latest[post_key] = candidate
            elif observed == previous_observed:
                previous_key = json.dumps(previous, ensure_ascii=False, sort_keys=True, default=str)
                candidate_key = json.dumps(candidate, ensure_ascii=False, sort_keys=True, default=str)
                if candidate_key > previous_key:
                    latest[post_key] = candidate
        else:
            latest[post_key] = candidate

        view_value = raw.get("view_count_snapshot")
        if type(view_value) is int and view_value >= 0:
            view_time = _parse_time(raw.get("view_count_observed_at"), "view_count_observed_at") or observed
            if view_time is None:
                view_time = datetime.min.replace(tzinfo=UTC)
            old_view = view_latest.get(post_key)
            if old_view is None or view_time > old_view[0]:
                view_latest[post_key] = (view_time, view_value, False)
            elif view_time == old_view[0] and view_value != old_view[1]:
                view_latest[post_key] = (view_time, old_view[1], True)

    result = []
    for post_key, row in sorted(latest.items()):
        normalized = dict(row)
        snapshot = view_latest.get(post_key)
        normalized["_view_count"] = snapshot[1] if snapshot is not None and not snapshot[2] else None
        result.append(normalized)
    return result, {"input_record_count": len(latest) + duplicate_records,
                    "unique_post_count": len(latest), "duplicate_snapshot_record_count": duplicate_records,
                    "view_counter_post_count": sum(not value[2] for value in view_latest.values()),
                    "view_snapshot_conflict_count": sum(value[2] for value in view_latest.values()),
                    "observation_timestamp_missing_count": sum(row["_observed"] is None for row in latest.values())}


def _time_series(posts: list[dict], field: str, *, bin_hours: int, timezone_name: str) -> list[dict]:
    timezone = ZoneInfo(timezone_name)
    bins: dict[tuple[str, datetime], list[dict]] = defaultdict(list)
    for row in posts:
        timestamp = row.get("_publication") if field == "publication_time" else row.get("_observed")
        if timestamp is None:
            continue
        local = timestamp.astimezone(timezone)
        start_hour = (local.hour // bin_hours) * bin_hours
        local_start = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
        start_utc = local_start.astimezone(UTC)
        bins[(row["platform"], start_utc)].append(row)
    result = []
    for (platform, start), records in sorted(bins.items(), key=lambda item: (item[0][1], item[0][0])):
        end = start + timedelta(hours=bin_hours)
        result.append({"platform": platform, "bin_start_utc": _iso(start), "bin_end_utc": _iso(end),
                       "post_count": len(records), "channel_count": len({_channel_ref(row) for row in records}),
                       "post_ids": sorted(row["post_id"] for row in records)})
    return result


def _coverage_summary(coverage: Iterable[dict] | dict | None, observed_platforms: set[str]) -> dict:
    by_platform: dict[str, dict] = {}
    if isinstance(coverage, dict):
        coverage_rows = [{**value, "platform": value.get("platform", key)}
                         for key, value in coverage.items() if isinstance(value, dict)]
    else:
        coverage_rows = list(coverage or [])
    for row in coverage_rows:
        if not isinstance(row, dict):
            continue
        platform = str(row.get("platform") or "").strip().casefold()
        if not platform:
            continue
        by_platform[platform] = {"status": str(row.get("status") or "unspecified"),
                                 "reason": row.get("reason"),
                                 "queried_at": row.get("queried_at")}
    for platform in observed_platforms:
        by_platform.setdefault(platform, {"status": "observed_coverage_unspecified", "reason": None,
                                          "queried_at": None})
    return {"platforms": dict(sorted(by_platform.items())),
            "unobserved_platforms": sorted(platform for platform in by_platform
                                            if platform not in observed_platforms),
            "limitations": ["does_not_establish_absence"]}


def _group_sensitivity(posts: list[dict]) -> list[dict]:
    output = []
    for scenario in _GROUP_SCENARIOS:
        groups: dict[str, dict] = defaultdict(lambda: {"post_count": 0, "channel_ids": set(),
                                                        "snapshot_view_counter_sum": 0,
                                                        "view_counter_post_count": 0})
        included = excluded = 0
        for row in posts:
            label = str(row.get("group_label") or "unknown")
            status = str(row.get("group_label_status") or "unknown").casefold()
            if scenario == "manual_and_evidence_supported_only" and status not in {
                    "manual", "evidence_supported", "evidence_verified"}:
                excluded += 1
                continue
            included += 1
            if scenario == "automatic_candidates_unknown" and status in {
                    "automatic_candidate", "auto_candidate", "automatic", "auto", "candidate"}:
                label = "unknown"
            item = groups[label]
            item["post_count"] += 1
            item["channel_ids"].add(_channel_ref(row))
            if row["_view_count"] is not None:
                item["snapshot_view_counter_sum"] += row["_view_count"]
                item["view_counter_post_count"] += 1
        group_rows = {}
        view_total = sum(item["snapshot_view_counter_sum"] for item in groups.values())
        for label, item in sorted(groups.items()):
            group_rows[label] = {**item, "channel_count": len(item["channel_ids"]),
                                 "post_share_of_included": item["post_count"] / included if included else None,
                                 "snapshot_view_counter_share_of_included": (
                                     item["snapshot_view_counter_sum"] / view_total if view_total else None)}
            del group_rows[label]["channel_ids"]
        output.append({"scenario_id": scenario, "included_post_count": included,
                       "excluded_post_count": excluded, "groups": group_rows,
                       "label_semantics": "descriptive_candidate_group_only",
                       "view_metric": "snapshot_view_counter_sum_not_unique_audience"})
    return output


def _top_k(posts: list[dict], top_k: tuple[int, ...]) -> list[dict]:
    channels: dict[str, dict] = defaultdict(lambda: {"post_count": 0, "view_count_sum": 0})
    for row in posts:
        item = channels[_channel_ref(row)]
        item["post_count"] += 1
        if row["_view_count"] is not None:
            item["view_count_sum"] += row["_view_count"]
    output = []
    for metric, field in (("post_count", "post_count"), ("snapshot_view_counter_sum", "view_count_sum")):
        ranked = sorted(((channel, values[field]) for channel, values in channels.items()),
                        key=lambda item: (-item[1], item[0]))
        total = sum(value for _, value in ranked)
        for k in top_k:
            selected = ranked[:k]
            top_value = sum(value for _, value in selected)
            output.append({"metric": metric, "k": k, "total": total, "top_value": top_value,
                           "top_channels": [channel for channel, _ in selected],
                           "share": top_value / total if total else None,
                           "unique_audience": False if metric == "snapshot_view_counter_sum" else None})
    return output


def _lineage_summary(posts: list[dict], source_lineage: dict | None) -> dict:
    evidence_to_post = {}
    for row in posts:
        evidence_id = str(row.get("evidence_id") or row["_snapshot_post_id"])
        if evidence_id in evidence_to_post:
            raise ValueError(f"duplicate evidence_id in post snapshot: {evidence_id}")
        evidence_to_post[evidence_id] = row
    parent = {evidence_id: evidence_id for evidence_id in evidence_to_post}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)

    confirmed_edges = candidate_edges = unmatched_edges = 0
    for edge in (source_lineage or {}).get("edges", []):
        if not isinstance(edge, dict):
            continue
        ids = edge.get("evidence_ids") or []
        if isinstance(ids, str):
            ids = [ids]
        known = [str(evidence_id) for evidence_id in ids if str(evidence_id) in parent]
        certainty = edge.get("certainty")
        if certainty in _CONFIRMED_DEPENDENCY and len(known) > 1:
            confirmed_edges += 1
            for evidence_id in known[1:]:
                union(known[0], evidence_id)
        elif certainty in _CANDIDATE_DEPENDENCY and len(known) > 1:
            candidate_edges += 1
        elif certainty in _CONFIRMED_DEPENDENCY or certainty in _CANDIDATE_DEPENDENCY:
            unmatched_edges += 1
    components: dict[str, list[str]] = defaultdict(list)
    for evidence_id in parent:
        components[find(evidence_id)].append(evidence_id)
    families = []
    linked = set()
    for evidence_ids in components.values():
        if len(evidence_ids) < 2:
            continue
        linked.update(evidence_ids)
        family_posts = [evidence_to_post[evidence_id] for evidence_id in evidence_ids]
        times = [row["_publication"] for row in family_posts]
        families.append({"evidence_ids": sorted(evidence_ids),
                         "post_ids": sorted(row["_snapshot_post_id"] for row in family_posts),
                         "post_count": len(family_posts),
                         "platform_count": len({row["platform"] for row in family_posts}),
                         "first_publication_time": _iso(min(times)),
                         "last_publication_time": _iso(max(times))})
    families.sort(key=lambda family: family["evidence_ids"])
    return {"confirmed_dependency_edge_count": confirmed_edges,
            "candidate_dependency_edge_count": candidate_edges,
            "unmatched_dependency_edge_count": unmatched_edges,
            "confirmed_dependency_family_count": len(families),
            "confirmed_dependency_families": families,
            "unlinked_post_count": len(posts) - len(linked),
            "independent_roots_lower_bound": 0,
            "limitations": ["confirmed dependency families are not source-of-origin findings",
                            "unlinked records are not assumed independent",
                            "no dependency edge does not establish independent corroboration"]}


def summarize_information_dynamics(posts: Iterable[dict], *, coverage: Iterable[dict] | dict | None = None,
                                   source_lineage: dict | None = None, bin_hours: int = 6,
                                   timezone_name: str = "Europe/Kyiv",
                                   top_k: tuple[int, ...] = (1, 3, 5, 10)) -> dict:
    """Build deterministic summaries from normalized post snapshots.

    Required post fields are ``post_id``, ``platform``, ``channel_id``,
    timezone-aware ``publication_time``; ``observed_at`` is optional. Duplicate
    post snapshots collapse to the latest observed record, while view counters
    use their latest explicitly timed snapshot.
    """
    if type(bin_hours) is not int or not 1 <= bin_hours <= 24 or 24 % bin_hours:
        raise ValueError("bin_hours must be a positive divisor of 24")
    if not top_k or any(type(k) is not int or k < 1 for k in top_k):
        raise ValueError("top_k must contain positive integers")
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown timezone: {timezone_name}") from exc
    normalized, corpus = _post_snapshot(posts)
    observed_platforms = {row["platform"] for row in normalized}
    event_refs = []
    for row in normalized:
        start = _parse_time(row.get("reported_event_time_start"), "reported_event_time_start")
        end = _parse_time(row.get("reported_event_time_end"), "reported_event_time_end")
        if start is None and end is None:
            continue
        if start is not None and end is not None and end < start:
            raise ValueError(f"reported event-time interval is reversed for post_id {row['post_id']}")
        event_refs.append({"post_id": row["post_id"], "platform": row["platform"],
                           "post_ref": row["_snapshot_post_id"], "event_time_start": _iso(start),
                           "event_time_end": _iso(end), "publication_time": _iso(row["_publication"]),
                           "observed_at": _iso(row["_observed"]),
                           "status": "reported_time_reference",
                           "interpretation": "source_attributed_time_not_independently_verified"})
    event_refs.sort(key=lambda item: (item["event_time_start"] or "", item["post_ref"]))
    lags = [(row["_observed"] - row["_publication"]).total_seconds()
            for row in normalized if row["_observed"] is not None]
    nonnegative_lags = [value for value in lags if value >= 0]

    platform_summary = {}
    for platform in sorted(observed_platforms):
        rows = [row for row in normalized if row["platform"] == platform]
        platform_summary[platform] = {"unique_post_count": len(rows),
                                      "channel_count": len({_channel_ref(row) for row in rows}),
                                      "view_counter_post_count": sum(row["_view_count"] is not None for row in rows),
                                      "snapshot_view_counter_sum": sum(row["_view_count"] or 0 for row in rows)}

    return {
        "schema_version": "1.0",
        "corpus": corpus,
        "temporal_scope": {"publication_time_field": "publication_time",
                            "observation_time_field": "observed_at",
                            "reported_event_time_fields": ["reported_event_time_start", "reported_event_time_end"],
                            "timezone": timezone_name, "bin_hours": bin_hours,
                            "binning_basis": "local_wall_clock_start_then_fixed_utc_duration",
                            "publication_time_is_not_event_time": True},
        "platform_summary": platform_summary,
        "publication_to_observation_lag_seconds": {
            "observed_post_count": len(lags),
            "negative_lag_count": sum(value < 0 for value in lags),
            "min": min(nonnegative_lags) if nonnegative_lags else None,
            "median": median(nonnegative_lags) if nonnegative_lags else None,
            "max": max(nonnegative_lags) if nonnegative_lags else None,
            "interpretation": "observed_collection_delay_not_platform_latency",
        },
        "publication_series": _time_series(normalized, "publication_time", bin_hours=bin_hours,
                                            timezone_name=timezone_name),
        "observation_series": _time_series(normalized, "observed_at", bin_hours=bin_hours,
                                            timezone_name=timezone_name),
        "reported_event_time_references": event_refs,
        "coverage": _coverage_summary(coverage, observed_platforms),
        "source_lineage": _lineage_summary(normalized, source_lineage),
        "group_sensitivity": _group_sensitivity(normalized),
        "top_k_concentration": _top_k(normalized, tuple(sorted(set(top_k)))),
        "limitations": ["descriptive_information_activity_is_not_physical_conflict_escalation",
                        "snapshot_view_counters_are_not_unique_audience_or_verified_reach",
                        "does_not_establish_causality",
                        "does_not_classify_bots_or_automation",
                        "does_not_establish_state_direction_or_operator_identity",
                        "targeted_nonprobability_corpus_does_not_support_platform_population_inference"],
    }
