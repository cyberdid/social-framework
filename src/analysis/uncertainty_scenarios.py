"""Descriptive scenario bounds for uncertain labels and incomplete target panels.

This module reports what changes under explicit assumptions. It does not turn a
non-probability corpus into a population estimate or a scenario range into a
confidence interval.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


_LABEL_STATES = {"target", "other", "unknown"}


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"JSONL line {line_number} must contain an object")
        rows.append(row)
    return rows


def _atomic_json(path: Path, payload: dict) -> None:
    if path.is_symlink():
        raise ValueError("output JSON must not be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _validate_records(records: list[dict], *, post_id_key: str, channel_key: str,
                      label_key: str, group_key: str, view_key: str) -> list[dict]:
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    seen = set()
    channel_groups: dict[str, str | None] = {}
    result = []
    for index, source in enumerate(records):
        if not isinstance(source, dict):
            raise ValueError(f"record {index} must be an object")
        row = dict(source)
        post_id = row.get(post_id_key)
        channel = row.get(channel_key)
        if not isinstance(post_id, str) or not post_id.strip():
            raise ValueError(f"record {index} requires non-empty {post_id_key}")
        if post_id in seen:
            raise ValueError(f"duplicate {post_id_key}: {post_id}")
        seen.add(post_id)
        if not isinstance(channel, str) or not channel.strip():
            raise ValueError(f"{post_id} requires non-empty {channel_key}")
        label = row.get(label_key, "unknown")
        if label not in _LABEL_STATES:
            raise ValueError(f"{post_id}.{label_key} must be one of {sorted(_LABEL_STATES)}")
        group = row.get(group_key)
        if group is not None and (not isinstance(group, str) or not group.strip()):
            raise ValueError(f"{post_id}.{group_key} must be a non-empty string or null")
        if isinstance(group, str) and group.strip().casefold() in {"unknown", "unassigned", "u"}:
            group = None
        if channel in channel_groups and channel_groups[channel] != group:
            raise ValueError(f"conflicting channel_group labels for channel {channel}")
        channel_groups[channel] = group
        views = row.get(view_key)
        if views is not None and (type(views) not in {int, float} or not math.isfinite(views) or views < 0):
            raise ValueError(f"{post_id}.{view_key} must be a finite non-negative number or null")
        family = row.get("source_family_id")
        lineage_status = row.get("lineage_status", "unknown")
        if family is not None and (not isinstance(family, str) or not family.strip()):
            raise ValueError(f"{post_id}.source_family_id must be a non-empty string or null")
        if lineage_status not in {"confirmed_dependency_family", "candidate", "unknown"}:
            raise ValueError(f"{post_id}.lineage_status is unsupported")
        if lineage_status == "confirmed_dependency_family" and family is None:
            raise ValueError(f"{post_id} requires source_family_id for confirmed dependency family")
        row["_target_state"] = label
        row["_group"] = group
        row["_views"] = float(views) if views is not None else None
        row["_post_id"] = post_id
        row["_channel"] = channel
        result.append(row)
    return result


def _ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _target_flag(row: dict, unknown_as_target: bool) -> bool:
    return row["_target_state"] == "target" or (unknown_as_target and row["_target_state"] == "unknown")


def _selected_group_rows(rows: list[dict], target_group: str | None, unknown_group_as_target: bool) -> list[dict]:
    if target_group is None:
        return rows
    return [row for row in rows
            if row["_group"] == target_group or (unknown_group_as_target and row["_group"] is None)]


def _measure(rows: list[dict], *, unknown_label_as_target: bool) -> dict:
    target_rows = [row for row in rows if _target_flag(row, unknown_label_as_target)]
    channel_post_counts = Counter(row["_channel"] for row in rows)
    target_channels = {row["_channel"] for row in target_rows}
    known_view_rows = [row for row in rows if row["_views"] is not None]
    target_view_sum = sum(row["_views"] for row in target_rows if row["_views"] is not None)
    all_view_sum = sum(row["_views"] for row in known_view_rows)
    return {
        "post_count": len(rows),
        "target_post_count": len(target_rows),
        "target_post_share_observed_corpus": _ratio(len(target_rows), len(rows)),
        "channel_count": len(channel_post_counts),
        "channels_with_target_post": len(target_channels),
        "target_channel_share_observed_corpus": _ratio(len(target_channels), len(channel_post_counts)),
        "snapshot_view_counter_rows": len(known_view_rows),
        "snapshot_view_counter_missing_rows": len(rows) - len(known_view_rows),
        "target_snapshot_view_counter_sum": target_view_sum,
        "all_snapshot_view_counter_sum": all_view_sum,
        "target_share_of_snapshot_view_counter_sum": _ratio(target_view_sum, all_view_sum),
        "view_counter_interpretation": "sum_of_platform_counter_snapshots_not_unique_people_or_reach",
    }


def _top_channels(rows: list[dict], count: int, basis: str) -> list[str]:
    if count <= 0:
        return []
    scores: dict[str, float] = defaultdict(float)
    for row in rows:
        if basis == "views_snapshot":
            if row["_views"] is not None:
                scores[row["_channel"]] += row["_views"]
        else:
            scores[row["_channel"]] += 1
    return [channel for channel, _ in sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:count]]


def _validate_coverage_bounds(bounds: dict | None) -> dict | None:
    if bounds is None:
        return None
    if not isinstance(bounds, dict):
        raise ValueError("coverage_bounds must be an object or null")
    required = ("missing_target_min", "missing_target_max", "missing_non_target_min", "missing_non_target_max")
    normalized = {}
    for key in required:
        value = bounds.get(key)
        if type(value) is not int or value < 0:
            raise ValueError(f"coverage_bounds.{key} must be a non-negative integer")
        normalized[key] = value
    if normalized["missing_target_min"] > normalized["missing_target_max"]:
        raise ValueError("missing_target_min cannot exceed missing_target_max")
    if normalized["missing_non_target_min"] > normalized["missing_non_target_max"]:
        raise ValueError("missing_non_target_min cannot exceed missing_non_target_max")
    return normalized


def analyze_uncertainty_scenarios(
    records: list[dict], *, target_label: str, target_group: str | None = None,
    top_k: tuple[int, ...] | list[int] = (0, 1, 5), top_k_basis: str = "views_snapshot",
    coverage_bounds: dict | None = None, post_id_key: str = "post_id",
    channel_key: str = "channel_id", label_key: str = "target_label_state",
    group_key: str = "channel_group", view_key: str = "views_snapshot",
) -> dict:
    """Return explicit descriptive scenarios and optional coverage partial bounds.

    `target_label_state` is `target`, `other`, or `unknown`. Channel uncertainty
    is represented by null/unknown `channel_group`. Source lineage is only
    counted as an observed dependency family when a confirmed family ID exists.
    This is deterministic sensitivity analysis, not inferential uncertainty.
    """
    if not isinstance(target_label, str) or not target_label.strip():
        raise ValueError("target_label must be a non-empty string")
    if target_group is not None and (not isinstance(target_group, str) or not target_group.strip()):
        raise ValueError("target_group must be a non-empty string or null")
    if top_k_basis not in {"views_snapshot", "post_count"}:
        raise ValueError("top_k_basis must be views_snapshot or post_count")
    if not isinstance(top_k, (tuple, list)) or not top_k or any(type(value) is not int or value < 0 for value in top_k):
        raise ValueError("top_k must contain one or more non-negative integers")
    if len(set(top_k)) != len(top_k):
        raise ValueError("top_k values must be unique")
    bounds = _validate_coverage_bounds(coverage_bounds)
    rows = _validate_records(records, post_id_key=post_id_key, channel_key=channel_key,
                             label_key=label_key, group_key=group_key, view_key=view_key)

    scenarios = []
    group_assumptions = ([("all_rows", None)] if target_group is None else
                         [("unknown_group_excluded", False), ("unknown_group_as_target", True)])
    for group_name, include_unknown_group in group_assumptions:
        for unknown_target, label_name in ((False, "unknown_labels_as_non_target"),
                                           (True, "unknown_labels_as_target")):
            group_rows = _selected_group_rows(rows, target_group, bool(include_unknown_group))
            for excluded_count in sorted(top_k):
                excluded_channels = _top_channels(group_rows, excluded_count, top_k_basis)
                kept = [row for row in group_rows if row["_channel"] not in set(excluded_channels)]
                scenario_id = f"{group_name}__{label_name}__exclude_top_{excluded_count}"
                scenarios.append({
                    "scenario_id": scenario_id,
                    "assumptions": {"unknown_post_labels": label_name,
                                    "unknown_channel_group": group_name,
                                    "excluded_top_k_channels": excluded_count,
                                    "top_k_basis": top_k_basis},
                    "excluded_channel_ids": excluded_channels,
                    "metrics": _measure(kept, unknown_label_as_target=unknown_target),
                })

    lower = _measure(rows, unknown_label_as_target=False)
    upper = _measure(rows, unknown_label_as_target=True)
    labels = Counter(row["_target_state"] for row in rows)
    groups = Counter(row["_group"] if row["_group"] is not None else "unknown" for row in rows)
    lineage_families = {row.get("source_family_id") for row in rows
                        if row.get("lineage_status") == "confirmed_dependency_family"}
    lineage_unknown = sum(row.get("lineage_status", "unknown") == "unknown" for row in rows)
    lineage_candidate = sum(row.get("lineage_status") == "candidate" for row in rows)
    known_family_ids = {family for family in lineage_families if isinstance(family, str)}
    lineage_summary = {
        "confirmed_dependency_family_count": len(known_family_ids),
        "candidate_lineage_record_count": lineage_candidate,
        "unknown_lineage_record_count": lineage_unknown,
        "possible_distinct_family_count_if_each_unknown_were_distinct": len(known_family_ids) + lineage_unknown,
        "independent_source_root_count": None,
        "interpretation": "dependency families are not independent source roots; unknown lineage is not assumed independent",
    }

    coverage_result = None
    if bounds is not None:
        observed_min = lower["target_post_count"]
        observed_max = upper["target_post_count"]
        observed_total = len(rows)
        lower_denominator = observed_total + bounds["missing_target_min"] + bounds["missing_non_target_max"]
        upper_denominator = observed_total + bounds["missing_target_max"] + bounds["missing_non_target_min"]
        coverage_result = {
            "unit": "posts",
            "observed_corpus_target_count_lower": observed_min,
            "observed_corpus_target_count_upper": observed_max,
            "missing_posts_assumptions": bounds,
            "target_share_lower": _ratio(observed_min + bounds["missing_target_min"], lower_denominator),
            "target_share_upper": _ratio(observed_max + bounds["missing_target_max"], upper_denominator),
            "interval_type": "scenario_partial_identification_bounds_not_confidence_interval",
            "population_claim": False,
        }

    return {
        "schema_version": "1.0",
        "producer": "social.analysis.uncertainty_scenarios@1.0",
        "analysis_type": "descriptive_sensitivity_and_partial_identification",
        "target_label": target_label,
        "target_group": target_group,
        "sample_design": "targeted_nonprobability_panel_unless_separately_documented",
        "input_record_count": len(rows),
        "observed_label_state_counts": dict(sorted(labels.items())),
        "observed_channel_group_counts": dict(sorted(groups.items())),
        "observed_corpus_bounds": {
            "target_post_share_lower": lower["target_post_share_observed_corpus"],
            "target_post_share_upper": upper["target_post_share_observed_corpus"],
            "target_channel_share_lower": lower["target_channel_share_observed_corpus"],
            "target_channel_share_upper": upper["target_channel_share_observed_corpus"],
            "target_snapshot_view_counter_share_lower": lower["target_share_of_snapshot_view_counter_sum"],
            "target_snapshot_view_counter_share_upper": upper["target_share_of_snapshot_view_counter_sum"],
            "interval_type": "unknown_label_scenario_bounds_within_observed_corpus_not_confidence_interval",
        },
        "coverage_partial_identification": coverage_result,
        "lineage": lineage_summary,
        "scenarios": scenarios,
        "limitations": [
            "The input is a target panel, not a probability sample; these summaries do not estimate population prevalence.",
            "Scenario bounds are conditional on explicit assumptions and are not confidence or credible intervals.",
            "Snapshot view counters are summed platform counters, not unique people, reach, or exposure.",
            "Unknown channel labels and post labels remain unknown outside the named scenarios.",
            "Confirmed dependency families do not establish independent source roots; no independence is inferred from missing edges.",
            "Coverage bounds are produced only from caller-supplied integer assumptions; the module does not infer missing-record counts.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Post-level JSONL scenario input")
    parser.add_argument("--output", required=True, type=Path, help="Output JSON report")
    parser.add_argument("--target-label", required=True)
    parser.add_argument("--target-group")
    parser.add_argument("--top-k", nargs="+", type=int, default=[0, 1, 5])
    parser.add_argument("--top-k-basis", choices=("views_snapshot", "post_count"), default="views_snapshot")
    parser.add_argument("--coverage-bounds", type=Path, help="Optional JSON with four missing-post count bounds")
    args = parser.parse_args(argv)
    input_path = args.input.expanduser().resolve(strict=True)
    output_path = args.output.expanduser()
    if output_path.is_symlink():
        raise ValueError("output JSON must not be a symlink")
    if output_path.resolve() == input_path:
        raise ValueError("input and output must be different files")
    coverage = json.loads(args.coverage_bounds.read_text(encoding="utf-8")) if args.coverage_bounds else None
    result = analyze_uncertainty_scenarios(_read_jsonl(input_path), target_label=args.target_label,
                                           target_group=args.target_group, top_k=args.top_k,
                                           top_k_basis=args.top_k_basis, coverage_bounds=coverage)
    _atomic_json(output_path, result)
    print(json.dumps({"input_record_count": result["input_record_count"],
                      "scenario_count": len(result["scenarios"]),
                      "coverage_bounds": result["coverage_partial_identification"] is not None}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
