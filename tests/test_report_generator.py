from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, ValidationError as JSONSchemaValidationError

from src.analysis import report_generator
from src.analysis.report_generator import ReportValidationError, build_report_bundle


def context(**overrides):
    result = {
        "incident_id": "case-demo",
        "run_id": "run-demo",
        "as_of_utc": "2026-11-07T00:00:00Z",
        "working_title": "Демонстраційний інцидент",
        "git_commit": "abcdef1234567",
        "scope": {"description": "Публічні матеріали у визначеному часовому вікні", "time_window": ["2026-11-02", "2026-11-05"]},
        "coverage": {"telegram": {"status": "partial", "records": 12}},
    }
    result.update(overrides)
    return result


def evidence(evidence_id="e1", text="Поліція оприлюднила заяву 2 листопада.", *, locator=None):
    return {
        "evidence_id": evidence_id,
        "source_id": "source-1",
        "source_title": "Публічна заява поліції",
        "source_uri": "https://example.org/statement",
        "artifact_id": "artifact-1",
        "artifact_sha256": "a" * 64,
        "captured_at_utc": "2026-11-03T00:00:00Z",
        "text": text,
        "locator": locator or {"kind": "text_span", "start_offset": 0, "end_offset": len(text), "offset_unit": "unicode_codepoint"},
    }


def verification(status="supported_within_scope", *, support=("e1",), contradict=(), scope=None):
    return {
        "verification_result_id": "vr-1",
        "claim_id": "claim-1",
        "status": status,
        "scope": scope or context()["scope"],
        "as_of_utc": "2026-11-06T00:00:00Z",
        "supporting_evidence_ids": list(support),
        "contradicting_evidence_ids": list(contradict),
        "supporting_root_count": len(support),
        "contradicting_root_count": len(contradict),
        "minimum_independent_roots": 1,
        "rationale_code": "explicit_assessment_aggregate",
        "limitations": ["Лише в межах переданого корпусу."],
    }


def finding(kind="supported_within_scope", *, statement="Поліція оприлюднила заяву 2 листопада.", refs=None, **overrides):
    result = {
        "finding_id": "finding-1",
        "kind": kind,
        "claim_id": "claim-1",
        "statement": statement,
        "scope": context()["scope"],
        "verification_result_id": "vr-1" if kind in {"supported_within_scope", "refuted_within_scope", "conflicting"} else None,
        "evidence_refs": refs if refs is not None else [{"evidence_id": "e1", "role": "supporting"}],
        "alternatives": [],
        "limitations": [],
    }
    result.update(overrides)
    return result


def build(tmp_path: Path, findings, evidence_records=None, results=None):
    return build_report_bundle(
        findings,
        evidence_records if evidence_records is not None else [evidence()],
        results if results is not None else [verification()],
        context(),
        tmp_path / "report",
    )


def test_supported_finding_renders_only_with_matching_bounded_verification_and_exact_citation(tmp_path):
    result = build(tmp_path, [finding()])
    markdown = (tmp_path / "report" / "incident_report.md").read_text(encoding="utf-8")
    assert "у межах" in markdown.lower()
    assert "source-1" in markdown
    assert "роль: supporting" in markdown
    assert "artifact-1" in markdown
    assert "a" * 64 in markdown
    assert "\\[e1\\]" in markdown
    assert result["quality"]["unsupported_assertion_count"] is None
    assert result["quality"]["assertion_check_status"] == "not_evaluated_semantic"


def test_report_rejects_evidence_captured_after_verification_cutoff(tmp_path):
    late = evidence()
    late["captured_at_utc"] = "2026-11-06T00:00:00Z"
    old_result = verification()
    old_result["as_of_utc"] = "2026-11-03T00:00:00Z"

    with pytest.raises(ReportValidationError, match="newer than verification as_of_utc"):
        build(tmp_path, [finding()], evidence_records=[late], results=[old_result])


def test_dangling_evidence_reference_blocks_report(tmp_path):
    with pytest.raises(ReportValidationError, match="unknown evidence_id"):
        build(tmp_path, [finding(refs=[{"evidence_id": "missing", "role": "supporting"}])])


def test_missing_or_inexact_locator_blocks_report(tmp_path):
    broken = evidence(locator={"kind": "text_span", "start_offset": 0, "end_offset": 999, "offset_unit": "unicode_codepoint"})
    with pytest.raises(ReportValidationError, match="locator"):
        build(tmp_path, [finding()], evidence_records=[broken])


def test_unverified_result_cannot_render_as_supported(tmp_path):
    with pytest.raises(ReportValidationError, match="supported_within_scope"):
        build(tmp_path, [finding()], results=[verification("insufficient_evidence")])


def test_supported_result_requires_all_matching_evidence_ids_to_be_cited(tmp_path):
    result = verification("supported_within_scope", support=("e1", "e2"))
    with pytest.raises(ReportValidationError, match="exactly match"):
        build(tmp_path, [finding()], evidence_records=[evidence(), evidence("e2")], results=[result])


def test_numbers_in_supported_proposition_must_appear_in_cited_evidence(tmp_path):
    with pytest.raises(ReportValidationError, match="numeric token"):
        build(tmp_path, [finding(statement="Підтверджено 40 людей у приміщенні.")])


def test_number_outside_exact_cited_locator_does_not_support_proposition(tmp_path):
    text = "У повідомленні йдеться про 40 людей. Окрема фраза без числа."
    quote = "Окрема фраза без числа."
    start = text.index(quote)
    record = evidence(text=text, locator={
        "kind": "text_span", "start_offset": start, "end_offset": start + len(quote),
        "offset_unit": "unicode_codepoint",
    })
    with pytest.raises(ReportValidationError, match="numeric token"):
        build(tmp_path, [finding(statement="Підтверджено 40 людей у приміщенні.")], evidence_records=[record])


def test_conflicting_finding_displays_both_sides_and_does_not_choose_a_winner(tmp_path):
    positive = evidence("e1", "У повідомленні сказано: людей утримували у приміщенні.")
    negative = evidence("e2", "Уточнення: людей у приміщенні не утримували.")
    result = verification("conflicting_evidence", support=("e1",), contradict=("e2",))
    item = finding("conflicting", statement="Людей утримували у приміщенні.", refs=[
        {"evidence_id": "e1", "role": "supporting"},
        {"evidence_id": "e2", "role": "contradicting"},
    ])
    build(tmp_path, [item], evidence_records=[positive, negative], results=[result])
    markdown = (tmp_path / "report" / "incident_report.md").read_text(encoding="utf-8")
    assert "суперечливі" in markdown.lower() or "суперечать" in markdown.lower()
    assert "\\[e1\\]" in markdown and "\\[e2\\]" in markdown
    assert "роль: supporting" in markdown and "роль: contradicting" in markdown
    assert "перемог" not in markdown.lower()


def test_refuted_finding_needs_matching_bounded_verification_and_contradicting_evidence(tmp_path):
    negative = evidence("e2", "Уточнення: людей у приміщенні не утримували.")
    result = verification("refuted_within_scope", support=(), contradict=("e2",))
    item = finding("refuted_within_scope", statement="Людей утримували у приміщенні.", refs=[
        {"evidence_id": "e2", "role": "contradicting"},
    ])
    build(tmp_path, [item], evidence_records=[negative], results=[result])
    markdown = (tmp_path / "report" / "incident_report.md").read_text(encoding="utf-8").lower()
    assert "у визначених межах доказів" in markdown
    assert "\\[e2\\]" in markdown


def test_source_attributed_statement_must_be_an_exact_quote(tmp_path):
    item = finding("source_attributed", statement="Поліція заявила, що постраждалих не було.", refs=[
        {"evidence_id": "e1", "role": "source_attributed"},
    ])
    with pytest.raises(ReportValidationError, match="exact .* quote"):
        build(tmp_path, [item])


def test_source_attribution_requires_each_named_source_to_have_exact_quote(tmp_path):
    item = finding("source_attributed", statement="Поліція оприлюднила заяву 2 листопада.", refs=[
        {"evidence_id": "e1", "role": "source_attributed"},
        {"evidence_id": "e2", "role": "source_attributed"},
    ])
    other = evidence("e2", "В іншому документі цитати немає.")
    with pytest.raises(ReportValidationError, match="every exact source-attributed quote"):
        build(tmp_path, [item], evidence_records=[evidence(), other])


def test_source_attributed_quote_is_rendered_as_source_statement(tmp_path):
    item = finding("source_attributed", statement="Поліція оприлюднила заяву 2 листопада.", refs=[
        {"evidence_id": "e1", "role": "source_attributed"},
    ])
    build(tmp_path, [item])
    markdown = (tmp_path / "report" / "incident_report.md").read_text(encoding="utf-8")
    assert "Джерело source-1 повідомляє" in markdown
    assert "«Поліція оприлюднила заяву 2 листопада.»" in markdown


def test_source_attributed_finding_can_preserve_exact_same_source_attribution_context(tmp_path):
    full_text = (
        "Командувач Національної гвардії України Олександр Півненко пояснив, що будівля була орендована. "
        "Загалом у приміщенні перебували 26 осіб."
    )
    context_quote = "Командувач Національної гвардії України Олександр Півненко пояснив"
    statement = "Загалом у приміщенні перебували 26 осіб."
    context_record = evidence("e-context", full_text, locator={
        "kind": "text_span", "start_offset": 0, "end_offset": len(context_quote),
        "offset_unit": "unicode_codepoint",
    })
    statement_start = full_text.index(statement)
    statement_record = evidence("e-statement", full_text, locator={
        "kind": "text_span", "start_offset": statement_start,
        "end_offset": statement_start + len(statement), "offset_unit": "unicode_codepoint",
    })
    item = finding("source_attributed", statement=statement, refs=[
        {"evidence_id": "e-statement", "role": "source_attributed"},
        {"evidence_id": "e-context", "role": "context"},
    ])

    build(tmp_path, [item], evidence_records=[context_record, statement_record], results=[])
    markdown = (tmp_path / "report" / "incident_report.md").read_text(encoding="utf-8")

    assert "Контекст у джерелі: «Командувач Національної гвардії України Олександр Півненко пояснив»" in markdown
    assert "Джерело source-1 повідомляє: «Загалом у приміщенні перебували 26 осіб.»" in markdown
    assert "роль: context" in markdown


def test_source_attributed_context_must_come_from_the_cited_source(tmp_path):
    quote = "Загалом у приміщенні перебували 26 осіб."
    context_text = "Командувач Олександр Півненко пояснив"
    context_record = evidence("e-context", context_text)
    quote_record = evidence("e-statement", quote)
    context_record["source_id"] = "different-source"
    item = finding("source_attributed", statement=quote, refs=[
        {"evidence_id": "e-statement", "role": "source_attributed"},
        {"evidence_id": "e-context", "role": "context"},
    ])

    with pytest.raises(ReportValidationError, match="context must come from the same source"):
        build(tmp_path, [item], evidence_records=[context_record, quote_record], results=[])


def test_non_verification_finding_cannot_emit_a_dangling_verification_id(tmp_path):
    item = finding("source_attributed", statement="Поліція оприлюднила заяву 2 листопада.", refs=[
        {"evidence_id": "e1", "role": "source_attributed"},
    ], verification_result_id="missing-vr")
    with pytest.raises(ReportValidationError, match="not valid for kind"):
        build(tmp_path, [item])


def test_documented_fact_is_rendered_as_artifact_content_not_world_truth(tmp_path):
    item = finding("documented_fact", statement="Поліція оприлюднила заяву 2 листопада.", refs=[
        {"evidence_id": "e1", "role": "source_attributed"},
    ])
    build(tmp_path, [item])
    markdown = (tmp_path / "report" / "incident_report.md").read_text(encoding="utf-8")
    assert "зафіксовано фрагмент" in markdown
    assert "\\[e1\\]" in markdown


def test_claim_scope_can_be_narrower_than_report_scope(tmp_path):
    claim_scope = {"time_window": ["2026-11-02T17:00:00Z", "2026-11-02T18:00:00Z"]}
    result = verification(scope=claim_scope)
    item = finding(scope=claim_scope)
    build(tmp_path, [item], results=[result])


def test_unverified_verification_counts_cannot_promote_status(tmp_path):
    bad = verification("supported_within_scope")
    bad["supporting_root_count"] = 0
    with pytest.raises(ReportValidationError, match="inconsistent supported status"):
        build(tmp_path, [finding()], results=[bad])


def test_report_rejects_untyped_free_form_finding_fields(tmp_path):
    item = finding()
    item["analyst_narrative"] = "Довільне формулювання без контракту"
    with pytest.raises(ReportValidationError, match="unsupported free-form fields"):
        build(tmp_path, [item])


def test_unresolved_only_report_says_unknown_not_false(tmp_path):
    unresolved = finding(
        "unresolved",
        statement="Твердження про утримання 40 людей",
        refs=[],
        verification_result_id="vr-1",
        reason_code="insufficient_evidence",
    )
    build(tmp_path, [unresolved], results=[verification("insufficient_evidence", support=(), contradict=())])
    markdown = (tmp_path / "report" / "incident_report.md").read_text(encoding="utf-8").lower()
    assert "не встановлено" in markdown
    assert "не є спростуванням" in markdown
    assert "неправда" not in markdown


def test_unresolved_report_requires_matching_verification_and_displays_partial_evidence(tmp_path):
    item = finding(
        "unresolved",
        statement="У приміщенні перебували люди",
        refs=[{"evidence_id": "e1", "role": "supporting"}],
        verification_result_id="vr-1",
        reason_code="insufficient_evidence",
    )
    result = verification("insufficient_evidence", support=("e1",), contradict=())
    build(tmp_path, [item], results=[result])
    markdown = (tmp_path / "report" / "incident_report.md").read_text(encoding="utf-8")
    assert "\\[e1\\]" in markdown
    item["verification_result_id"] = None
    with pytest.raises(ReportValidationError, match="matching bounded VerificationResult"):
        build(tmp_path / "second", [item], results=[result])


def test_unsupported_causal_sentence_is_blocked(tmp_path):
    with pytest.raises(ReportValidationError, match="causal"):
        build(tmp_path, [finding(statement="Публікація спричинила протест 2 листопада.")])


def test_unsupported_temporal_order_is_blocked_without_temporal_relation(tmp_path):
    with pytest.raises(ReportValidationError, match="TemporalRelation"):
        build(tmp_path, [finding(statement="Публікація передувала протесту 2 листопада.")])


def test_multiple_propositions_must_be_split_into_separate_findings(tmp_path):
    with pytest.raises(ReportValidationError, match="one proposition"):
        build(tmp_path, [finding(statement="Поліція приїхала. Люди зібралися.")])


def test_report_package_has_deterministic_hashes_and_refuses_silent_overwrite(tmp_path):
    first = build_report_bundle([finding()], [evidence()], [verification()], context(), tmp_path / "one")
    second = build_report_bundle([finding()], [evidence()], [verification()], context(), tmp_path / "two")
    assert first["receipt"]["input_sha256"] == second["receipt"]["input_sha256"]
    assert first["receipt"]["output_sha256"] == second["receipt"]["output_sha256"]
    with pytest.raises(FileExistsError, match="overwrite"):
        build_report_bundle([finding()], [evidence()], [verification()], context(), tmp_path / "one")


def test_explicit_overwrite_publishes_replacement_bundle(tmp_path):
    output = tmp_path / "overwrite"
    build_report_bundle([finding()], [evidence()], [verification()], context(), output)
    replacement_context = context(working_title="Replacement report")
    build_report_bundle([finding()], [evidence()], [verification()], replacement_context, output, overwrite=True)
    assert "Replacement report" in (output / "incident_report.md").read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".overwrite.staging-*"))


def test_failed_atomic_exchange_preserves_prior_bundle(tmp_path, monkeypatch):
    output = tmp_path / "preserve"
    build_report_bundle([finding()], [evidence()], [verification()], context(), output)
    previous = (output / "incident_report.md").read_bytes()

    def fail_exchange(_left, _right):
        raise OSError("simulated atomic exchange failure")

    monkeypatch.setattr(report_generator, "_rename_exchange", fail_exchange)
    with pytest.raises(OSError, match="simulated atomic exchange failure"):
        build_report_bundle([finding()], [evidence()], [verification()], context(working_title="Next"), output, overwrite=True)
    assert (output / "incident_report.md").read_bytes() == previous
    assert not list(tmp_path.glob(".preserve.staging-*"))


def test_overwrite_flag_must_be_boolean(tmp_path):
    with pytest.raises(ReportValidationError, match="overwrite must be boolean"):
        build_report_bundle([finding()], [evidence()], [verification()], context(), tmp_path / "report", overwrite="false")


def test_output_contains_machine_readable_findings_and_quality_metrics(tmp_path):
    result = build(tmp_path, [finding()])
    finding_rows = [json.loads(line) for line in (tmp_path / "report" / "findings.jsonl").read_text(encoding="utf-8").splitlines()]
    quality = json.loads((tmp_path / "report" / "report_quality.json").read_text(encoding="utf-8"))
    assert finding_rows[0]["finding_id"] == "finding-1"
    citation = finding_rows[0]["citations"][0]
    assert citation["source_id"] == "source-1"
    assert citation["artifact_sha256"] == "a" * 64
    assert citation["locator"]["start_offset"] == 0
    assert quality["validated_finding_count"] == 1
    assert result["receipt"]["run_id"] == "run-demo"


def test_methodological_limit_accepts_generated_citations(tmp_path):
    limit = {
        "finding_id": "limit-coverage",
        "kind": "methodological_limit",
        "evidence_refs": [{"evidence_id": "e1", "role": "context"}],
        "limitation_code": "incomplete_source_coverage",
    }
    build_report_bundle([limit], [evidence()], [], context(), tmp_path / "methodological-limit")

    markdown = (tmp_path / "methodological-limit" / "incident_report.md").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in (tmp_path / "methodological-limit" / "findings.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "Покриття джерел неповне" in markdown
    assert rows[0]["citation_ids"] == ["e1"]


def test_evidence_captured_after_report_cutoff_is_rejected(tmp_path):
    late = evidence()
    late["captured_at_utc"] = "2026-11-08T00:00:00Z"
    with pytest.raises(ReportValidationError, match="newer than report"):
        build(tmp_path, [finding()], evidence_records=[late])


def test_json_embedded_in_markdown_cannot_close_its_fence(tmp_path):
    dangerous_context = context(coverage={"note": "```\\n# untrusted heading"})
    build_report_bundle([finding()], [evidence()], [verification()], dangerous_context, tmp_path / "safe")
    markdown = (tmp_path / "safe" / "incident_report.md").read_text(encoding="utf-8")
    assert r"\u0060\u0060\u0060" in markdown


def test_untrusted_finding_text_cannot_inject_markdown_link(tmp_path):
    item = finding(statement="Цитата [перейти](https://bad.example) та *виділити* 2 листопада.")
    build(tmp_path, [item])
    markdown = (tmp_path / "report" / "incident_report.md").read_text(encoding="utf-8")
    assert "[перейти](https://bad.example)" not in markdown
    assert "\\[перейти\\]" in markdown


def test_analytical_finding_schema_matches_supported_contract():
    schema_path = Path(__file__).parents[1] / "schemas" / "analytical_finding.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema)
    validator.validate(finding())
    malformed = finding()
    malformed["unexpected_narrative"] = "free prose"
    assert list(validator.iter_errors(malformed))
    with pytest.raises(JSONSchemaValidationError):
        validator.validate(finding(refs=[]))
    conflict_missing_side = finding("conflicting", refs=[{"evidence_id": "e1", "role": "supporting"}])
    with pytest.raises(JSONSchemaValidationError):
        validator.validate(conflict_missing_side)
