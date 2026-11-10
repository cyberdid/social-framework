"""Replay a saved relevance audit sample from a Social posts_clean JSONL frame.

The receipt contains hashes and aggregate match states only; sample identifiers
and source text are kept in memory and are never written to the receipt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from src.analysis.prep import relevance_decision
from src.analysis.relevance_sampling_audit_v1 import (
    _atomic_json,
    _read_jsonl,
    add_composite_record_ids,
    stratified_relevance_sample,
)


def replay_social_sample(source_records: list[dict], reference_manifest: dict) -> dict:
    """Reconstruct the in-window frame and compare a prior manifest privately.

    Social's time/relevance decision is recomputed with the current ``prep`` rule.
    The source rows are retained, `relevant` and reason code are overwritten with
    that decision, and channel-scoped IDs are constructed from `(channel,msg_id)`.
    The native `links` field is preserved and recognized by the sampler.
    """
    if not isinstance(source_records, list):
        raise ValueError("source_records must be a list")
    if not isinstance(reference_manifest, dict):
        raise ValueError("reference_manifest must be an object")
    design = reference_manifest.get("design")
    if not isinstance(design, dict):
        raise ValueError("reference manifest requires a design object")
    strata = reference_manifest.get("strata")
    sample = reference_manifest.get("sample")
    if not isinstance(strata, list) or not isinstance(sample, list):
        raise ValueError("reference manifest requires strata and sample arrays")

    in_scope = []
    excluded = 0
    for index, source in enumerate(source_records):
        if not isinstance(source, dict):
            raise ValueError(f"source record {index} must be an object")
        if "t_kyiv" not in source or not isinstance(source.get("text_clean"), str):
            raise ValueError(f"source record {index} requires t_kyiv and text_clean")
        relevant, reason = relevance_decision(source["text_clean"], source["t_kyiv"])
        if reason == "outside_time_window":
            excluded += 1
            continue
        in_scope.append({
            **source,
            "relevant": relevant,
            "relevance_reason_code": reason,
        })

    prepared = add_composite_record_ids(
        in_scope,
        component_keys=("channel", "msg_id"),
        output_key="_audit_record_id",
    )
    allocations = {}
    for item in strata:
        if not isinstance(item, dict):
            raise ValueError("reference manifest strata entries must be objects")
        stratum_id = item.get("stratum_id")
        sample_count = item.get("sample_count")
        if not isinstance(stratum_id, str) or type(sample_count) is not int:
            raise ValueError("reference manifest strata require stratum_id and integer sample_count")
        if stratum_id in allocations:
            raise ValueError(f"duplicate reference stratum: {stratum_id}")
        allocations[stratum_id] = sample_count

    computed = stratified_relevance_sample(
        prepared,
        sample_sizes=allocations,
        seed=design.get("seed"),
        id_key="_audit_record_id",
        selected_key="relevant",
        text_key="text_clean",
        reason_key="relevance_reason_code",
        confidence_level=design.get("confidence_level", 0.95),
    )

    expected_ids = [item.get("record_id") for item in sample if isinstance(item, dict)]
    if len(expected_ids) != len(sample) or any(not isinstance(value, str) for value in expected_ids):
        raise ValueError("reference manifest sample entries require record_id strings")
    if len(set(expected_ids)) != len(expected_ids):
        raise ValueError("reference manifest sample contains duplicate record IDs")
    computed_ids = [item["record_id"] for item in computed["sample"]]

    expected_source_sha = design.get("frame_source_sha256")
    actual_strata = computed["strata"]
    return {
        "schema_version": "relevance_sampling_replay_receipt_v1",
        "source_record_count": len(source_records),
        "time_window_excluded_count": excluded,
        "in_scope_record_count": len(in_scope),
        "selected_record_count": computed["design"]["selected_record_count"],
        "rejected_record_count": computed["design"]["rejected_frame_count"],
        "sample_record_count": len(computed_ids),
        "strata": actual_strata,
        "strata_match": actual_strata == strata,
        "sample_membership_match": set(computed_ids) == set(expected_ids),
        "frame_digest_match": computed["frame_sha256"] == reference_manifest.get("frame_sha256"),
        "expected_frame_sha256": reference_manifest.get("frame_sha256"),
        "computed_frame_sha256": computed["frame_sha256"],
        "expected_source_sha256": expected_source_sha,
        "interpretation": (
            "This replay checks the in-scope panel frame and sample selection only. "
            "It does not adjudicate relevance, estimate recall, or establish platform-wide coverage."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Social posts_clean JSONL")
    parser.add_argument("--manifest", required=True, type=Path, help="private prior sampling manifest")
    parser.add_argument("--output", required=True, type=Path, help="content-free replay receipt JSON")
    args = parser.parse_args(argv)
    if args.output.resolve() in {args.input.resolve(), args.manifest.resolve()}:
        raise ValueError("output must be different from both inputs")
    source_bytes = args.input.read_bytes()
    manifest_bytes = args.manifest.read_bytes()
    source_records = _read_jsonl(args.input)
    reference_manifest = json.loads(manifest_bytes)
    receipt = replay_social_sample(source_records, reference_manifest)
    receipt["source_sha256"] = hashlib.sha256(source_bytes).hexdigest()
    receipt["reference_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    expected_source_sha = receipt.get("expected_source_sha256")
    receipt["source_sha256_matches_reference"] = (
        receipt["source_sha256"] == expected_source_sha if expected_source_sha else None
    )
    receipt["replay_implementation_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    _atomic_json(args.output, receipt)
    print(json.dumps({
        "output": str(args.output),
        "strata_match": receipt["strata_match"],
        "sample_membership_match": receipt["sample_membership_match"],
        "frame_digest_match": receipt["frame_digest_match"],
        "source_sha256_matches_reference": receipt["source_sha256_matches_reference"],
        "counts": {
            key: receipt[key] for key in (
                "source_record_count", "time_window_excluded_count", "in_scope_record_count",
                "selected_record_count", "rejected_record_count", "sample_record_count",
            )
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
