import hashlib
import json
import os
from pathlib import Path
import pytest

from src.contracts import ContractValidationError, validate_document

SOCIAL_MEDIA_ROOT = Path(
    os.environ.get("SOCIAL_MEDIA_ROOT", Path.home() / "Downloads" / "SocialIncidentVideos")
).expanduser()
CANDIDATES_PATH = Path(os.environ.get(
    "SOCIAL_ASR_SECOND_PASS_PATH",
    SOCIAL_MEDIA_ROOT / "telegram/analysis/asr-correction-second-pass-20261108/correction_candidates.jsonl",
)).expanduser()
RAW_ASR_PATH = Path(os.environ.get(
    "SOCIAL_ASR_RAW_PATH",
    SOCIAL_MEDIA_ROOT / "telegram/analysis/asr-bulk-20261108/telegram_text_observations.jsonl",
)).expanduser()
RECEIPT_PATH = Path("reports/video-pipeline/asr-correction-receipt-2026-11-08.json")

EXPECTED_SIDECAR_SHA256 = "cfcf8ec7bffc2585ff3545141c8d34b12ba7ef71b9709b2b0b6b66060594cad3"
EXPECTED_RAW_ASR_SHA256 = "987ee53ff56a477e86347509786ed5d1bc7f5e908bb18037c202bb023ff43912"
EXPECTED_RAW_ASR_BYTES = 10462222


def _make_candidate(**overrides):
    base = {
        "schema_version": "1.0",
        "correction_id": "test-candidate-001",
        "asset_sha256": "3c5a89bb6174a810dc2e9c12b7f0e74f88062409f8724d1421c43d9e03403a5b",
        "audio_interval_ms": {"start_ms": 2920, "end_ms": 3520},
        "raw_text": "дислотасії",
        "candidate_text": "дислокації",
        "correction_type": "domain_term",
        "status": "candidate_contextual",
        "basis": ["second_asr_hypothesis", "domain_terminology"],
        "materiality": "low",
        "quote_safe": False,
        "created_at_utc": "2026-11-08T12:00:00Z",
        "limitations": ["Second ASR pass suggestion; unverified by human audio listening."],
    }
    base.update(overrides)
    return base


def test_schema_validation_valid_candidate():
    candidate = _make_candidate()
    validate_document(candidate, "transcript_correction.schema.json")


@pytest.mark.parametrize("invalid_interval", [
    {"start_ms": 3000, "end_ms": 3000},  # empty duration
    {"start_ms": 3500, "end_ms": 2000},  # reversed duration
    {"start_ms": 1000, "end_ms": 0},     # non-positive end_ms
])
def test_schema_rejects_empty_or_non_positive_intervals(invalid_interval):
    candidate = _make_candidate(audio_interval_ms=invalid_interval)
    with pytest.raises(ContractValidationError):
        validate_document(candidate, "transcript_correction.schema.json")


@pytest.mark.parametrize("status", [
    "candidate_contextual",
    "needs_audio_review",
    "formatting_only",
    "rejected",
])
def test_quote_safe_false_strictly_enforced_for_unverified_statuses(status):
    candidate = _make_candidate(status=status, quote_safe=True)
    with pytest.raises(ContractValidationError):
        validate_document(candidate, "transcript_correction.schema.json")

    # When quote_safe is False, it passes validation
    candidate["quote_safe"] = False
    validate_document(candidate, "transcript_correction.schema.json")


def test_verified_from_audio_requires_audio_listening_and_audio_review_basis():
    candidate = _make_candidate(
        status="verified_from_audio",
        quote_safe=True,
        basis=["audio_review"],
        review={"method": "audio_listening", "reviewed_at_utc": "2026-11-08T13:00:00Z"},
    )
    validate_document(candidate, "transcript_correction.schema.json")

    # Reject if review method is independent reference transcript rather than audio listening
    candidate_bad_method = dict(candidate)
    candidate_bad_method["review"] = {
        "method": "independent_reference_transcript",
        "reviewed_at_utc": "2026-11-08T13:00:00Z",
    }
    with pytest.raises(ContractValidationError):
        validate_document(candidate_bad_method, "transcript_correction.schema.json")

    # Reject if basis lacks audio_review
    candidate_bad_basis = dict(candidate)
    candidate_bad_basis["basis"] = ["second_asr_hypothesis"]
    with pytest.raises(ContractValidationError):
        validate_document(candidate_bad_basis, "transcript_correction.schema.json")


@pytest.mark.skipif(not CANDIDATES_PATH.exists(), reason="Candidate queue file not present")
def test_live_correction_candidates_sidecar_queue_invariants():
    content = CANDIDATES_PATH.read_text(encoding="utf-8")
    actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    assert actual_hash == EXPECTED_SIDECAR_SHA256

    records = [json.loads(line) for line in content.splitlines() if line.strip()]
    assert len(records) == 10

    sensitive_keywords = ["хребт", "СЗЧ", "сича/свища", "15 фонів", "перелуманим", "сазачешники"]

    for record in records:
        # Schema and interval check
        validate_document(record, "transcript_correction.schema.json")
        interval = record["audio_interval_ms"]
        assert interval["end_ms"] > interval["start_ms"]

        # Invariant: non-audio-listened must have quote_safe: False
        assert record["quote_safe"] is False

        # Invariant: sensitive and legal phrases must be needs_audio_review
        raw = record.get("raw_text", "")
        cand = record.get("candidate_text") or ""
        combined = f"{raw} {cand}"
        if any(kw in combined for kw in sensitive_keywords):
            assert record["status"] == "needs_audio_review"


@pytest.mark.skipif(not RAW_ASR_PATH.exists(), reason="Raw ASR bulk file not present")
def test_raw_asr_immutability():
    data = RAW_ASR_PATH.read_bytes()
    assert len(data) == EXPECTED_RAW_ASR_BYTES
    actual_hash = hashlib.sha256(data).hexdigest()
    assert actual_hash == EXPECTED_RAW_ASR_SHA256


def test_asr_correction_receipt_matches_invariants():
    assert RECEIPT_PATH.exists()
    data = json.loads(RECEIPT_PATH.read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0"
    assert data["candidate_sidecar"]["sha256"] == EXPECTED_SIDECAR_SHA256
    assert data["raw_asr_preservation"]["sha256"] == EXPECTED_RAW_ASR_SHA256
    assert data["raw_asr_preservation"]["byte_size"] == EXPECTED_RAW_ASR_BYTES
    assert data["raw_asr_preservation"]["raw_asr_modified"] is False
    assert data["candidate_sidecar"]["quote_safe_distribution"]["true"] == 0
    assert data["candidate_sidecar"]["quote_safe_distribution"]["false"] == 10
    assert data["candidate_sidecar"]["interval_semantic_validation"]["interval_violations"] == 0
