"""Descriptive, frame-conditional aggregation for target-linked comment labels."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from src.analysis.audience_response_contracts import ContractValidationError, comment_reaction_state


_STATES = ("not_collected", "not_reported", "empty", "available", "current_user_excluded")
_SENSITIVITY_KEYS = {
    "deduplicate_exact", "cap_author_contribution", "exclude_candidate_coordination"
}
_LIMITATIONS = [
    "Estimates describe only the observed, selected comment frame; they are not a population estimate.",
    "Reaction counts are platform feedback, not votes for every claim in a comment; exposure is unavailable.",
    "Only the latest available reaction snapshot per comment is used; one snapshot cannot establish reaction history.",
    "No population confidence intervals are emitted because the comment frame is not probability sampled.",
    "Interpretation accuracy is not established by aggregation; model labels retain their provenance and abstentions.",
    "Largest reaction counts rank only within each platform among comments whose latest selected reaction snapshot is available; they are not exposure-adjusted engagement or population support.",
    "Telegram Star currency payments are decoupled from non-paid emoji reaction counts; currency amounts do not count toward emoji reaction rankings.",
]


def _id(value: Any, field: str, *, allow_none: bool = False) -> str | None:
    if allow_none and value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        value = str(value)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string or positive integer")
    return value.strip()


def _utc(value: Any, field: str, *, allow_none: bool = False) -> datetime | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z") if value is not None else None


def _normalize_comment(raw: Mapping[str, Any]) -> dict[str, Any]:
    comment_id = _id(raw.get("comment_id"), "comment_id")
    platform = str(raw.get("platform") or ("telegram" if raw.get("discussion_chat") or str(raw.get("post_url", "")).startswith("https://t.me/") else "unknown"))
    post_ref = raw.get("source_post_id", raw.get("post_url"))
    thread_ref = raw.get("thread_id", post_ref)
    channel = raw.get("channel_id", raw.get("channel"))
    if channel is None and isinstance(post_ref, str) and post_ref.startswith("https://t.me/"):
        channel = post_ref.rstrip("/").split("/")[-2]
    return {
        "comment_id": comment_id,
        "platform": platform,
        "channel": _id(channel, "channel", allow_none=True) or "unknown",
        "thread_id": _id(thread_ref, "thread_id"),
        "author": _id(raw.get("author_pseudonym", raw.get("sender")), "author_pseudonym", allow_none=True),
        "published": _utc(raw.get("published_at_utc", raw.get("date")), "published_at_utc", allow_none=True),
        "text": raw.get("text") if isinstance(raw.get("text"), str) else "",
        "candidate_coordination": raw.get("is_candidate_coordination") is True,
        "source": raw,
    }


def _period_start(value: datetime | None, width_seconds: int | None) -> str | None:
    if value is None or width_seconds is None:
        return None
    epoch = int(value.timestamp())
    start = datetime.fromtimestamp((epoch // width_seconds) * width_seconds, tz=timezone.utc)
    return _iso(start)


def _dedup_key(comment: Mapping[str, Any]) -> tuple[str, str, str] | None:
    normalized = re.sub(r"\s+", " ", comment["text"].casefold()).strip()
    if len(normalized) < 20:
        return None
    return comment["platform"], comment["thread_id"], normalized


def _reaction_view(comment: Mapping[str, Any], snapshots_by_comment: Mapping[str, list[Mapping[str, Any]]]) -> dict[str, Any]:
    comment_id = comment["comment_id"]
    candidates = []
    request_status_counts = {
        "ok": 0, "request_failed": 0, "no_update": 0, "wrong_peer_update": 0,
    }
    for item in snapshots_by_comment.get(comment_id, []):
        request_status = item.get("request_status")
        if request_status is not None:
            if request_status not in request_status_counts:
                raise ValueError("reaction request_status is unsupported")
            request_status_counts[request_status] += 1
        else:
            request_status = "not_requested"
        if request_status in {"request_failed", "no_update", "wrong_peer_update"}:
            if item.get("reaction_state") is not None or item.get("reaction_breakdown") is not None:
                raise ContractValidationError("non-observation reaction request cannot carry observed state")
            continue
        candidates.append(item)
    source = comment["source"]
    if "reaction_state" in source or "reaction_breakdown" in source:
        candidates.append({
            "comment_id": comment_id,
            "reaction_state": source.get("reaction_state"),
            "reaction_breakdown": source.get("reaction_breakdown"),
            "reaction_observed_at": source.get("reaction_observed_at", source.get("fetched_at")),
            "snapshot_id": "comment-record",
        })
    if not candidates:
        return {"reaction_state": "not_collected", "breakdown": None, "observed": None,
                "request_status_counts": request_status_counts}
    ranked = []
    for item in candidates:
        state = comment_reaction_state(item)
        observed_raw = item.get("observed_at_utc", item.get("reaction_observed_at"))
        observed = _utc(observed_raw, "reaction_observed_at", allow_none=True)
        ranked.append((observed is not None, observed or datetime.min.replace(tzinfo=timezone.utc), str(item.get("snapshot_id", "")), state, item.get("reaction_breakdown"), observed))
    _, _, _, state, breakdown, observed = max(ranked, key=lambda value: value[:3])
    return {"reaction_state": state, "breakdown": breakdown, "observed": observed,
            "request_status_counts": request_status_counts}


def separate_reaction_breakdown(breakdown: list[Mapping[str, Any]]) -> dict[str, int]:
    """Separate reaction breakdown into non_paid_reaction_count, paid_star_amount, and unknown_reaction_count.

    Decouples Telegram Star currency payments from non-paid emoji reaction counts so currency
    payments do not mix into or distort emoji reaction counts.
    """
    if not isinstance(breakdown, list) or not breakdown:
        raise ContractValidationError("available reaction snapshots require an observed vector")
    non_paid = 0
    paid = 0
    unknown = 0
    for item in breakdown:
        count = item.get("count") if isinstance(item, Mapping) else None
        if type(count) is not int or count < 0:
            raise ContractValidationError("available reaction counts must be non-negative integers")
        kind = item.get("kind") or item.get("reaction_type") if isinstance(item, Mapping) else None
        if kind == "paid":
            paid += count
        elif kind == "unknown":
            unknown += count
        else:
            non_paid += count
    return {
        "non_paid_reaction_count": non_paid,
        "paid_star_amount": paid,
        "unknown_reaction_count": unknown,
    }


def largest_available_reaction_comments(
    comments: Iterable[Mapping[str, Any]],
    top_k: int = 10,
    snapshots_by_comment: Mapping[str, list[Mapping[str, Any]]] | None = None,
    *,
    include_paid: bool = False,
) -> list[dict[str, Any]]:
    """Rank comments by non-paid emoji reaction counts, decoupling paid Star currency.

    Ranks within each platform among comments whose latest selected reaction snapshot
    is 'available'. Comments with paid Telegram Stars or unknown reaction types have
    those currency/unknown units segregated and do not distort non-paid emoji reaction rankings.
    """
    if type(top_k) is not int or top_k <= 0:
        raise ValueError("top_k must be a positive integer")
    if snapshots_by_comment is None:
        snapshots_by_comment = {}

    available_by_platform: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for raw in comments:
        comment = raw if "source" in raw else _normalize_comment(raw)
        comment_id = comment["comment_id"]
        reaction = _reaction_view(comment, snapshots_by_comment)
        if reaction["reaction_state"] != "available":
            continue
        breakdown = reaction["breakdown"]
        counts = separate_reaction_breakdown(breakdown)
        available_by_platform[comment["platform"]].append((
            counts["non_paid_reaction_count"],
            counts["paid_star_amount"],
            comment_id,
        ))

    ranking_rows = []
    for platform in sorted(available_by_platform):
        platform_rows = sorted(
            available_by_platform[platform],
            key=lambda row: (-row[0], row[2]),
        )
        denominator = len(platform_rows)
        for rank, (non_paid_count, paid_stars, _comment_id) in enumerate(platform_rows[:top_k], 1):
            row_dict: dict[str, Any] = {
                "platform": platform,
                "rank": rank,
                "unit": "comment",
                "reaction_count": non_paid_count,
                "denominator": denominator,
                "denominator_basis": "comments whose latest selected reaction snapshot is available in this platform frame; latest current_user_excluded snapshots are excluded",
            }
            if include_paid:
                row_dict["paid_star_amount"] = paid_stars
            ranking_rows.append(row_dict)
    return ranking_rows


def _report_views(
    groups: list[dict[str, Any]],
    rows: list[dict[str, str]],
    retained_by_id: Mapping[str, Mapping[str, Any]],
    snapshots_by_comment: Mapping[str, list[Mapping[str, Any]]],
    *,
    unit: str,
) -> dict[str, list[dict[str, Any]]]:
    stance_denominators = {
        "comment": "comments with at least one stance output for this target",
        "author": "authors with at least one stance output for this target",
        "thread": "threads with at least one stance output for this target",
        "channel": "channels with at least one stance output for this target",
    }
    stance_by_key: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    for group in groups:
        if group["dimension"] != "stance":
            continue
        key = (group["platform"], group["period_start_utc"], group["target_id"])
        view = stance_by_key.setdefault(key, {
            "platform": group["platform"],
            "period_start_utc": group["period_start_utc"],
            "target_id": group["target_id"],
            "unit": unit,
            "denominator": group["denominator"],
            "denominator_basis": stance_denominators[unit],
            "stance_counts": {},
            "labels_may_overlap": True,
        })
        if group["denominator"] != view["denominator"]:
            raise ValueError("stance labels for one target must share the same unit denominator")
        view["stance_counts"][group["label"]] = group["numerator"]
    stance_rows = []
    for key in sorted(stance_by_key, key=lambda item: (item[0], item[1] or "", item[2])):
        view = stance_by_key[key]
        view["stance_counts"] = dict(sorted(view["stance_counts"].items()))
        stance_rows.append(view)

    labels_by_comment_target: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"topic": set(), "emotion": set()}
    )
    for row in rows:
        if row["dimension"] in {"topic", "emotion"} and row["label"] != "abstain":
            labels_by_comment_target[(row["comment_id"], row["target_id"])][row["dimension"]].add(row["label"])
    denominator_comments: dict[tuple[str, str], set[str]] = defaultdict(set)
    pair_comments: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)
    for (comment_id, target_id), dimensions in labels_by_comment_target.items():
        topics, emotions = dimensions["topic"], dimensions["emotion"]
        if not topics or not emotions:
            continue
        comment = retained_by_id[comment_id]
        platform = comment["platform"]
        denominator_comments[(platform, target_id)].add(comment_id)
        for topic in topics:
            for emotion in emotions:
                pair_comments[(platform, target_id, topic, emotion)].add(comment_id)
    cooccurrence_rows = []
    for key in sorted(pair_comments):
        platform, target_id, topic, emotion = key
        cooccurrence_rows.append({
            "platform": platform,
            "target_id": target_id,
            "topic_label": topic,
            "emotion_label": emotion,
            "unit": "comment",
            "numerator": len(pair_comments[key]),
            "denominator": len(denominator_comments[(platform, target_id)]),
            "denominator_basis": "comments with at least one non-abstained topic and emotion output for this same target",
            "labels_may_overlap": True,
        })

    ranking_rows = largest_available_reaction_comments(
        retained_by_id.values(),
        top_k=10,
        snapshots_by_comment=snapshots_by_comment,
    )

    return {
        "target_stance_distribution": stance_rows,
        "topic_emotion_cooccurrence": cooccurrence_rows,
        "largest_available_reaction_comments": ranking_rows,
    }


def aggregate_audience(
    comments: Iterable[Mapping[str, Any]],
    interpretations: Iterable[Mapping[str, Any]],
    reaction_snapshots: Iterable[Mapping[str, Any]],
    *,
    unit: str,
    time_bins: int | None,
    sensitivity: Mapping[str, bool],
) -> dict[str, Any]:
    """Aggregate candidate labels within an observed frame, never to population opinion.

    For ``author``/``thread``/``channel`` units, the numerator counts units that
    expressed a label at least once. A unit can contribute to multiple labels if
    it expressed multiple topics/emotions or changed its stated position. The
    denominator is units with at least one interpretation, including abstentions.
    """
    if unit not in {"comment", "author", "thread", "channel"}:
        raise ValueError("unit must be comment, author, thread, or channel")
    if time_bins is not None and (type(time_bins) is not int or time_bins <= 0):
        raise ValueError("time_bins must be a positive integer number of seconds or None")
    if set(sensitivity) != _SENSITIVITY_KEYS or any(type(v) is not bool for v in sensitivity.values()):
        raise ValueError("sensitivity must contain exactly three boolean scenario flags")

    normalized_comments = [_normalize_comment(item) for item in comments]
    interpretations = list(interpretations)
    comment_by_id: dict[str, dict[str, Any]] = {}
    for item in normalized_comments:
        if item["comment_id"] in comment_by_id:
            raise ValueError(f"duplicate comment_id: {item['comment_id']}")
        comment_by_id[item["comment_id"]] = item

    input_n = len(normalized_comments)
    excluded = {"exact_duplicate": 0, "candidate_coordination": 0}
    retained = []
    seen_exact: set[tuple[str, str, str]] = set()
    ordered_comments = sorted(normalized_comments, key=lambda c: (c["published"] or datetime.max.replace(tzinfo=timezone.utc), c["comment_id"]))
    for item in ordered_comments:
        if sensitivity["exclude_candidate_coordination"] and item["candidate_coordination"]:
            excluded["candidate_coordination"] += 1
            continue
        key = _dedup_key(item) if sensitivity["deduplicate_exact"] else None
        if key is not None and key in seen_exact:
            excluded["exact_duplicate"] += 1
            continue
        if key is not None:
            seen_exact.add(key)
        retained.append(item)
    retained_by_id = {item["comment_id"]: item for item in retained}

    snaps: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for snapshot in reaction_snapshots:
        if not isinstance(snapshot, Mapping):
            raise ValueError("reaction snapshots must be objects")
        comment_id = _id(snapshot.get("comment_id"), "snapshot.comment_id")
        if comment_id not in comment_by_id:
            raise ValueError(f"reaction snapshot references unknown comment_id: {comment_id}")
        snaps[comment_id].append(snapshot)

    rows = []
    model_versions = set()
    for raw in interpretations:
        if not isinstance(raw, Mapping):
            raise ValueError("interpretation rows must be objects")
        comment_id = _id(raw.get("comment_id"), "interpretation.comment_id")
        if comment_id not in comment_by_id:
            raise ValueError(f"interpretation references unknown comment_id: {comment_id}")
        if comment_id not in retained_by_id:
            continue
        target_id = _id(raw.get("target_id"), "target_id")
        dimension = _id(raw.get("dimension"), "dimension")
        label = _id(raw.get("label"), "label")
        model_id = _id(raw.get("model_id"), "model_id")
        model_version = _id(raw.get("model_version"), "model_version")
        model_versions.add((model_id, model_version))
        rows.append({"comment_id": comment_id, "target_id": target_id, "dimension": dimension,
                     "label": label, "model_id": model_id, "model_version": model_version})
    if len(model_versions) > 1:
        raise ValueError("select one model_id/model_version before aggregation; model outputs are not pooled")
    if len({(r["comment_id"], r["target_id"], r["dimension"], r["label"]) for r in rows}) != len(rows):
        raise ValueError("duplicate comment-target-dimension-label interpretation rows")

    grouped: dict[tuple[str, str | None, str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    comment_group: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for row in rows:
        comment = retained_by_id[row["comment_id"]]
        period = _period_start(comment["published"], time_bins)
        key = (comment["platform"], period, row["target_id"], row["dimension"])
        grouped[key][row["comment_id"]].add(row["label"])
        comment_group[(row["comment_id"], row["target_id"], row["dimension"])].add(row["label"])

    groups = []
    author_cap_dropped = 0
    unit_field = {"comment": "comment_id", "author": "author", "thread": "thread_id", "channel": "channel"}[unit]
    for (platform, period, target_id, dimension), comment_labels in sorted(grouped.items(), key=lambda x: tuple("" if v is None else v for v in x[0])):
        entries = []
        unit_missing_excluded_n = 0
        for comment_id, labels in comment_labels.items():
            comment = retained_by_id[comment_id]
            value = comment[unit_field]
            if value is None:
                unit_missing_excluded_n += 1
                continue
            entries.append((comment, value, labels))
        if sensitivity["cap_author_contribution"]:
            by_author: dict[str, list[tuple[dict[str, Any], str, set[str]]]] = defaultdict(list)
            keep_unattributed = []
            for entry in entries:
                if entry[0]["author"] is None:
                    keep_unattributed.append(entry)
                else:
                    by_author[entry[0]["author"]].append(entry)
            capped = list(keep_unattributed)
            for author_entries in by_author.values():
                author_entries.sort(key=lambda e: (e[0]["published"] or datetime.max.replace(tzinfo=timezone.utc), e[0]["comment_id"]))
                capped.append(author_entries[0])
                author_cap_dropped += len(author_entries) - 1
            entries = capped

        unit_labels: dict[str, set[str]] = defaultdict(set)
        comments_for_label: dict[str, set[str]] = defaultdict(set)
        for comment, unit_value, labels in entries:
            unit_labels[unit_value].update(labels)
            for label in labels:
                comments_for_label[label].add(comment["comment_id"])
        denominator = len(unit_labels)
        if not denominator:
            continue
        relevant_comment_ids = {comment_id for ids in comments_for_label.values() for comment_id in ids}
        comments_n = len(relevant_comment_ids)
        authors_n = len({retained_by_id[cid]["author"] for cid in relevant_comment_ids if retained_by_id[cid]["author"] is not None})
        threads_n = len({retained_by_id[cid]["thread_id"] for cid in relevant_comment_ids})
        channels_n = len({retained_by_id[cid]["channel"] for cid in relevant_comment_ids})
        for label in sorted({label for labels in unit_labels.values() for label in labels}):
            numerator = sum(label in labels for labels in unit_labels.values())
            label_comment_ids = comments_for_label[label]
            state_counts = {state: 0 for state in _STATES}
            reaction_counts = {"available": {}, "current_user_excluded": {}}
            request_status_counts = {
                "ok": 0, "request_failed": 0, "no_update": 0, "wrong_peer_update": 0,
            }
            request_not_requested_comments = 0
            observed_times = []
            for comment_id in sorted(label_comment_ids):
                reaction = _reaction_view(retained_by_id[comment_id], snaps)
                state = reaction["reaction_state"]
                state_counts[state] += 1
                for status, count in reaction["request_status_counts"].items():
                    request_status_counts[status] += count
                if not any(reaction["request_status_counts"].values()):
                    request_not_requested_comments += 1
                if reaction["observed"] is not None:
                    observed_times.append(reaction["observed"])
                if state in reaction_counts and isinstance(reaction["breakdown"], list):
                    for item in reaction["breakdown"]:
                        kind = item.get("kind") or item.get("reaction_type") if isinstance(item, Mapping) else None
                        if (not isinstance(item, Mapping) or not isinstance(kind, str)
                                or not isinstance(item.get("key"), str) or type(item.get("count")) is not int
                                or item["count"] < 0):
                            raise ContractValidationError("reaction breakdown entries require kind/key and non-negative integer count")
                        key = f"{kind}:{item['key']}"
                        reaction_counts[state][key] = reaction_counts[state].get(key, 0) + item["count"]
            groups.append({
                "platform": platform,
                "period_start_utc": period,
                "target_id": target_id,
                "dimension": dimension,
                "label": label,
                "numerator": numerator,
                "denominator": denominator,
                "proportion": numerator / denominator,
                "proportion_basis": "units with label divided by units with any output for this target and dimension; abstentions remain in the denominator",
                "labels_may_overlap": True,
                "comments_n": len(label_comment_ids),
                "authors_n": len({retained_by_id[cid]["author"] for cid in label_comment_ids if retained_by_id[cid]["author"] is not None}),
                "threads_n": len({retained_by_id[cid]["thread_id"] for cid in label_comment_ids}),
                "channels_n": len({retained_by_id[cid]["channel"] for cid in label_comment_ids}),
                "reaction_state_counts": state_counts,
                "reaction_counts_by_state": reaction_counts,
                "reaction_comments_n": len(label_comment_ids),
                "reaction_metrics_unit": "comment",
                "reaction_request_attempts_n": sum(request_status_counts.values()),
                "reaction_request_status_counts": request_status_counts,
                "reaction_comments_not_requested_n": request_not_requested_comments,
                "reaction_observed_at_min_utc": _iso(min(observed_times)) if observed_times else None,
                "reaction_observed_at_max_utc": _iso(max(observed_times)) if observed_times else None,
                "included_records_n": len(label_comment_ids),
                "frame_excluded_records_n": excluded["exact_duplicate"] + excluded["candidate_coordination"],
                "frame_excluded_records_scope": "whole_input_frame",
                "unit_missing_excluded_n": unit_missing_excluded_n,
                "sampling_frame_limitation": "Observed selected comments only; not a population estimate.",
            })

    model = next(iter(model_versions), None)
    views = _report_views(groups, rows, retained_by_id, snaps, unit=unit)
    return {
        "schema_version": "1.1",
        "frame": {
            "sampling_frame": "observed_selected_comments",
            "platforms": sorted({item["platform"] for item in retained}),
            "comments_input": input_n,
            "comments_included": len(retained),
            "comments_excluded": excluded,
            "author_cap_dropped_group_memberships": author_cap_dropped,
            "interpretation_rows_input": len(interpretations),
            "model_id": model[0] if model else None,
            "model_version": model[1] if model else None,
        },
        "unit": unit,
        "time_bins": time_bins,
        "groups": groups,
        "views": views,
        "sensitivity": dict(sensitivity),
        "limitations": list(_LIMITATIONS),
        "semantic_quality_status": "not_evaluated",
    }
