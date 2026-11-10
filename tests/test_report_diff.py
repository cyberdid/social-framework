import hashlib
import html
import json
from pathlib import Path

import pytest

from src.analysis.report_generator import build_report_bundle
from src.analysis.report_diff import ReportDiffError, compare_report_bundles, main


def render_value(value):
    escaped = "".join("\\" + char if char in "\\`*_{}[]()!|>~" else char for char in value)
    return html.escape(escaped, quote=True)


def make_bundle(root, run_id, as_of, findings):
    root.mkdir(parents=True)
    findings_path = root / "findings.jsonl"
    findings_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in findings),
        encoding="utf-8",
    )
    report_path = root / "incident_report.md"
    report_path.write_text(
        f"# Demo — аналітичний звіт\n\n- Incident ID: {render_value('case_demo_2026')}\n"
        f"- Run ID: {render_value(run_id)}\n- Зріз as_of: {render_value(as_of)}\n",
        encoding="utf-8",
    )
    (root / "run_receipt.json").write_text(json.dumps({
        "incident_id": "case_demo_2026", "run_id": run_id, "as_of_utc": as_of,
        "output_sha256": {
            "findings.jsonl": hashlib.sha256(findings_path.read_bytes()).hexdigest(),
            "incident_report.md": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        },
    }), encoding="utf-8")
    return root


def test_as_of_diff_explains_new_evidence_and_explicit_correction(tmp_path):
    before = make_bundle(tmp_path / "before", "run-then", "2026-11-06T18:00:00Z", [{
        "finding_id": "f1", "kind": "unresolved", "statement": "Травми не встановлені",
        "citation_ids": ["e1"], "verification_result_id": "v1",
    }])
    after = make_bundle(tmp_path / "after", "run-now", "2026-11-07T18:00:00Z", [{
        "finding_id": "f1", "kind": "source_attributed", "statement": "Медик повідомив про травми",
        "citation_ids": ["e1", "e2"], "verification_result_id": "v2",
        "correction_of": "v1", "reason_code": "new_primary_source",
    }, {
        "finding_id": "f2", "kind": "unresolved", "statement": "Час травмування невідомий",
        "citation_ids": [],
    }])

    diff = compare_report_bundles(before, after)
    assert diff["comparison"]["known_then_as_of_utc"] == "2026-11-06T18:00:00Z"
    assert diff["comparison"]["known_now_as_of_utc"] == "2026-11-07T18:00:00Z"
    change = diff["changed_findings"][0]
    assert change["finding_id"] == "f1"
    assert change["change_reasons"] == ["explicit_correction", "evidence_refs_changed", "finding_kind_changed", "statement_changed", "verification_result_changed"]
    assert change["explicit_correction_of"] == "v1"
    assert diff["added_findings"][0]["finding_id"] == "f2"
    assert "does not infer" in diff["limitations"][0].lower()


def test_diff_rejects_symlink_bundle_and_path_traversal_output(tmp_path):
    before = make_bundle(tmp_path / "before", "run-then", "2026-11-06T18:00:00Z", [])
    after = make_bundle(tmp_path / "after", "run-now", "2026-11-07T18:00:00Z", [])
    link = tmp_path / "link"
    link.symlink_to(before, target_is_directory=True)
    with pytest.raises(ReportDiffError, match="symlink"):
        compare_report_bundles(link, after)

    assert main(["--before", str(before), "--after", str(after), "--output-dir", str(tmp_path / ".." / "escape")]) == 2
    assert not (tmp_path.parent / "escape").exists()


def test_diff_rejects_tampered_findings_artifact(tmp_path):
    before = make_bundle(tmp_path / "before", "run-then", "2026-11-06T18:00:00Z", [])
    after = make_bundle(tmp_path / "after", "run-now", "2026-11-07T18:00:00Z", [])
    (after / "findings.jsonl").write_text('{"finding_id":"tampered"}\n', encoding="utf-8")

    with pytest.raises(ReportDiffError, match="SHA-256"):
        compare_report_bundles(before, after)


@pytest.mark.parametrize(("field", "tampered_value"), [
    ("incident_id", "case_other_2026"),
    ("run_id", "run-other"),
    ("as_of_utc", "2026-11-08T18:00:00Z"),
])
def test_diff_rejects_receipt_metadata_mismatch_against_verified_report(tmp_path, field, tampered_value):
    before = make_bundle(tmp_path / "before", "run-then", "2026-11-06T18:00:00Z", [])
    after = make_bundle(tmp_path / "after", "run-now", "2026-11-07T18:00:00Z", [])
    receipt_path = after / "run_receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt[field] = tampered_value
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(ReportDiffError, match="receipt metadata does not match verified incident_report.md"):
        compare_report_bundles(before, after)


def test_diff_parses_exact_findings_bytes_that_were_hash_verified(tmp_path, monkeypatch):
    before = make_bundle(tmp_path / "before", "run-then", "2026-11-06T18:00:00Z", [])
    after = make_bundle(tmp_path / "after", "run-now", "2026-11-07T18:00:00Z", [{
        "finding_id": "verified-row", "kind": "unresolved", "statement": "hashed row", "citation_ids": [],
    }])
    original_read_text = Path.read_text

    def altered_findings_read(path, *args, **kwargs):
        if path.name == "findings.jsonl":
            return json.dumps({"finding_id": "unhashed-row", "kind": "unresolved",
                               "statement": "replacement", "citation_ids": []}) + "\n"
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", altered_findings_read)
    diff = compare_report_bundles(before, after)
    assert [row["finding_id"] for row in diff["added_findings"]] == ["verified-row"]


def test_diff_accepts_receipts_from_report_generator(tmp_path):
    before_context = {"incident_id": "case-demo", "run_id": "run-before",
                      "working_title": "Демонстраційний інцидент", "as_of_utc": "2026-11-06T18:00:00Z",
                      "scope": {"description": "public records"}, "coverage": {}}
    after_context = {**before_context, "run_id": "run-after", "as_of_utc": "2026-11-07T18:00:00Z"}
    build_report_bundle([], [], [], before_context, tmp_path / "generated-before")
    build_report_bundle([], [], [], after_context, tmp_path / "generated-after")

    diff = compare_report_bundles(tmp_path / "generated-before", tmp_path / "generated-after")
    assert diff["comparison"]["known_then_run_id"] == "run-before"
    assert diff["comparison"]["known_now_run_id"] == "run-after"


def test_report_diff_accepts_findings_generated_with_typed_evidence_refs(tmp_path):
    scope = {"description": "synthetic integration control"}

    def build_as_of(root, run_id, as_of, evidence_id, statement):
        record = {
            "evidence_id": evidence_id,
            "source_id": "source-demo",
            "source_uri": "https://example.org/control",
            "artifact_id": f"artifact-{evidence_id}",
            "artifact_sha256": hashlib.sha256(evidence_id.encode()).hexdigest(),
            "captured_at_utc": as_of,
            "text": statement,
            "locator": {"kind": "text_span", "start_offset": 0,
                        "end_offset": len(statement), "offset_unit": "unicode_codepoint"},
        }
        finding = {
            "finding_id": "finding-demo",
            "kind": "source_attributed",
            "claim_id": "claim-demo",
            "statement": statement,
            "scope": scope,
            "evidence_refs": [{"evidence_id": evidence_id, "role": "source_attributed"}],
        }
        context = {
            "incident_id": "case-demo",
            "run_id": run_id,
            "working_title": "Synthetic integration control",
            "as_of_utc": as_of,
            "scope": scope,
            "coverage": {},
        }
        build_report_bundle([finding], [record], [], context, root)

    before = tmp_path / "generated-before"
    after = tmp_path / "generated-after"
    build_as_of(before, "run-before", "2026-11-06T18:00:00Z", "e1", "Initial synthetic statement.")
    build_as_of(after, "run-after", "2026-11-07T18:00:00Z", "e2", "Corrected synthetic statement.")

    diff = compare_report_bundles(before, after)

    assert diff["summary"]["changed"] == 1
    assert diff["changed_findings"][0]["change_reasons"] == ["evidence_refs_changed", "statement_changed"]


def generated_bundle(root, run_id, as_of, *, evidence_id, role, kind="source_attributed"):
    scope = {"description": "synthetic integration control"}
    statement = "Synthetic statement." if kind != "methodological_limit" else None
    record = {
        "evidence_id": evidence_id,
        "source_id": "source-demo",
        "source_uri": "https://example.org/control",
        "artifact_id": f"artifact-{evidence_id}",
        "artifact_sha256": hashlib.sha256(evidence_id.encode()).hexdigest(),
        "captured_at_utc": "2026-11-06T17:00:00Z",
        "text": "Synthetic statement.",
        "locator": {"kind": "text_span", "start_offset": 0, "end_offset": 20,
                    "offset_unit": "unicode_codepoint"},
    }
    finding = {
        "finding_id": "finding-demo",
        "kind": kind,
        "evidence_refs": [
            {"evidence_id": evidence_id, "role": item_role}
            for item_role in (role if isinstance(role, list) else [role])
        ],
    }
    if kind == "methodological_limit":
        finding["limitation_code"] = "incomplete_source_coverage"
    else:
        finding.update({"claim_id": "claim-demo", "statement": statement, "scope": scope})
    context = {
        "incident_id": "case_demo_2026",
        "run_id": run_id,
        "working_title": "Synthetic integration control",
        "as_of_utc": as_of,
        "scope": scope,
        "coverage": {},
    }
    return build_report_bundle([finding], [record], [], context, root)


def test_report_diff_treats_legacy_and_typed_refs_with_same_evidence_as_unchanged(tmp_path):
    scope = {"description": "synthetic integration control"}
    before = make_bundle(tmp_path / "legacy", "run-before", "2026-11-07T18:00:00Z", [{
        "finding_id": "finding-demo", "kind": "source_attributed", "claim_id": "claim-demo",
        "statement": "Synthetic statement.", "scope": scope,
        "verification_result_id": None, "citation_ids": ["e1"],
    }])
    after = tmp_path / "typed"
    generated_bundle(after, "run-after", "2026-11-08T18:00:00Z", evidence_id="e1", role="source_attributed")

    diff = compare_report_bundles(before, after)

    assert diff["summary"] == {"added": 0, "removed": 0, "changed": 0, "unchanged": 1}


def test_report_diff_explains_evidence_role_only_change(tmp_path):
    before = tmp_path / "before-role"
    after = tmp_path / "after-role"
    generated_bundle(before, "run-before", "2026-11-07T18:00:00Z", evidence_id="e1",
                     role="context", kind="methodological_limit")
    generated_bundle(after, "run-after", "2026-11-08T18:00:00Z", evidence_id="e1",
                     role="supporting", kind="methodological_limit")

    diff = compare_report_bundles(before, after)

    assert diff["summary"]["changed"] == 1
    assert diff["changed_findings"][0]["change_reasons"] == ["evidence_roles_changed"]


def test_report_diff_detects_removal_of_one_role_from_shared_evidence(tmp_path):
    before = tmp_path / "before-multiple-roles"
    after = tmp_path / "after-one-role"
    generated_bundle(before, "run-before", "2026-11-07T18:00:00Z", evidence_id="e1",
                     role=["context", "supporting"], kind="methodological_limit")
    generated_bundle(after, "run-after", "2026-11-08T18:00:00Z", evidence_id="e1",
                     role="supporting", kind="methodological_limit")

    diff = compare_report_bundles(before, after)

    assert diff["summary"]["changed"] == 1
    assert diff["changed_findings"][0]["change_reasons"] == ["evidence_roles_changed"]


def test_report_diff_rejects_duplicate_typed_evidence_role(tmp_path):
    before = make_bundle(tmp_path / "before", "run-before", "2026-11-07T18:00:00Z", [])
    after = make_bundle(tmp_path / "after", "run-after", "2026-11-08T18:00:00Z", [{
        "finding_id": "finding-demo", "kind": "source_attributed",
        "evidence_refs": [
            {"evidence_id": "e1", "role": "supporting"},
            {"evidence_id": "e1", "role": "supporting"},
        ],
    }])

    with pytest.raises(ReportDiffError, match="duplicate evidence reference"):
        compare_report_bundles(before, after)


@pytest.mark.parametrize("malformed_refs", [
    {},
    0,
    [{"evidence_id": "e1"}],
    [{"evidence_id": "e1", "role": "unknown"}],
    [{"evidence_id": "e1", "role": "context", "extra": "unexpected"}],
    [" e1"],
])
def test_report_diff_rejects_malformed_evidence_refs_even_when_finding_is_added(tmp_path, malformed_refs):
    before = make_bundle(tmp_path / "before", "run-before", "2026-11-07T18:00:00Z", [])
    after = make_bundle(tmp_path / "after", "run-after", "2026-11-08T18:00:00Z", [{
        "finding_id": "finding-demo", "kind": "source_attributed", "evidence_refs": malformed_refs,
    }])

    with pytest.raises(ReportDiffError, match="evidence reference"):
        compare_report_bundles(before, after)


def test_cli_help_usage_and_output_bundle(tmp_path, capsys):
    assert main(["--help"]) == 0
    assert "--before" in capsys.readouterr().out
    before = make_bundle(tmp_path / "before", "run-then", "2026-11-06T18:00:00Z", [])
    after = make_bundle(tmp_path / "after", "run-now", "2026-11-07T18:00:00Z", [])
    output = tmp_path / "diff"
    assert main(["--before", str(before), "--after", str(after), "--output-dir", str(output)]) == 0
    assert (output / "report_diff.json").is_file()
    assert (output / "report_diff.md").is_file()
    payload = json.loads((output / "report_diff.json").read_text(encoding="utf-8"))
    assert payload["changed_findings"] == []

