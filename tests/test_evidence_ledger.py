import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.evidence_ledger import EvidenceLedger, LedgerIntegrityError, LedgerValidationError

TEST_PROVENANCE = {
    "producer": "unit-test", "run_id": "test-run-001", "limitations": [],
}


class EvidenceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ledger = EvidenceLedger(Path(self.temp.name) / "evidence.sqlite")
        self.addCleanup(self.ledger.close)

    def make_evidence(self):
        self.ledger.add_source(
            "source-1", uri="https://example.org/report", title="Public report",
            created_at="2026-11-07T08:00:00+02:00",
        )
        artifact_provenance = {
            **TEST_PROVENANCE,
            "limitations": ["capture covers the accessible page only"],
        }
        self.ledger.add_artifact(
            "artifact-1", source_id="source-1", path="raw/web/report.html.gz",
            sha256="a" * 64, media_type="text/html",
            captured_at="2026-11-07T06:01:00Z",
            **artifact_provenance,
        )
        self.ledger.add_passage(
            "passage-1", artifact_id="artifact-1", text="The event occurred at noon.",
            locator={"page": 2, "paragraph": 4},
            **TEST_PROVENANCE,
        )

    def test_records_are_versioned_typed_and_timestamps_are_utc(self):
        source = self.ledger.add_source(
            "source-1", uri="https://example.org/report", title="Public report",
            created_at="2026-11-07T08:00:00+02:00",
        )

        self.assertEqual(source["record_type"], "Source")
        self.assertEqual(source["schema_version"], 1)
        self.assertEqual(source["created_at"], "2026-11-07T06:00:00+00:00")

    def test_finding_requires_existing_passage_or_artifact_evidence(self):
        with self.assertRaises(LedgerValidationError):
            self.ledger.add_claim(
                "finding-1", statement="A research conclusion", claim_type="research_finding",
                evidence_refs=[],
                **TEST_PROVENANCE,
            )

        with self.assertRaises(LedgerValidationError):
            self.ledger.add_claim(
                "finding-1", statement="A research conclusion", claim_type="research_finding",
                evidence_refs=["missing-passage"],
                **TEST_PROVENANCE,
            )

        self.make_evidence()
        finding = self.ledger.add_claim(
            "finding-1", statement="The report places the event at noon.",
            claim_type="research_finding", evidence_refs=["passage-1"],
            **TEST_PROVENANCE,
        )
        published_claim = self.ledger.add_claim(
            "claim-1", statement="The event occurred at noon.",
            claim_type="published_claim", source_id="source-1",
        )

        self.assertEqual(finding["claim_type"], "research_finding")
        self.assertEqual(finding["evidence_refs"], ["passage-1"])
        self.assertEqual(published_claim["claim_type"], "published_claim")
        self.assertEqual(published_claim["source_id"], "source-1")

    def test_published_claim_mentions_must_match_the_claim_source(self):
        self.make_evidence()
        self.ledger.add_source("source-2", uri="https://elsewhere.example/", title="Other")
        self.ledger.add_claim(
            "claim-1", statement="A source statement", claim_type="published_claim",
            source_id="source-2",
        )

        with self.assertRaises(LedgerValidationError):
            self.ledger.add_claim_mention("mention-1", claim_id="claim-1", passage_id="passage-1",
                                          **TEST_PROVENANCE)

    def test_assessments_use_plan_statuses_and_append_superseding_versions(self):
        self.make_evidence()
        self.ledger.add_claim(
            "finding-1", statement="The report places the event at noon.",
            claim_type="research_finding", evidence_refs=["passage-1"],
            **TEST_PROVENANCE,
        )
        first = self.ledger.add_assessment(
            "assessment-1", claim_id="finding-1", status="insufficient_evidence",
            evidence_refs=["passage-1"], rationale="One passage only.",
            **TEST_PROVENANCE,
        )
        second = self.ledger.add_assessment(
            "assessment-2", claim_id="finding-1", status="supported_within_scope",
            evidence_refs=["passage-1"], supersedes="assessment-1",
            **TEST_PROVENANCE,
        )

        self.assertEqual(first["status"], "insufficient_evidence")
        self.assertEqual(second["status"], "supported_within_scope")
        self.assertEqual(second["supersedes"], "assessment-1")
        self.assertEqual([r["record_id"] for r in self.ledger.assessments_for("finding-1")],
                         ["assessment-1", "assessment-2"])
        with self.assertRaises(LedgerValidationError):
            self.ledger.add_assessment(
                "assessment-3", claim_id="finding-1", status="confirmed",
                evidence_refs=["passage-1"],
                **TEST_PROVENANCE,
            )

    def test_assessments_validate_references_and_supersedes_target(self):
        self.make_evidence()
        self.ledger.add_claim(
            "finding-1", statement="A finding", claim_type="research_finding",
            evidence_refs=["passage-1"],
            **TEST_PROVENANCE,
        )
        self.ledger.add_claim(
            "finding-2", statement="Another finding", claim_type="research_finding",
            evidence_refs=["passage-1"],
            **TEST_PROVENANCE,
        )
        self.ledger.add_assessment(
            "assessment-1", claim_id="finding-1", status="unverified",
            evidence_refs=["passage-1"],
            **TEST_PROVENANCE,
        )

        with self.assertRaises(LedgerValidationError):
            self.ledger.add_assessment(
                "assessment-2", claim_id="finding-1", status="supported_within_scope",
                evidence_refs=["missing"],
                **TEST_PROVENANCE,
            )
        with self.assertRaises(LedgerValidationError):
            self.ledger.add_assessment(
                "assessment-2", claim_id="finding-2", status="supported_within_scope",
                evidence_refs=["passage-1"], supersedes="assessment-1",
                **TEST_PROVENANCE,
            )

    def test_every_plan_verification_status_is_accepted(self):
        self.make_evidence()
        self.ledger.add_claim(
            "finding-1", statement="A finding", claim_type="research_finding",
            evidence_refs=["passage-1"],
            **TEST_PROVENANCE,
        )
        statuses = [
            "unverified", "supported_within_scope", "refuted_within_scope",
            "conflicting_evidence", "insufficient_evidence", "not_verifiable", "superseded",
        ]
        for index, status in enumerate(statuses):
            refs = [] if status in {"not_verifiable", "superseded"} else ["passage-1"]
            result = self.ledger.add_assessment(
                f"assessment-{index}", claim_id="finding-1", status=status, evidence_refs=refs,
                **TEST_PROVENANCE,
            )
            self.assertEqual(result["status"], status)

    def test_unverified_published_claim_can_be_assessed_before_evidence_is_available(self):
        self.ledger.add_source("source-1", uri="https://example.org/", title="Report")
        self.ledger.add_claim(
            "claim-1", statement="An attributed statement", claim_type="published_claim",
            source_id="source-1",
        )

        assessment = self.ledger.add_assessment(
            "assessment-1", claim_id="claim-1", status="unverified",
            **TEST_PROVENANCE,
        )

        self.assertEqual(assessment["status"], "unverified")
        self.assertEqual(assessment["evidence_refs"], [])
        with self.assertRaises(LedgerValidationError):
            self.ledger.add_assessment(
                "assessment-2", claim_id="claim-1", status="supported_within_scope",
                **TEST_PROVENANCE,
            )

    def test_verification_result_records_scope_and_validates_separate_evidence_links(self):
        self.make_evidence()
        self.ledger.add_passage(
            "passage-2", artifact_id="artifact-1", text="A second account disputes the time.",
            locator={"page": 3}, **TEST_PROVENANCE,
        )
        self.ledger.add_claim(
            "finding-1", statement="The time of the event is disputed.",
            claim_type="research_finding", evidence_refs=["passage-1", "passage-2"],
            **TEST_PROVENANCE,
        )
        result = self.ledger.add_verification_result(
            "verification-1", claim_id="finding-1", scope="The archived report only",
            as_of="2026-11-07T10:00:00+02:00", status="conflicting_evidence",
            supporting_evidence_ids=["passage-1"], contradicting_evidence_ids=["passage-2"],
            **TEST_PROVENANCE,
        )

        self.assertEqual(result["record_type"], "VerificationResult")
        self.assertEqual(result["scope"], "The archived report only")
        self.assertEqual(result["as_of"], "2026-11-07T08:00:00+00:00")
        self.assertEqual(result["supporting_evidence_ids"], ["passage-1"])
        self.assertEqual(result["contradicting_evidence_ids"], ["passage-2"])
        self.assertEqual(result["producer"], "unit-test")
        self.assertEqual(result["run_id"], "test-run-001")
        self.assertEqual(result["limitations"], [])

        with self.assertRaises(LedgerValidationError):
            self.ledger.add_verification_result(
                "verification-bad", claim_id="finding-1", scope="The archived report only",
                as_of="2026-11-07T08:00:00+00:00", status="supported_within_scope",
                supporting_evidence_ids=["missing-passage"], contradicting_evidence_ids=[],
                **TEST_PROVENANCE,
            )
        with self.assertRaises(LedgerValidationError):
            self.ledger.add_verification_result(
                "verification-overlap", claim_id="finding-1", scope="The archived report only",
                as_of="2026-11-07T08:00:00+00:00", status="conflicting_evidence",
                supporting_evidence_ids=["passage-1"], contradicting_evidence_ids=["passage-1"],
                **TEST_PROVENANCE,
            )
        with self.assertRaises(LedgerValidationError):
            self.ledger.add_verification_result(
                "verification-no-support", claim_id="finding-1", scope="The archived report only",
                as_of="2026-11-07T08:00:00+00:00", status="supported_within_scope",
                supporting_evidence_ids=[], contradicting_evidence_ids=[], **TEST_PROVENANCE,
            )

        exported = self.ledger.to_jsonl()
        replica = EvidenceLedger(Path(self.temp.name) / "verification-replica.sqlite")
        self.addCleanup(replica.close)
        self.assertEqual(replica.import_jsonl(exported), 6)
        self.assertEqual(replica.get_record("verification-1"), result)

    def test_research_findings_require_provenance(self):
        self.make_evidence()
        with self.assertRaises(LedgerValidationError):
            self.ledger.add_claim(
                "finding-no-provenance", statement="A finding",
                claim_type="research_finding", evidence_refs=["passage-1"],
            )

    def test_derived_artifact_requires_provenance_but_source_does_not(self):
        self.ledger.add_source("source-1", uri="https://example.org/", title="Report")
        with self.assertRaises(LedgerValidationError):
            self.ledger.add_artifact(
                "artifact-no-provenance", source_id="source-1", path="raw/report.html",
                sha256="e" * 64, media_type="text/html", captured_at="2026-11-07T00:00:00Z",
            )

    def test_identical_jsonl_replay_of_supersession_chain_is_idempotent(self):
        self.make_evidence()
        self.ledger.add_claim(
            "finding-1", statement="A finding", claim_type="research_finding",
            evidence_refs=["passage-1"], **TEST_PROVENANCE,
        )
        self.ledger.add_assessment(
            "assessment-1", claim_id="finding-1", status="unverified",
            evidence_refs=["passage-1"], **TEST_PROVENANCE,
        )
        self.ledger.add_assessment(
            "assessment-2", claim_id="finding-1", status="supported_within_scope",
            evidence_refs=["passage-1"], supersedes="assessment-1", **TEST_PROVENANCE,
        )
        exported = self.ledger.to_jsonl()
        replica = EvidenceLedger(Path(self.temp.name) / "replay.sqlite")
        self.addCleanup(replica.close)

        self.assertEqual(replica.import_jsonl(exported), 6)
        self.assertEqual(replica.import_jsonl(exported), 0)
        self.assertEqual(replica.to_jsonl(), exported)
        self.assertEqual(replica.get_record("artifact-1")["limitations"],
                         ["capture covers the accessible page only"])
        with self.assertRaises(LedgerValidationError):
            replica.add_assessment(
                "assessment-3", claim_id="finding-1", status="supported_within_scope",
                evidence_refs=["passage-1"], supersedes="assessment-1", **TEST_PROVENANCE,
            )

    def test_jsonl_rejects_null_required_timestamps(self):
        self.ledger.add_source("source-1", uri="https://example.org/", title="Report")
        source_with_null_created_at = {
            "record_type": "Source", "schema_version": 1, "record_id": "source-null-time",
            "created_at": None, "uri": "https://example.net/", "title": "Null time",
        }
        artifact_with_null_capture_time = {
            "record_type": "Artifact", "schema_version": 1, "record_id": "artifact-null-time",
            "created_at": "2026-11-07T00:00:00+00:00", "source_id": "source-1",
            "path": "raw/null.html", "sha256": "d" * 64, "media_type": "text/html",
            "captured_at": None, **TEST_PROVENANCE,
        }

        with self.assertRaisesRegex(LedgerValidationError, "created_at"):
            self.ledger.import_jsonl(json.dumps(source_with_null_created_at) + "\n")
        with self.assertRaisesRegex(LedgerValidationError, "captured_at"):
            self.ledger.import_jsonl(json.dumps(artifact_with_null_capture_time) + "\n")

    def test_naive_timestamps_are_rejected(self):
        with self.assertRaises(LedgerValidationError):
            self.ledger.add_source(
                "source-1", uri="https://example.org/report", title="Public report",
                created_at=datetime(2026, 11, 7, 8, 0, 0),
            )

    def test_artifact_hash_and_path_cannot_be_replaced(self):
        self.make_evidence()
        with self.assertRaises(LedgerIntegrityError):
            self.ledger.add_artifact(
                "artifact-1", source_id="source-1", path="raw/web/replaced.html.gz",
                sha256="b" * 64, media_type="text/html",
                captured_at="2026-11-07T06:01:00Z",
                **TEST_PROVENANCE,
            )
        self.assertEqual(self.ledger.get_record("artifact-1")["path"], "raw/web/report.html.gz")
        self.assertEqual(self.ledger.get_record("artifact-1")["sha256"], "a" * 64)

    def test_jsonl_round_trip_preserves_records_and_is_atomic_on_bad_input(self):
        self.make_evidence()
        self.ledger.add_claim(
            "finding-1", statement="The report places the event at noon.",
            claim_type="research_finding", evidence_refs=["passage-1"],
            **TEST_PROVENANCE,
        )
        exported = self.ledger.to_jsonl()
        replica = EvidenceLedger(Path(self.temp.name) / "replica.sqlite")
        self.addCleanup(replica.close)
        self.assertEqual(replica.import_jsonl(exported), 4)
        self.assertEqual(replica.to_jsonl(), exported)

        before = replica.to_jsonl()
        bad_records = [
            json.dumps({
                "record_type": "Source", "schema_version": 1, "record_id": "source-2",
                "created_at": "2026-11-07T00:00:00+00:00", "uri": "https://example.net/",
                "title": "Import candidate",
            }),
            json.dumps({
                "record_type": "Artifact", "schema_version": 1, "record_id": "artifact-bad",
                "created_at": "2026-11-07T00:00:00+00:00", "source_id": "missing-source",
                "path": "raw/bad", "sha256": "c" * 64, "media_type": "text/html",
                "captured_at": "2026-11-07T00:00:00+00:00",
                **TEST_PROVENANCE,
            }),
        ]
        with self.assertRaises(LedgerValidationError):
            replica.import_jsonl("\n".join(bad_records) + "\n")
        self.assertEqual(replica.to_jsonl(), before)

    def test_jsonl_rejects_boolean_schema_version(self):
        record = {
            "record_type": "Source", "schema_version": True, "record_id": "source-1",
            "created_at": "2026-11-07T00:00:00+00:00", "uri": "https://example.org/",
            "title": "Report",
        }
        with self.assertRaises(LedgerValidationError):
            self.ledger.import_jsonl(json.dumps(record) + "\n")


if __name__ == "__main__":
    unittest.main()
