"""Deterministic stratified audit sampling for relevance decisions in a supplied panel.

This estimates missed relevance only within the explicitly supplied rejected-record
frame. Candidate signals define audit strata; the random remainder is essential because
the signals are not assumed to find every false negative.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any


_STRATA = (
    "rejected__candidate_no_toponym_context",
    "rejected__candidate_place_only",
    "rejected__candidate_reference_context",
    "rejected__random_remainder",
)
_PLACE = re.compile(
    r"борщаг[іi]в|борщагов|петропавл[іi]в|петропавлов|"
    r"borshch|borschag|borshchahiv|petropavl|софіївськ\w*\s+борщаг|"
    r"софиевск\w*\s+борщаг",
    re.IGNORECASE,
)
_TOPIC = re.compile(
    r"тцк|мобіліз|мобилиз|протест|бунт|заручник|заложник|підвал|подвал|"
    r"катув|тортур|пытк|травм|поран|госпітал|госпитал|поліці|полици|"
    r"військов|военн|сутичк|стычк|затрим|задерж|насил|сила|вбив|убий|"
    r"розсліду|расслед|адвокат|медик|лікар|врач|родичі|родственник",
    re.IGNORECASE,
)
_REFERENCE_KEYS = (
    "fwd_from_url", "fwd_channel", "reply_to_msg_id", "reply_to_url",
    "quote_text", "parent_text", "context_text", "links", "urls", "source_url",
)
_URL = re.compile(r"https?://\S+", re.IGNORECASE)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _stratum(row: dict, text_key: str) -> str:
    text = row.get(text_key) or ""
    if not isinstance(text, str):
        raise ValueError(f"{text_key} must be a string or null")
    place = bool(_PLACE.search(text))
    topic = bool(_TOPIC.search(text))
    has_reference = bool(_URL.search(text)) or any(
        value not in (None, "", [], {}) for key in _REFERENCE_KEYS if (value := row.get(key)) is not None
    )
    if topic and not place:
        return _STRATA[0]
    if place:
        return _STRATA[1]
    if has_reference:
        return _STRATA[2]
    return _STRATA[3]


def _validated_frame(
    records: list[dict], *, id_key: str, selected_key: str, text_key: str,
    reason_key: str,
) -> tuple[list[dict], list[dict], str]:
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    seen: set[str] = set()
    rejected = []
    selected_count = 0
    for index, source in enumerate(records):
        if not isinstance(source, dict):
            raise ValueError(f"record {index} must be an object")
        record_id = source.get(id_key)
        if not isinstance(record_id, str) or not record_id.strip() or record_id != record_id.strip():
            raise ValueError(f"record {index} requires non-empty {id_key}")
        if record_id in seen:
            raise ValueError(f"duplicate {id_key}: {record_id}")
        seen.add(record_id)
        selected = source.get(selected_key)
        if type(selected) is not bool:
            raise ValueError(f"{record_id}.{selected_key} must be boolean")
        if selected:
            selected_count += 1
            continue
        if text_key in source and source[text_key] is not None and not isinstance(source[text_key], str):
            raise ValueError(f"{record_id}.{text_key} must be a string or null")
        reason = source.get(reason_key)
        if reason is not None and (not isinstance(reason, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", reason)):
            raise ValueError(f"{record_id}.{reason_key} must be a lowercase reason code or null")
        stratum = _stratum(source, text_key)
        # Hash the rejected row for change detection without exporting its text or metadata.
        row_digest = hashlib.sha256(_canonical_json(source)).hexdigest()
        rejected.append({
            "record_id": record_id,
            "stratum_id": stratum,
            "reason_code": reason,
            "row_sha256": row_digest,
        })
    rejected.sort(key=lambda row: row["record_id"])
    frame_digest = hashlib.sha256(_canonical_json(rejected)).hexdigest()
    return rejected, [{"record_count": len(records), "selected_count": selected_count}], frame_digest


def stratified_relevance_sample(
    records: list[dict], *, sample_sizes: dict[str, int], seed: int,
    id_key: str = "post_id", selected_key: str = "relevant", text_key: str = "text_clean",
    reason_key: str = "relevance_reason_code", confidence_level: float = 0.95,
) -> dict:
    """Create a reproducible SRSWOR audit sample from all rejected records.

    Selection is a seeded SHA-256 ranking, so sampling is reproducible across Python
    versions. `sample_sizes` must explicitly name every non-empty stratum.
    """
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    _validate_confidence(confidence_level)
    frame_rows, totals, frame_sha = _validated_frame(
        records, id_key=id_key, selected_key=selected_key, text_key=text_key, reason_key=reason_key,
    )
    populations: dict[str, list[dict]] = {name: [] for name in _STRATA}
    for row in frame_rows:
        populations[row["stratum_id"]].append(row)
    nonempty = {name for name, rows in populations.items() if rows}
    if not isinstance(sample_sizes, dict) or set(sample_sizes) != nonempty:
        raise ValueError(f"allocation must name every non-empty stratum exactly; expected {sorted(nonempty)}")
    strata = []
    sample = []
    for stratum_id in _STRATA:
        rows = populations[stratum_id]
        if not rows:
            continue
        n = sample_sizes[stratum_id]
        if type(n) is not int or n < 1 or n > len(rows):
            raise ValueError(f"allocation for non-empty stratum {stratum_id} requires a positive allocation integer in [1, {len(rows)}]")
        N = len(rows)
        pi = n / N
        strata.append({"stratum_id": stratum_id, "population_count": N, "sample_count": n,
                       "inclusion_probability": pi})
        ranked = sorted(
            rows,
            key=lambda row: hashlib.sha256(
                f"{seed}\0{stratum_id}\0{row['record_id']}".encode("utf-8")
            ).digest(),
        )[:n]
        for row in ranked:
            item = {"record_id": row["record_id"], "stratum_id": stratum_id,
                    "inclusion_probability": pi}
            if row["reason_code"] is not None:
                item["reason_code"] = row["reason_code"]
            sample.append(item)
    sample.sort(key=lambda row: (row["stratum_id"], row["record_id"]))
    supplied_counts = totals[0]
    return {
        "schema_version": "relevance_sampling_manifest_v1",
        "frame_sha256": frame_sha,
        "design": {
            "method": "stratified_simple_random_sampling_without_replacement_sha256_rank",
            "seed": seed,
            "confidence_level": confidence_level,
            "id_key": id_key,
            "selection_key": selected_key,
            "text_key": text_key,
            "reason_key": reason_key,
            "supplied_record_count": supplied_counts["record_count"],
            "selected_record_count": supplied_counts["selected_count"],
            "rejected_frame_count": len(frame_rows),
            "population_scope": "rejected_records_in_supplied_panel_only",
            "scope_is_platform_wide": False,
            "candidate_signal_interpretation": "strata prioritize review candidates; random remainder remains in the frame because signals can miss false negatives",
        },
        "strata": strata,
        "sample": sample,
    }


def add_composite_record_ids(
    records: list[dict], *, component_keys: tuple[str, ...] | list[str],
    output_key: str = "_audit_record_id",
) -> list[dict]:
    """Return copies with an unambiguous JSON-array ID derived from component fields.

    Typed JSON encoding distinguishes e.g. ("ab", "c") from ("a", "bc") and
    string values from numeric Telegram message IDs. Duplicate tuples fail closed.
    """
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    if (not isinstance(component_keys, (tuple, list)) or not component_keys
            or any(not isinstance(key, str) or not key.strip() for key in component_keys)
            or len(set(component_keys)) != len(component_keys)):
        raise ValueError("component_keys must contain unique non-empty field names")
    if not isinstance(output_key, str) or not output_key.strip():
        raise ValueError("output_key must be a non-empty string")
    seen = set()
    result = []
    for index, source in enumerate(records):
        if not isinstance(source, dict):
            raise ValueError(f"record {index} must be an object")
        values = []
        for key in component_keys:
            value = source.get(key)
            if type(value) is str:
                if not value.strip() or value != value.strip():
                    raise ValueError(f"record {index}.{key} must be a non-empty trimmed string or integer")
            elif type(value) is not int:
                raise ValueError(f"record {index}.{key} must be a non-empty string or integer")
            values.append(value)
        composite = "composite-id-v1:" + json.dumps(values, ensure_ascii=False, separators=(",", ":"))
        if composite in seen:
            raise ValueError(f"duplicate composite ID from component fields: {composite}")
        seen.add(composite)
        row = dict(source)
        if output_key in source:
            raise ValueError(f"record {index} already contains reserved composite ID field {output_key}")
        row[output_key] = composite
        result.append(row)
    return result


def _validate_confidence(confidence_level: float) -> None:
    if type(confidence_level) not in {int, float} or not math.isfinite(confidence_level) or not 0.5 < confidence_level < 1:
        raise ValueError("confidence_level must be finite and in (0.5, 1)")


def finite_population_sample_size(
    *, population_size: int, margin_error: float, confidence_level: float = 0.95,
) -> int:
    """Conservative SRS sample size for a proportion (p=.5), with finite-population correction."""
    if type(population_size) is not int or population_size < 0:
        raise ValueError("population_size must be a non-negative integer")
    if not isinstance(margin_error, (int, float)) or not math.isfinite(margin_error) or not 0 < margin_error < 1:
        raise ValueError("margin_error must be finite and in (0, 1)")
    _validate_confidence(confidence_level)
    if population_size == 0:
        return 0
    z = _normal_quantile(0.5 + confidence_level / 2)
    n0 = z * z * 0.25 / (margin_error * margin_error)
    return min(population_size, math.ceil(n0 / (1 + (n0 - 1) / population_size)))


def _proportional_allocation_with_minimum(
    population_counts: dict[str, int], total_sample_size: int, minimum_per_stratum: int,
) -> dict[str, int]:
    """Integer proportional allocation with lower bounds and deterministic ties.

    First reserve ``min(N_h, minimum_per_stratum)`` observations in every stratum,
    then distribute the remainder proportionally to each stratum's remaining
    population using the largest-remainder method. This is transparent and stable;
    it is not claimed to be cost-optimal Neyman allocation.
    """
    names = sorted(population_counts)
    allocation = {name: min(population_counts[name], minimum_per_stratum) for name in names}
    remaining_sample = total_sample_size - sum(allocation.values())
    remaining_capacity = {name: population_counts[name] - allocation[name] for name in names}
    total_capacity = sum(remaining_capacity.values())
    if remaining_sample < 0 or remaining_sample > total_capacity:
        raise ValueError("total_sample_size is incompatible with stratum bounds")
    if not remaining_sample:
        return allocation
    if not total_capacity:
        raise ValueError("no remaining population capacity")
    quotas = {name: remaining_sample * remaining_capacity[name] / total_capacity for name in names}
    additions = {name: min(remaining_capacity[name], math.floor(quotas[name])) for name in names}
    for name, value in additions.items():
        allocation[name] += value
    left = total_sample_size - sum(allocation.values())
    order = sorted(
        (name for name in names if allocation[name] < population_counts[name]),
        key=lambda name: (-(quotas[name] - math.floor(quotas[name])), name),
    )
    while left:
        if not order:
            raise ValueError("allocation exhausted population before reaching requested total")
        progressed = False
        for name in order:
            if allocation[name] < population_counts[name]:
                allocation[name] += 1
                left -= 1
                progressed = True
                if not left:
                    break
        if not progressed:
            raise ValueError("allocation exhausted population before reaching requested total")
        order = [name for name in order if allocation[name] < population_counts[name]]
    return allocation


def conservative_stratified_half_width(
    population_counts: dict[str, int], sample_counts: dict[str, int], *, confidence_level: float = 0.95,
) -> float:
    """Worst-case normal half-width for a stratified binary proportion.

    Uses SRS without replacement within each stratum, ``S_h^2 <= 0.25``, and
    finite-population correction. This is a precision calculation conditional on
    the supplied finite frame, not a correction for panel/coverage bias.
    """
    _validate_confidence(confidence_level)
    if not isinstance(population_counts, dict) or not isinstance(sample_counts, dict):
        raise ValueError("population_counts and sample_counts must be mappings")
    names = {name for name, size in population_counts.items() if isinstance(name, str) and name}
    if len(names) != len(population_counts) or set(sample_counts) != names:
        raise ValueError("sample_counts must name every population stratum exactly")
    total_population = sum(population_counts.values())
    if not total_population:
        return 0.0
    variance = 0.0
    for name in sorted(names):
        N, n = population_counts[name], sample_counts[name]
        if type(N) is not int or N < 1:
            raise ValueError("population sizes must be positive integers")
        if type(n) is not int or not 1 <= n <= N:
            raise ValueError("sample sizes must be positive integers no greater than population sizes")
        weight = N / total_population
        if n < N:
            # For a finite binary population, the finite-population variance is
            # S_h^2 = N_h/(N_h-1) * P_h(1-P_h). Its conservative upper bound is
            # therefore 0.25*N_h/(N_h-1), not simply 0.25.
            finite_population_binary_bound = 0.25 * N / (N - 1)
            variance += weight * weight * (1 - n / N) * finite_population_binary_bound / n
    z = _normal_quantile(0.5 + confidence_level / 2)
    return z * math.sqrt(max(0.0, variance))


def plan_stratified_allocation(
    population_counts: dict[str, int], *, target_margin_error: float,
    minimum_sample_per_stratum: int = 1, confidence_level: float = 0.95,
) -> dict:
    """Find the smallest proportional stratified sample meeting a conservative
    overall margin target for a binary outcome.

    The target is based on the worst-case variance ``p_h(1-p_h) <= 0.25`` and
    finite-population correction. Integer apportionment ties break lexically.
    """
    if not isinstance(population_counts, dict):
        raise ValueError("population_counts must be a mapping")
    if any(not isinstance(name, str) or not name for name in population_counts):
        raise ValueError("stratum names must be non-empty strings")
    if any(type(size) is not int or size < 1 for size in population_counts.values()):
        raise ValueError("stratum population sizes must be positive integer values")
    if type(minimum_sample_per_stratum) is not int or minimum_sample_per_stratum < 1:
        raise ValueError("minimum_sample_per_stratum must be a positive integer")
    if (not isinstance(target_margin_error, (int, float)) or not math.isfinite(target_margin_error)
            or not 0 < target_margin_error < 1):
        raise ValueError("margin_error target must be finite and in (0, 1)")
    _validate_confidence(confidence_level)
    if not population_counts:
        return {
            "status": "empty_frame", "confidence_level": confidence_level,
            "target_margin_error": target_margin_error, "total_population": 0,
            "total_sample_size": 0, "achieved_conservative_half_width": None,
            "allocations": [], "assumptions": {
                "allocation_method": "proportional_to_remaining_population_with_minimum",
                "worst_case_binary_superpopulation_variance": 0.25,
                "finite_population_binary_variance_bound": "0.25 * N_h/(N_h-1)",
                "finite_population_correction": True,
            },
        }
    total_population = sum(population_counts.values())
    minimum_total = sum(min(size, minimum_sample_per_stratum) for size in population_counts.values())
    for total_n in range(minimum_total, total_population + 1):
        allocation = _proportional_allocation_with_minimum(
            population_counts, total_n, minimum_sample_per_stratum,
        )
        half_width = conservative_stratified_half_width(
            population_counts, allocation, confidence_level=confidence_level,
        )
        if half_width <= target_margin_error:
            rows = [{
                "stratum_id": name,
                "population_count": population_counts[name],
                "sample_count": allocation[name],
                "inclusion_probability": allocation[name] / population_counts[name],
            } for name in sorted(population_counts)]
            return {
                "status": "target_met",
                "confidence_level": confidence_level,
                "target_margin_error": target_margin_error,
                "achieved_conservative_half_width": half_width,
                "total_population": total_population,
                "total_sample_size": total_n,
                "allocations": rows,
                "assumptions": {
                    "allocation_method": "proportional_to_remaining_population_with_minimum",
                    "worst_case_binary_superpopulation_variance": 0.25,
                    "finite_population_binary_variance_bound": "0.25 * N_h/(N_h-1)",
                    "finite_population_correction": True,
                    "precision_scope": "supplied_finite_stratified_frame_only",
                    "does_not_correct": ["non_probability_panel_selection", "platform_access_gaps", "uncollected_records"],
                },
            }
    raise AssertionError("full census must produce zero variance and meet a positive target")


def hypergeometric_zero_probability(population_size: int, positive_count: int, sample_size: int) -> float:
    """Exact probability of zero observed positives in SRSWOR."""
    if type(population_size) is not int or population_size < 0:
        raise ValueError("population_size must be a non-negative integer")
    if type(positive_count) is not int or not 0 <= positive_count <= population_size:
        raise ValueError("positive_count must be between 0 and population_size")
    if type(sample_size) is not int or not 0 <= sample_size <= population_size:
        raise ValueError("sample_size must be between 0 and population_size")
    if sample_size > population_size - positive_count:
        return 0.0
    probability = 1.0
    for index in range(sample_size):
        probability *= (population_size - positive_count - index) / (population_size - index)
    return probability


def rare_miss_detection_sample_size(
    *, population_size: int, minimum_prevalence: float, confidence_level: float = 0.95,
) -> int:
    """Smallest SRSWOR n with 1-P(no hits) >= confidence at a specified finite-population prevalence."""
    if type(population_size) is not int or population_size < 0:
        raise ValueError("population_size must be a non-negative integer")
    if not isinstance(minimum_prevalence, (int, float)) or not math.isfinite(minimum_prevalence) or not 0 < minimum_prevalence <= 1:
        raise ValueError("minimum_prevalence must be finite and in (0, 1]")
    _validate_confidence(confidence_level)
    if population_size == 0:
        return 0
    positives = max(1, math.ceil(population_size * minimum_prevalence))
    alpha = 1 - confidence_level
    low, high = 0, population_size
    while low < high:
        mid = (low + high) // 2
        if hypergeometric_zero_probability(population_size, positives, mid) <= alpha:
            high = mid
        else:
            low = mid + 1
    return low


def _log_comb(n: int, k: int) -> float:
    if k < 0 or k > n:
        return -math.inf
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)


def _hypergeometric_cdf_at(N: int, K: int, n: int, observed: int) -> float:
    denominator = _log_comb(N, n)
    lower = max(0, n - (N - K))
    upper = min(observed, n, K)
    if lower > upper:
        return 0.0
    logs = [_log_comb(K, x) + _log_comb(N - K, n - x) - denominator for x in range(lower, upper + 1)]
    largest = max(logs)
    return min(1.0, math.exp(largest) * sum(math.exp(value - largest) for value in logs))


def _upper_success_count(N: int, n: int, observed: int, alpha: float) -> int:
    """One-sided exact hypergeometric upper confidence bound for total positives K."""
    if n == N:
        return observed
    low = observed
    high = N - (n - observed)
    while low < high:
        mid = (low + high + 1) // 2
        if _hypergeometric_cdf_at(N, mid, n, observed) >= alpha:
            low = mid
        else:
            high = mid - 1
    return low


def _normal_quantile(probability: float) -> float:
    # Acklam inverse-normal approximation, sufficient for sample-size and Wald intervals.
    if not 0 < probability < 1:
        raise ValueError("probability must be in (0, 1)")
    a = (-39.69683028665376, 220.9460984245205, -275.9285104469687,
         138.3577518672690, -30.66479806614716, 2.506628277459239)
    b = (-54.47609879822406, 161.5858368580409, -155.6989798598866,
         66.80131188771972, -13.28068155288572)
    c = (-0.007784894002430293, -0.3223964580411365, -2.400758277161838,
         -2.549732539343734, 4.374664141464968, 2.938163982698783)
    d = (0.007784695709041462, 0.3224671290700398, 2.445134137142996, 3.754408661907416)
    tail = 0.02425
    if probability < tail:
        q = math.sqrt(-2 * math.log(probability))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if probability > 1 - tail:
        q = math.sqrt(-2 * math.log(1 - probability))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    q = probability - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def analyze_relevance_audit(manifest: dict, adjudications: list[dict], *,
                            record_id_key: str = "record_id", label_key: str = "missed_relevant") -> dict:
    """Estimate missed-relevant share among rejected rows in the audited supplied frame."""
    if not isinstance(manifest, dict) or not isinstance(manifest.get("design"), dict):
        raise ValueError("manifest requires a design object")
    if manifest["design"].get("population_scope") != "rejected_records_in_supplied_panel_only":
        raise ValueError("manifest population scope is unsupported")
    confidence = manifest["design"].get("confidence_level", 0.95)
    _validate_confidence(confidence)
    strata = manifest.get("strata")
    sample = manifest.get("sample")
    if not isinstance(strata, list) or not isinstance(sample, list):
        raise ValueError("manifest requires strata and sample arrays")
    stratum_map = {}
    for item in strata:
        if not isinstance(item, dict):
            raise ValueError("strata entries must be objects")
        name, N, n = item.get("stratum_id"), item.get("population_count"), item.get("sample_count")
        if not isinstance(name, str) or not name or type(N) is not int or N < 1 or type(n) is not int or not 1 <= n <= N:
            raise ValueError("invalid stratum counts; each non-empty stratum requires a positive sample count")
        if name in stratum_map:
            raise ValueError(f"duplicate stratum: {name}")
        stratum_map[name] = {"N": N, "n": n, "items": []}
    sample_ids = set()
    for row in sample:
        if not isinstance(row, dict):
            raise ValueError("sample entries must be objects")
        rid = row.get("record_id")
        sid = row.get("stratum_id")
        if not isinstance(rid, str) or not rid or rid in sample_ids:
            raise ValueError("sample record IDs must be unique non-empty strings")
        if sid not in stratum_map:
            raise ValueError(f"sample references unknown stratum: {sid}")
        group = stratum_map[sid]
        if type(row.get("inclusion_probability")) not in {int, float} or not math.isclose(row["inclusion_probability"], group["n"] / group["N"], rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError(f"incorrect inclusion probability for {rid}")
        sample_ids.add(rid)
        group["items"].append(rid)
    if any(len(group["items"]) != group["n"] for group in stratum_map.values()):
        raise ValueError("manifest sample counts do not match strata")
    if not isinstance(adjudications, list):
        raise ValueError("adjudications must be a list")
    labels = {}
    for row in adjudications:
        if not isinstance(row, dict):
            raise ValueError("adjudication entries must be objects")
        rid = row.get(record_id_key)
        label = row.get(label_key)
        if not isinstance(rid, str) or rid not in sample_ids:
            raise ValueError(f"adjudication references unsampled record: {rid}")
        if rid in labels:
            raise ValueError(f"duplicate adjudication: {rid}")
        if type(label) is not bool:
            raise ValueError(f"{rid}.{label_key} must be boolean")
        labels[rid] = label
    missing = sample_ids - set(labels)
    if missing:
        raise ValueError(f"missing adjudication for {len(missing)} sampled record(s)")
    total_N = sum(group["N"] for group in stratum_map.values())
    if total_N == 0:
        return {"population_scope": "rejected_records_in_supplied_panel_only", "rejected_frame_count": 0,
                "estimated_missed_relevant_count": None, "estimated_missed_relevant_share_of_rejected_frame": None,
                "design_based_variance_of_share": None, "confidence_interval": None,
                "exact_upper_bound_missed_relevant_share": None, "interval_type": "not_estimable_empty_frame",
                "scope_is_platform_wide": False}
    estimate_total = 0.0
    variance_total = 0.0
    variance_known = True
    observed_total = 0
    stratum_results = []
    alpha = 1 - confidence
    exact_upper_total = 0
    H = max(1, len(stratum_map))
    degenerate_non_census_stratum = False
    for sid, group in stratum_map.items():
        N, n = group["N"], group["n"]
        y = sum(labels[rid] for rid in group["items"])
        observed_total += y
        estimate_total += N * y / n
        if n == N:
            exact_upper = y
        else:
            if y in {0, n}:
                degenerate_non_census_stratum = True
            if n < 2:
                variance_known = False
            else:
                sample_var = n / (n - 1) * (y / n) * (1 - y / n)
                var_total = N * N * (1 - n / N) * sample_var / n
                variance_total += var_total
            exact_upper = _upper_success_count(N, n, y, alpha / H)
        exact_upper_total += exact_upper
        stratum_results.append({"stratum_id": sid, "population_count": N, "sample_count": n,
                                "observed_missed_relevant": y, "estimated_missed_relevant_count": N * y / n,
                                "exact_one_sided_upper_count_familywise_adjusted": exact_upper})
    estimate_share = estimate_total / total_N
    variance_share = variance_total / (total_N * total_N) if variance_known else None
    globally_degenerate_sparse_sample = (observed_total == 0 or observed_total == len(sample_ids)) and any(
        group["n"] < group["N"] for group in stratum_map.values()
    )
    if variance_known and not degenerate_non_census_stratum:
        z = _normal_quantile(0.5 + confidence / 2)
        se = math.sqrt(max(0.0, variance_share))
        ci = [max(0.0, estimate_share - z * se), min(1.0, estimate_share + z * se)]
        count_se = math.sqrt(max(0.0, variance_total))
        count_ci = [max(0.0, estimate_total - z * count_se), min(float(total_N), estimate_total + z * count_se)]
    else:
        ci = None
        count_ci = None
    return {
        "population_scope": "rejected_records_in_supplied_panel_only",
        "scope_is_platform_wide": False,
        "frame_sha256": manifest.get("frame_sha256"),
        "confidence_level": confidence,
        "rejected_frame_count": total_N,
        "audited_record_count": len(sample_ids),
        "observed_missed_relevant_count": observed_total,
        "estimated_missed_relevant_count": estimate_total,
        "estimated_missed_relevant_count_ci": count_ci,
        "estimated_missed_relevant_share_of_rejected_frame": estimate_share,
        "design_based_variance_of_share": variance_share,
        "confidence_interval": ci,
        "interval_type": ("approximate_design_based_wald_interval_with_fpc" if ci is not None else
                          "not_estimable_from_singleton_sample" if not variance_known else
                          "not_reported_degenerate_sparse_sample_exact_upper_bound_provided" if globally_degenerate_sparse_sample else
                          "not_reported_degenerate_stratum_exact_upper_bound_provided" if degenerate_non_census_stratum else
                          "not_estimable"),
        "exact_one_sided_missed_relevant_count_upper_bound": exact_upper_total,
        "exact_upper_bound_missed_relevant_share": min(1.0, exact_upper_total / total_N),
        "exact_upper_bound_method": "stratified_hypergeometric_one_sided_bounds_with_bonferroni_alpha_allocation",
        "zero_observed_misses": observed_total == 0,
        "selected_record_count_in_supplied_panel": manifest["design"].get("selected_record_count"),
        "strata": stratum_results,
        "interpretation": "Estimate applies only to the supplied rejected-record sampling frame; it does not correct channel-panel, platform-access, or retrieval-coverage bias.",
    }


def _read_jsonl(path: Path) -> list[dict]:
    result = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"JSONL line {line_number} must contain an object")
        result.append(row)
    return result


def _atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise ValueError("output JSON must not be a symlink")
    if path.exists():
        raise FileExistsError(f"output JSON already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Atomic no-clobber publication: unlike os.replace, link fails if another
            # process creates the destination between the check and publication.
            os.link(temporary, path)
        except FileExistsError as exc:
            raise FileExistsError(f"output JSON already exists: {path}") from exc
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="JSONL of selected and rejected panel records")
    parser.add_argument("--output", required=True, type=Path, help="sampling manifest JSON output")
    parser.add_argument("--seed", required=True, type=int)
    parser.add_argument("--sample-size", action="append", default=[], metavar="STRATUM=N",
                        help="explicit sample allocation; repeat for each non-empty stratum")
    parser.add_argument("--sample-size-per-stratum", type=int,
                        help="apply the same maximum sample size to each non-empty stratum")
    parser.add_argument("--id-key", default="post_id")
    parser.add_argument("--composite-id-keys",
                        help="comma-separated source fields, e.g. channel,msg_id; use instead of --id-key")
    parser.add_argument("--selected-key", default="relevant")
    parser.add_argument("--text-key", default="text_clean")
    parser.add_argument("--reason-key", default="relevance_reason_code")
    args = parser.parse_args(argv)
    if args.input.resolve() == args.output.resolve():
        raise ValueError("input and output must be different files")
    if args.sample_size and args.sample_size_per_stratum is not None:
        raise ValueError("use either --sample-size or --sample-size-per-stratum")
    if args.sample_size_per_stratum is not None and args.sample_size_per_stratum < 0:
        raise ValueError("--sample-size-per-stratum must be non-negative")
    allocations = {}
    for item in args.sample_size:
        if "=" not in item:
            raise ValueError("--sample-size must be STRATUM=N")
        name, value = item.split("=", 1)
        if name in allocations:
            raise ValueError(f"duplicate allocation for {name}")
        try:
            allocations[name] = int(value)
        except ValueError as exc:
            raise ValueError(f"invalid sample size for {name}") from exc
    records = _read_jsonl(args.input)
    id_key = args.id_key
    if args.composite_id_keys is not None:
        if args.id_key != "post_id":
            raise ValueError("--composite-id-keys cannot be combined with a non-default --id-key")
        component_keys = tuple(part.strip() for part in args.composite_id_keys.split(","))
        records = add_composite_record_ids(records, component_keys=component_keys)
        id_key = "_audit_record_id"
    if args.sample_size_per_stratum is not None:
        frame_rows, _, _ = _validated_frame(records, id_key=id_key, selected_key=args.selected_key,
                                            text_key=args.text_key, reason_key=args.reason_key)
        populations: dict[str, int] = defaultdict(int)
        for row in frame_rows:
            populations[row["stratum_id"]] += 1
        allocations = {name: min(args.sample_size_per_stratum, count) for name, count in populations.items()}
    result = stratified_relevance_sample(
        records, sample_sizes=allocations, seed=args.seed,
        id_key=id_key, selected_key=args.selected_key, text_key=args.text_key, reason_key=args.reason_key,
    )
    _atomic_json(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
