"""Synthetic acceptance smoke test for the existing evidence pipeline APIs.

Every input in this test is fictional and marked as a test fixture. A passing
test checks structural linkage and byte/quote integrity; it does not adjudicate
any real-world claim.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.analysis.claim_verification import (
    append_verification_result,
    aggregate_verification,
    create_evidence_assessment,
    retrieve_evidence_candidates,
)
from src.analysis.evaluation_harness import verify_report_bundle
from src.analysis.report_generator import ReportValidationError, build_report_bundle
from src.analysis.source_ingest_bridge import ingest_source_watch_manifest
from src.evidence_ledger import EvidenceLedger


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "e2e-synthetic"
AS_OF_UTC = "2026-11-08T12:00:00Z"
CAPTURED_AT_UTC = "2026-11-07T10:00:00Z"
SYNTHETIC_SCOPE = {
    "description": "Synthetic fixture only; no real incident or adjudication",
    "time_window": ["2026-11-07", "2026-11-08"],
}
SYNTHETIC_STATEMENT = (
    "SYNTHETIC FIXTURE ONLY: The fictional laboratory displayed a blue lantern "
    "beside a paper moon."
)


def _passages_from_ledger(records: list[dict]) -> list[dict]:
    sources = {row["record_id"]: row for row in records if row["record_type"] == "Source"}
    artifacts = {row["record_id"]: row for row in records if row["record_type"] == "Artifact"}
    passages = []
    for row in records:
        if row["record_type"] != "Passage":
            continue
        artifact = artifacts[row["artifact_id"]]
        source = sources[artifact["source_id"]]
        passages.append({
            "passage_id": row["record_id"],
            "source_id": artifact["source_id"],
            "artifact_id": artifact["record_id"],
            "captured_at_utc": artifact["captured_at"],
            "text": row["text"],
            "source_uri": source["uri"],
            "source_title": source["title"],
            "artifact_sha256": artifact["sha256"],
            "artifact_path": artifact["path"],
            "locator": row["locator"],
            # Source Watch filtered text is not mapped to raw HTML byte or
            # character offsets. Claim candidates must retain that limitation.
            "artifact_start_offset": None,
        })
    return passages


def _report_evidence(passage: dict, candidate: dict) -> dict:
    return {
        "evidence_id": passage["passage_id"],
        "source_id": passage["source_id"],
        "source_title": passage["source_title"],
        "source_uri": passage["source_uri"],
        "artifact_id": passage["artifact_id"],
        "artifact_sha256": passage["artifact_sha256"],
        "captured_at_utc": passage["captured_at_utc"],
        "text": passage["text"],
        "locator": {
            "kind": "text_span",
            "start_offset": candidate["passage_start_offset"],
            "end_offset": candidate["passage_end_offset"],
            "offset_unit": candidate["passage_offset_unit"],
            "quote": candidate["quote"],
        },
    }


def _report_context() -> dict:
    return {
        "incident_id": "synthetic-e2e-no-real-incident",
        "run_id": "synthetic-e2e-run",
        "as_of_utc": AS_OF_UTC,
        "working_title": "SYNTHETIC TEST ONLY — not an incident report",
        "git_commit": None,
        "scope": SYNTHETIC_SCOPE,
        "coverage": {"synthetic_fixture": {"status": "complete", "records": 1}},
    }


def test_synthetic_source_to_claim_assessment_verification_and_report_bundle(tmp_path):
    run_dir = tmp_path / "private-synthetic-run"
    watch_dir = run_dir / "source-watch" / "synthetic-snapshot"
    watch_dir.mkdir(parents=True)
    shutil.copyfile(FIXTURE_DIR / "capture.html", watch_dir / "capture.html")
    shutil.copyfile(FIXTURE_DIR / "manifest.json", watch_dir / "manifest.json")
    manifest_path = watch_dir / "manifest.json"

    ledger_path = tmp_path / "private-ledger.sqlite"
    ledger = EvidenceLedger(ledger_path)
    try:
        bridge_receipt = ingest_source_watch_manifest(manifest_path, ledger, root=run_dir)
        assert bridge_receipt["items"][0]["status"] == "ingested"
        records = ledger.records()
        assert [row["record_type"] for row in records] == ["Source", "Artifact", "Passage"]
        source_record, artifact_record, passage_record = records
        assert artifact_record["source_id"] == source_record["record_id"]
        assert passage_record["artifact_id"] == artifact_record["record_id"]
        passages = _passages_from_ledger(records)
        assert len(passages) == 1
        passage = passages[0]
        assert passage["text"] == SYNTHETIC_STATEMENT
        assert passage["source_uri"] == "https://synthetic.invalid/source-watch-e2e"
        assert passage["artifact_sha256"] == hashlib.sha256((watch_dir / "capture.html").read_bytes()).hexdigest()
        assert passage["locator"]["kind"] == "source_watch_filtered_article_text"
        assert passage["locator"]["manifest_path"] == "source-watch/synthetic-snapshot/manifest.json"
        assert passage["locator"]["body_path"] == "source-watch/synthetic-snapshot/capture.html"
        assert passage["artifact_start_offset"] is None

        claim_id = "claim:synthetic-e2e-fixture"
        claim = {
            "claim_id": claim_id,
            "statement": SYNTHETIC_STATEMENT,
            "scope": SYNTHETIC_SCOPE,
        }
        ledger.add_claim(
            claim_id,
            statement=SYNTHETIC_STATEMENT,
            claim_type="research_finding",
            evidence_refs=[passage["passage_id"]],
            producer="synthetic-e2e-test",
            run_id="synthetic-e2e-run",
            limitations=["Synthetic test fixture only; no real-world assertion."],
        )
        retrieval = retrieve_evidence_candidates(claim, passages, as_of_utc=AS_OF_UTC)
        assert retrieval["search_receipt"]["status"] == "candidates_found"
        assert len(retrieval["candidates"]) == 1
        candidate = retrieval["candidates"][0]
        assert candidate["candidate_id"]
        assert candidate["passage_id"] == passage["passage_id"]
        assert candidate["relation"] == "unassessed"
        assert candidate["verification_status"] == "unverified"
        assert candidate["artifact_start_offset"] is None
        assert candidate["artifact_offset_status"] == "unavailable"
        assert candidate["passage_offset_unit"] == "unicode_codepoint"
        assert passage["text"][candidate["passage_start_offset"]:candidate["passage_end_offset"]] == candidate["quote"]
        assert candidate["locator"] == passage["locator"]
        assert candidate["quote"].startswith("SYNTHETIC FIXTURE ONLY:")

        assessment = create_evidence_assessment(
            claim,
            candidate,
            passage,
            relation="mentions",
            decision="accepted",
            scope=SYNTHETIC_SCOPE,
            assessed_at_utc=AS_OF_UTC,
            assessor_id="synthetic-fixture-rule",
            acceptance_basis="validated_rule",
            validated_rule_id="synthetic-fixture-exact-span-v1",
            limitations=["Synthetic citation check only; not a human adjudication or truth finding."],
        )
        assert assessment["decision"] == "accepted"
        assert assessment["assessment_id"]
        assert assessment["candidate_id"] == candidate["candidate_id"]
        assert assessment["assessment_source"] == "validated_rule"
        assert assessment["validated_rule_id"] == "synthetic-fixture-exact-span-v1"
        assert assessment["relation"] == "mentions"
        assert assessment["citation"]["quote"] == candidate["quote"]
        assert assessment["citation"]["passage_id"] == passage["passage_id"]
        assert candidate["verification_status"] == "unverified"  # Retrieval output stays candidate-only.

        verification = aggregate_verification(
            claim,
            [assessment],
            as_of_utc=AS_OF_UTC,
            search_complete=True,
        )
        assert verification["status"] == "insufficient_evidence"
        assert verification["result_id"]
        assert verification["claim_id"] == claim_id
        assert verification["supporting_evidence_ids"] == []
        assert verification["contradicting_evidence_ids"] == []
        assert verification["assessment_ids"] == []
        assert verification["excluded_assessments"] == [{
            "assessment_id": assessment["assessment_id"],
            "reason": "non_verdict_relation",
        }]
        ledger_verification = append_verification_result(
            ledger,
            verification,
            producer="synthetic-e2e-test",
            run_id="synthetic-e2e-run",
        )
        assert ledger_verification["record_type"] == "VerificationResult"
        assert ledger_verification["record_id"] == verification["result_id"]

        evidence_record = _report_evidence(passage, candidate)
        findings = [
            {
                "finding_id": "finding:synthetic-e2e-source-quote",
                "kind": "source_attributed",
                "claim_id": claim_id,
                "statement": candidate["quote"],
                "scope": SYNTHETIC_SCOPE,
                "evidence_refs": [{
                    "evidence_id": passage["passage_id"],
                    "role": "source_attributed",
                }],
            },
            {
                "finding_id": "finding:synthetic-e2e-unresolved",
                "kind": "unresolved",
                "claim_id": claim_id,
                "statement": SYNTHETIC_STATEMENT,
                "scope": SYNTHETIC_SCOPE,
                "verification_result_id": verification["result_id"],
                "reason_code": "insufficient_evidence",
                "evidence_refs": [],
            },
        ]
        schema = json.loads((Path(__file__).parents[1] / "schemas" / "analytical_finding.schema.json").read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
        for finding in findings:
            validator.validate(finding)

        report_dir = tmp_path / "private-analysis" / "report-bundle"
        report = build_report_bundle(
            findings,
            [evidence_record],
            [verification],
            _report_context(),
            report_dir,
        )
        assert report["quality"]["validated_finding_count"] == 2
        assert report["quality"]["citation_count"] == 1
        assert report["quality"]["assertion_check_status"] == "not_evaluated_semantic"
        assert report["receipt"]["incident_id"].startswith("synthetic-")

        rendered = (report_dir / "incident_report.md").read_text(encoding="utf-8")
        assert "SYNTHETIC TEST ONLY" in rendered
        assert candidate["quote"] in rendered
        findings_rows = [json.loads(line) for line in (report_dir / "findings.jsonl").read_text(encoding="utf-8").splitlines()]
        attributed = next(row for row in findings_rows if row["finding_id"] == findings[0]["finding_id"])
        citation = attributed["citations"][0]
        assert citation["evidence_id"] == passage["passage_id"]
        assert citation["locator"]["quote"] == candidate["quote"]
        assert citation["locator"]["start_offset"] == candidate["passage_start_offset"]
        assert citation["locator"]["end_offset"] == candidate["passage_end_offset"]
        assert citation["locator"]["offset_unit"] == "unicode_codepoint"

        receipt = json.loads((report_dir / "run_receipt.json").read_text(encoding="utf-8"))
        for relative_name, expected_hash in receipt["output_sha256"].items():
            actual_hash = hashlib.sha256((report_dir / relative_name).read_bytes()).hexdigest()
            assert actual_hash == expected_hash
        validation = verify_report_bundle(report_dir)
        assert validation["status"] == "passed"
        assert validation["verified_files"] == len(receipt["output_sha256"])

        bad_citation = json.loads(json.dumps(findings[0]))
        bad_citation["evidence_refs"][0]["evidence_id"] = "missing:synthetic-evidence-id"
        with pytest.raises(ReportValidationError, match="unknown evidence_id"):
            build_report_bundle(
                [bad_citation], [evidence_record], [], _report_context(),
                tmp_path / "private-analysis" / "bad-missing-citation",
            )

        missing_finding_id = json.loads(json.dumps(findings[0]))
        missing_finding_id.pop("finding_id")
        with pytest.raises(ReportValidationError, match="requires finding_id"):
            build_report_bundle(
                [missing_finding_id], [evidence_record], [], _report_context(),
                tmp_path / "private-analysis" / "bad-missing-finding-id",
            )

        missing_verification_id = json.loads(json.dumps(findings[1]))
        missing_verification_id["verification_result_id"] = "missing:synthetic-verification-id"
        with pytest.raises(ReportValidationError, match="matching bounded VerificationResult"):
            build_report_bundle(
                [missing_verification_id], [evidence_record], [verification], _report_context(),
                tmp_path / "private-analysis" / "bad-missing-verification-id",
            )
    finally:
        ledger.close()
