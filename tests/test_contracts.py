import json
from pathlib import Path

from jsonschema import Draft202012Validator

from src.contracts import ContractValidationError, SCHEMA_DIR, validate_document, validate_incident_spec, validate_run_manifest


def valid_spec():
    return {
        "schema_version": "1.0",
        "incident_id": "case_demo_2026",
        "working_title": "Test incident",
        "scope": {"description": "Publicly documented event", "included": [], "excluded": []},
        "location_candidates": [],
        "time_window_utc": ["2026-11-02T00:00:00Z", "2026-11-05T23:59:59Z"],
        "languages": ["uk", "ru", "en"],
        "seed_urls": [],
        "platforms": ["telegram", "web"],
        "collection_policy": {"public_only": True, "allow_private_sources": False,
                              "access_methods": ["public_api", "public_web"]},
        "privacy_policy": "local_only_by_default",
        "allow_external_ai": False,
        "models": {"text_provider": "disabled", "video_provider": "disabled"},
        "budgets": {"max_queries_per_claim": 8, "max_new_sources_per_run": 500,
                    "max_video_minutes_to_analyze": 120, "max_cost_usd": None},
        "as_of_utc": "2026-11-07T18:00:00Z",
        "report": {"allow_unresolved_sections": True},
    }


def test_valid_incident_spec():
    validate_incident_spec(valid_spec())


def test_incident_spec_rejects_reversed_time_window():
    spec = valid_spec()
    spec["time_window_utc"] = ["2026-11-06T00:00:00Z", "2026-11-05T00:00:00Z"]
    try:
        validate_incident_spec(spec)
    except ContractValidationError as exc:
        assert "time window" in str(exc).lower()
    else:
        raise AssertionError("reversed time window should be invalid")


def test_incident_spec_rejects_private_source_collection():
    spec = valid_spec()
    spec["collection_policy"]["allow_private_sources"] = True
    try:
        validate_incident_spec(spec)
    except ContractValidationError:
        pass
    else:
        raise AssertionError("private-source collection must be forbidden")


def test_external_ai_requires_explicit_privacy_policy():
    spec = valid_spec()
    spec["allow_external_ai"] = True
    try:
        validate_incident_spec(spec)
    except ContractValidationError as exc:
        assert "privacy" in str(exc).lower() or "external" in str(exc).lower()
    else:
        raise AssertionError("external AI must require explicit privacy policy")


def test_incident_spec_rejects_naive_timestamp_and_negative_budget():
    spec = valid_spec()
    spec["as_of_utc"] = "2026-11-07T18:00:00"
    try:
        validate_incident_spec(spec)
    except ContractValidationError:
        pass
    else:
        raise AssertionError("naive timestamp should be invalid")

    spec = valid_spec()
    spec["budgets"]["max_new_sources_per_run"] = -1
    try:
        validate_incident_spec(spec)
    except ContractValidationError:
        pass
    else:
        raise AssertionError("negative budget should be invalid")


def test_run_manifest_validates_receipts_and_completion_order():
    manifest = {
        "schema_version": "1.0", "run_id": "run-1", "incident_id": "case_demo_2026",
        "state": "completed_bounded", "started_at_utc": "2026-11-07T17:00:00Z",
        "finished_at_utc": "2026-11-07T18:00:00Z", "producer": "social/1.0",
        "git_commit": "abcdef1234567", "policy_version": "1.0",
        "source_snapshots": [{"snapshot_id": "snap-1", "sha256": "a" * 64,
                               "access_status": "partial", "platform": "telegram",
                               "fetched_at_utc": "2026-11-07T17:30:00Z"}],
        "task_receipts": [{"task_id": "collect", "status": "partial",
                           "attempted_at_utc": "2026-11-07T17:20:00Z"}],
        "outputs": [{"path": "reports/run.md", "sha256": "b" * 64}],
    }
    validate_run_manifest(manifest)
    manifest["finished_at_utc"] = "2026-11-07T16:00:00Z"
    try:
        validate_run_manifest(manifest)
    except ContractValidationError as exc:
        assert "finished_at" in str(exc)
    else:
        raise AssertionError("finished timestamp must not precede start")


def test_json_schema_documents_and_synthetic_fixture_validate():
    for name in ("common.schema.json", "incident_spec.schema.json", "run_manifest.schema.json"):
        Draft202012Validator.check_schema(json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8")))
    fixture = json.loads(Path("tests/fixtures/incident_spec.valid.json").read_text(encoding="utf-8"))
    validate_incident_spec(fixture)


def test_transcript_correction_rejects_empty_or_reversed_audio_interval():
    correction = {
        "schema_version": "1.0",
        "correction_id": "correction-demo",
        "asset_sha256": "a" * 64,
        "audio_interval_ms": {"start_ms": 1000, "end_ms": 2000},
        "raw_text": "raw ASR wording",
        "candidate_text": "candidate wording",
        "correction_type": "domain_term",
        "status": "candidate_contextual",
        "basis": ["second_asr_hypothesis", "domain_terminology"],
        "materiality": "low",
        "quote_safe": False,
        "created_at_utc": "2026-11-08T00:00:00Z",
    }
    for invalid_end in (1000, 999):
        correction["audio_interval_ms"]["end_ms"] = invalid_end
        try:
            validate_document(correction, "transcript_correction.schema.json")
        except ContractValidationError as exc:
            assert "audio interval" in str(exc).lower()
        else:
            raise AssertionError("transcript correction interval must have positive duration")


def test_transcript_correction_quote_safe_requires_audio_listening():
    correction = {
        "schema_version": "1.0",
        "correction_id": "correction-demo",
        "asset_sha256": "a" * 64,
        "audio_interval_ms": {"start_ms": 1000, "end_ms": 2000},
        "raw_text": "raw ASR wording",
        "candidate_text": "candidate wording",
        "correction_type": "domain_term",
        "status": "verified_from_audio",
        "basis": ["audio_review"],
        "materiality": "low",
        "quote_safe": True,
        "review": {
            "method": "audio_listening",
            "reviewed_at_utc": "2026-11-08T00:00:00Z",
        },
        "created_at_utc": "2026-11-08T00:00:00Z",
    }
    validate_document(correction, "transcript_correction.schema.json")

    correction["review"]["method"] = "independent_reference_transcript"
    try:
        validate_document(correction, "transcript_correction.schema.json")
    except ContractValidationError:
        pass
    else:
        raise AssertionError("a reference transcript must not be labelled audio-verified")


def test_transcript_formatting_only_is_not_quote_safe_without_audio_review():
    correction = {
        "schema_version": "1.0",
        "correction_id": "correction-demo",
        "asset_sha256": "a" * 64,
        "audio_interval_ms": {"start_ms": 1000, "end_ms": 2000},
        "raw_text": "raw ASR wording",
        "candidate_text": "Raw ASR wording.",
        "correction_type": "punctuation",
        "status": "formatting_only",
        "basis": ["formatting"],
        "materiality": "low",
        "quote_safe": True,
        "created_at_utc": "2026-11-08T00:00:00Z",
    }
    try:
        validate_document(correction, "transcript_correction.schema.json")
    except ContractValidationError:
        pass
    else:
        raise AssertionError("formatting-only changes must not become quote-safe without audio review")

    correction["quote_safe"] = False
    validate_document(correction, "transcript_correction.schema.json")
