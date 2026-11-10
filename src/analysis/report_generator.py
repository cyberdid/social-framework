"""Deterministic, citation-gated Markdown report generation.

This module renders typed findings; it does not generate facts or decide
whether cited material semantically entails a finding.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.analysis.audience_response_reporting import (
    render_audience_response_markdown,
    validate_audience_aggregate,
)


class ReportValidationError(ValueError):
    """A finding, locator, verification result, or report context is invalid."""


FINDING_KINDS = {
    "documented_fact",
    "source_attributed",
    "supported_within_scope",
    "refuted_within_scope",
    "conflicting",
    "hypothesis",
    "unresolved",
    "methodological_limit",
}
FINDING_ORDER = (
    "documented_fact", "source_attributed", "supported_within_scope", "refuted_within_scope",
    "conflicting", "hypothesis", "unresolved", "methodological_limit",
)
EVIDENCE_ROLES = {"supporting", "contradicting", "source_attributed", "context"}
UNRESOLVED_STATUSES = {"unverified", "insufficient_evidence", "not_verifiable"}
VERIFICATION_STATUSES = {
    "unverified", "supported_within_scope", "refuted_within_scope",
    "conflicting_evidence", "insufficient_evidence", "not_verifiable", "superseded",
}
LIMITATION_TEXT = {
    "incomplete_source_coverage": "Покриття джерел неповне; висновки обмежені наданим корпусом.",
    "nonprobability_sample": "Корпус є цільовою вибіркою і не підтримує оцінок для всієї платформи чи населення.",
    "views_not_unique_reach": "Лічильники переглядів є знімками платформи, а не кількістю унікальних людей.",
    "no_causal_design": "Часова послідовність сама по собі не встановлює причинність.",
    "model_observation_candidate": "Модельні спостереження є кандидатами й не підтверджують реальну подію самостійно.",
    "unknown_lineage": "Невиявлений зв'язок між джерелами не означає, що вони незалежні.",
}
ALTERNATIVE_TEXT = {
    "incomplete_source_capture": "Неповне захоплення відкритих матеріалів може змінювати видимий перебіг події.",
    "unknown_source_dependency": "Невиявлена залежність між джерелами залишається можливою.",
    "organic_repetition": "Подібність повідомлень може виникнути через звичайне копіювання спільного першоджерела.",
    "platform_visibility_bias": "Алгоритмічна видимість і доступність платформи можуть змінювати спостережувану картину.",
    "event_time_uncertainty": "Час події може бути неточним або відрізнятися від часу публікації.",
    "identity_uncertainty": "Ідентифікація особи чи об'єкта може бути помилковою.",
}
_NUMERIC_TOKEN = re.compile(r"(?<!\w)[+-]?\d+(?:[.,]\d+)?%?(?!\w)", re.UNICODE)
_CAUSAL_CUE = re.compile(
    r"\b(?:спричинив|спричинила|спричинило|спричинили|викликав|викликала|викликало|"
    r"призвів|призвела|призвело|зумовив|зумовила|призвели|зумовили|спровокував|спровокувала|"
    r"спровокувало|спровокували|caused|causes|led to|resulted in|triggered|provoked|contributed to)\b",
    re.IGNORECASE,
)
_TEMPORAL_ORDER_CUE = re.compile(
    r"\b(?:після|раніше|потім|згодом|передував|передувала|передувало|передували|"
    r"наступного дня|напередодні|сьогодні|вчора|завтра|before|after|earlier|later|"
    r"subsequently|today|yesterday|tomorrow)\b",
    re.IGNORECASE,
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MARKDOWN_ESCAPE = frozenset("\\`*_{}[]()!|>~")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ReportValidationError(f"input is not canonical JSON: {exc}") from exc


def _utc(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReportValidationError(f"{field} must be a timezone-aware UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReportValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReportValidationError(f"{field} must include a timezone")
    if parsed.utcoffset().total_seconds() != 0:
        raise ReportValidationError(f"{field} must be UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _md(value: str) -> str:
    """Escape untrusted strings before placing them in Markdown prose/code."""
    safe_lines = value.replace("\r\n", " ↵ ").replace("\r", " ↵ ").replace("\n", " ↵ ")
    markdown_escaped = "".join("\\" + char if char in _MARKDOWN_ESCAPE else char for char in safe_lines)
    return html.escape(markdown_escaped, quote=True)


def _scope_key(scope: Any) -> str:
    if not isinstance(scope, dict) or not scope:
        raise ReportValidationError("finding/verification scope must be a non-empty object")
    return _canonical(scope)


def _validate_context(context: dict) -> dict:
    if not isinstance(context, dict):
        raise ReportValidationError("report context must be an object")
    for field in ("incident_id", "run_id", "working_title"):
        value = context.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ReportValidationError(f"context.{field} must be a non-empty string")
    normalized = dict(context)
    normalized["as_of_utc"] = _utc(context.get("as_of_utc"), "context.as_of_utc")
    _scope_key(context.get("scope"))
    if not isinstance(context.get("coverage"), dict):
        raise ReportValidationError("context.coverage must be an object")
    git_commit = context.get("git_commit")
    if git_commit is not None and (not isinstance(git_commit, str) or not git_commit):
        raise ReportValidationError("context.git_commit must be a non-empty string or null")
    return normalized


def _make_evidence_index(evidence_records: list[dict], report_as_of_utc: str) -> dict[str, dict]:
    if not isinstance(evidence_records, list):
        raise ReportValidationError("evidence_records must be a list")
    indexed = {}
    report_as_of = datetime.fromisoformat(report_as_of_utc.replace("Z", "+00:00"))
    for record in evidence_records:
        if not isinstance(record, dict):
            raise ReportValidationError("each evidence record must be an object")
        evidence_id = record.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip():
            raise ReportValidationError("each evidence record requires evidence_id")
        evidence_id = evidence_id.strip()
        if evidence_id in indexed:
            raise ReportValidationError(f"duplicate evidence_id: {evidence_id}")
        for field in ("source_id", "source_uri", "artifact_id"):
            value = record.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ReportValidationError(f"{evidence_id}.{field} must be a non-empty string")
        digest = record.get("artifact_sha256")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ReportValidationError(f"{evidence_id}.artifact_sha256 must be lowercase SHA-256")
        record = dict(record)
        record["evidence_id"] = evidence_id
        record["captured_at_utc"] = _utc(record.get("captured_at_utc"), f"{evidence_id}.captured_at_utc")
        captured_at = datetime.fromisoformat(record["captured_at_utc"].replace("Z", "+00:00"))
        if captured_at > report_as_of:
            raise ReportValidationError(f"{evidence_id} is newer than report as_of_utc")
        record["citation"] = _validate_locator(record)
        indexed[evidence_id] = record
    return indexed


def _validate_locator(record: dict) -> dict:
    locator = record.get("locator")
    text = record.get("text")
    if not isinstance(locator, dict):
        raise ReportValidationError(f"{record['evidence_id']} requires an exact locator")
    kind = locator.get("kind")
    if kind == "text_span":
        if not isinstance(text, str):
            raise ReportValidationError(f"{record['evidence_id']} text_span requires full passage text")
        start, end = locator.get("start_offset"), locator.get("end_offset")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start or end > len(text):
            raise ReportValidationError(f"{record['evidence_id']} locator has invalid text offsets")
        if locator.get("offset_unit") != "unicode_codepoint":
            raise ReportValidationError(f"{record['evidence_id']} locator offset_unit must be unicode_codepoint")
        quote = text[start:end]
        if locator.get("quote") is not None and locator["quote"] != quote:
            raise ReportValidationError(f"{record['evidence_id']} locator quote does not match exact source span")
        return {
            "kind": kind,
            "start_offset": start,
            "end_offset": end,
            "offset_unit": "unicode_codepoint",
            "quote": quote,
        }
    if kind == "video_segment":
        start, end = locator.get("start_ms"), locator.get("end_ms")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start:
            raise ReportValidationError(f"{record['evidence_id']} locator has invalid video offsets")
        if locator.get("time_basis") != "original_media_pts":
            raise ReportValidationError(f"{record['evidence_id']} video locator requires original_media_pts")
        return {"kind": kind, "start_ms": start, "end_ms": end, "time_basis": "original_media_pts"}
    raise ReportValidationError(f"{record['evidence_id']} locator kind is unsupported: {kind}")


def _verification_index(results: list[dict], evidence_index: dict[str, dict], context: dict) -> dict[str, dict]:
    if not isinstance(results, list):
        raise ReportValidationError("verification_results must be a list")
    index = {}
    report_as_of = datetime.fromisoformat(context["as_of_utc"].replace("Z", "+00:00"))
    for result in results:
        if not isinstance(result, dict):
            raise ReportValidationError("each verification result must be an object")
        result_id = result.get("verification_result_id") or result.get("result_id")
        if not isinstance(result_id, str) or not result_id:
            raise ReportValidationError("verification result requires verification_result_id")
        if result_id in index:
            raise ReportValidationError(f"duplicate verification_result_id: {result_id}")
        if not isinstance(result.get("status"), str) or result["status"] not in VERIFICATION_STATUSES:
            raise ReportValidationError(f"{result_id} has unsupported status")
        claim_id = result.get("claim_id")
        if not isinstance(claim_id, str) or not claim_id:
            raise ReportValidationError(f"{result_id} requires claim_id")
        result = dict(result)
        result["verification_result_id"] = result_id
        result["as_of_utc"] = _utc(result.get("as_of_utc"), f"{result_id}.as_of_utc")
        if datetime.fromisoformat(result["as_of_utc"].replace("Z", "+00:00")) > report_as_of:
            raise ReportValidationError(f"{result_id} is newer than report as_of_utc")
        _scope_key(result.get("scope"))
        for field in ("supporting_evidence_ids", "contradicting_evidence_ids"):
            refs = result.get(field) or []
            if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
                raise ReportValidationError(f"{result_id}.{field} must be a list of evidence IDs")
            if len(refs) != len(set(refs)):
                raise ReportValidationError(f"{result_id}.{field} contains duplicate evidence IDs")
            missing = sorted(set(refs) - set(evidence_index))
            if missing:
                raise ReportValidationError(f"{result_id} has unknown evidence IDs: {', '.join(missing)}")
            result[field] = sorted(set(refs))
            result_cutoff = datetime.fromisoformat(result["as_of_utc"].replace("Z", "+00:00"))
            for evidence_id in result[field]:
                captured_at = datetime.fromisoformat(
                    evidence_index[evidence_id]["captured_at_utc"].replace("Z", "+00:00")
                )
                if captured_at > result_cutoff:
                    raise ReportValidationError(
                        f"{evidence_id} is newer than verification as_of_utc for {result_id}"
                    )
        minimum = result.get("minimum_independent_roots", 1)
        if type(minimum) is not int or minimum < 1:
            raise ReportValidationError(f"{result_id}.minimum_independent_roots must be a positive integer")
        for field in ("supporting_root_count", "contradicting_root_count",
                      "independent_support_roots", "independent_contradiction_roots"):
            value = result.get(field)
            if value is not None and (type(value) is not int or value < 0):
                raise ReportValidationError(f"{result_id}.{field} must be a non-negative integer")
        supporting_roots = _independent_roots(result, "supporting")
        contradicting_roots = _independent_roots(result, "contradicting")
        status = result["status"]
        if status == "supported_within_scope" and (supporting_roots < minimum or contradicting_roots > 0):
            raise ReportValidationError(f"{result_id} has inconsistent supported status and root counts")
        if status == "refuted_within_scope" and (contradicting_roots < minimum or supporting_roots > 0):
            raise ReportValidationError(f"{result_id} has inconsistent refuted status and root counts")
        if status == "conflicting_evidence" and not (supporting_roots and contradicting_roots):
            raise ReportValidationError(f"{result_id} conflicting status requires roots on both sides")
        index[result_id] = result
    return index


def _numeric_tokens(text: str) -> set[str]:
    return {match.group(0).replace(",", ".") for match in _NUMERIC_TOKEN.finditer(text)}


def _cited_text(evidence_index: dict[str, dict], evidence_ids: set[str]) -> str:
    """Only text inside the validated exact locator can satisfy a text check."""
    return " ".join(
        evidence_index[evidence_id]["citation"].get("quote", "")
        for evidence_id in sorted(evidence_ids)
    )


def _validate_single_proposition(finding_id: str, statement: str) -> None:
    if "\n" in statement or ";" in statement or re.search(r"[.!?]\s+\S", statement.strip()):
        raise ReportValidationError(f"{finding_id} must contain one proposition per finding")


def _validate_claim_language(finding_id: str, statement: str) -> None:
    _validate_single_proposition(finding_id, statement)
    if _CAUSAL_CUE.search(statement):
        raise ReportValidationError(f"{finding_id} causal claim lacks a dedicated causal verification contract")
    if _TEMPORAL_ORDER_CUE.search(statement):
        raise ReportValidationError(f"{finding_id} temporal-order claim lacks a dedicated TemporalRelation contract")


def _prepare_findings(findings: list[dict], evidence_index: dict[str, dict], verification_index: dict[str, dict], context: dict) -> list[dict]:
    if not isinstance(findings, list):
        raise ReportValidationError("findings must be a list")
    prepared = []
    seen = set()
    allowed_fields = {
        "finding_id", "kind", "claim_id", "statement", "scope", "verification_result_id",
        "evidence_refs", "alternatives", "limitations", "limitation_codes", "limitation_code", "reason_code",
    }
    for raw in findings:
        if not isinstance(raw, dict):
            raise ReportValidationError("each finding must be an object")
        item = dict(raw)
        finding_id = item.get("finding_id")
        kind = item.get("kind")
        if not isinstance(finding_id, str) or not finding_id:
            raise ReportValidationError("finding requires finding_id")
        if finding_id in seen:
            raise ReportValidationError(f"duplicate finding_id: {finding_id}")
        seen.add(finding_id)
        if not isinstance(kind, str) or kind not in FINDING_KINDS:
            raise ReportValidationError(f"{finding_id} has unsupported kind: {kind}")
        unknown = sorted(set(item) - allowed_fields)
        if unknown:
            raise ReportValidationError(f"{finding_id} contains unsupported free-form fields: {', '.join(unknown)}")
        if kind == "methodological_limit" and set(item) - {
            "finding_id", "kind", "evidence_refs", "limitation_code",
        }:
            raise ReportValidationError(f"{finding_id} methodological_limit accepts only its code and evidence references")
        if item.get("limitations"):
            raise ReportValidationError(f"{finding_id} free-text limitations are not allowed; use limitation_codes")
        if kind != "hypothesis" and item.get("alternatives"):
            raise ReportValidationError(f"{finding_id}.alternatives are only valid for hypothesis findings")
        if kind != "methodological_limit" and item.get("limitation_code"):
            raise ReportValidationError(f"{finding_id}.limitation_code is only valid for methodological_limit")
        verification_id = item.get("verification_result_id")
        if kind not in {"supported_within_scope", "refuted_within_scope", "conflicting", "unresolved"} and verification_id is not None:
            raise ReportValidationError(f"{finding_id}.verification_result_id is not valid for kind {kind}")
        if "evidence_refs" not in item:
            raise ReportValidationError(f"{finding_id}.evidence_refs is required; use an empty list when none apply")
        evidence_refs = item.get("evidence_refs")
        if not isinstance(evidence_refs, list):
            raise ReportValidationError(f"{finding_id}.evidence_refs must be a list")
        normalized_refs = []
        for ref in evidence_refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("role"), str) or ref["role"] not in EVIDENCE_ROLES:
                raise ReportValidationError(f"{finding_id} evidence reference requires a valid role")
            evidence_id = ref.get("evidence_id")
            if not isinstance(evidence_id, str) or evidence_id not in evidence_index:
                raise ReportValidationError(f"{finding_id} references unknown evidence_id: {evidence_id}")
            normalized_refs.append({"evidence_id": evidence_id, "role": ref["role"]})
        ref_keys = [(ref["evidence_id"], ref["role"]) for ref in normalized_refs]
        if len(ref_keys) != len(set(ref_keys)):
            raise ReportValidationError(f"{finding_id} contains duplicate evidence references")
        normalized_refs.sort(key=lambda ref: (ref["role"], ref["evidence_id"]))
        item["evidence_refs"] = normalized_refs
        item["citations"] = [
            {"evidence": evidence_index[ref["evidence_id"]], "role": ref["role"]}
            for ref in normalized_refs
        ]

        if kind == "methodological_limit":
            code = item.get("limitation_code")
            if not isinstance(code, str) or code not in LIMITATION_TEXT:
                raise ReportValidationError(f"{finding_id} has unsupported limitation_code")
            item["rendered_text"] = LIMITATION_TEXT[code]
            if item.get("limitation_codes") or item.get("alternatives"):
                raise ReportValidationError(f"{finding_id} methodological_limit uses limitation_code only")
            item["verification"] = None
            prepared.append(item)
            continue

        claim_id = item.get("claim_id")
        statement = item.get("statement")
        if not isinstance(claim_id, str) or not claim_id:
            raise ReportValidationError(f"{finding_id} requires claim_id")
        if not isinstance(statement, str) or not statement.strip():
            raise ReportValidationError(f"{finding_id} requires a non-empty statement")
        if kind not in {"documented_fact", "source_attributed", "methodological_limit"}:
            _validate_single_proposition(finding_id, statement)
        _scope_key(item.get("scope"))
        refs_by_role = {role: [ref["evidence_id"] for ref in normalized_refs if ref["role"] == role] for role in EVIDENCE_ROLES}

        if kind in {"documented_fact", "source_attributed"}:
            if (not normalized_refs or not refs_by_role["source_attributed"]
                    or refs_by_role["supporting"] or refs_by_role["contradicting"]):
                raise ReportValidationError(f"{finding_id} requires source-attributed evidence")
            if kind == "documented_fact" and refs_by_role["context"]:
                raise ReportValidationError(f"{finding_id} documented_fact does not accept context citations")
            if not all(
                evidence_index[eid]["citation"].get("quote") == statement for eid in refs_by_role["source_attributed"]
            ):
                raise ReportValidationError(f"{finding_id} statement must equal every exact source-attributed quote")
            source_ids = sorted({evidence_index[eid]["source_id"] for eid in refs_by_role["source_attributed"]})
            context_source_ids = {
                evidence_index[eid]["source_id"] for eid in refs_by_role["context"]
            }
            if not context_source_ids.issubset(source_ids):
                raise ReportValidationError(f"{finding_id} context must come from the same source as its attributed quote")
            if kind == "documented_fact":
                item["rendered_text"] = (
                    f"У збереженому матеріалі джерела {', '.join(source_ids)} зафіксовано фрагмент: «{statement.strip()}»"
                )
            else:
                context_quotes = [
                    evidence_index[eid]["citation"].get("quote", "")
                    for eid in refs_by_role["context"]
                ]
                context_sentence = (
                    f"Контекст у джерелі: «{'»; «'.join(context_quotes)}». "
                    if context_quotes else ""
                )
                item["rendered_text"] = (
                    f"{context_sentence}Джерело {', '.join(source_ids)} повідомляє: «{statement.strip()}»"
                )
            item["verification"] = None
        elif kind in {"supported_within_scope", "refuted_within_scope", "conflicting"}:
            verification_id = item.get("verification_result_id")
            if not isinstance(verification_id, str) or not verification_id:
                raise ReportValidationError(f"{finding_id} requires a matching bounded VerificationResult")
            vr = verification_index.get(verification_id)
            if not vr or vr["claim_id"] != claim_id or _scope_key(vr["scope"]) != _scope_key(item["scope"]):
                raise ReportValidationError(f"{finding_id} requires a matching bounded VerificationResult")
            if kind == "supported_within_scope" and vr["status"] != "supported_within_scope":
                raise ReportValidationError(f"{finding_id} requires status supported_within_scope")
            if kind == "refuted_within_scope" and vr["status"] != "refuted_within_scope":
                raise ReportValidationError(f"{finding_id} requires status refuted_within_scope")
            if kind == "conflicting" and vr["status"] != "conflicting_evidence":
                raise ReportValidationError(f"{finding_id} requires status conflicting_evidence")
            if kind == "supported_within_scope":
                cited = set(refs_by_role["supporting"])
                contradicted = set(refs_by_role["contradicting"])
                if (not cited or cited != set(vr["supporting_evidence_ids"])
                        or contradicted != set(vr["contradicting_evidence_ids"])
                        or refs_by_role["context"] or refs_by_role["source_attributed"]):
                    raise ReportValidationError(f"{finding_id} evidence citations must exactly match VerificationResult")
                if _independent_roots(vr, "supporting") < _minimum_roots(vr):
                    raise ReportValidationError(f"{finding_id} VerificationResult lacks its required independent support roots")
                _validate_claim_language(finding_id, statement)
                citation_text = _cited_text(evidence_index, cited)
                missing_numbers = sorted(_numeric_tokens(statement) - _numeric_tokens(citation_text))
                if missing_numbers:
                    raise ReportValidationError(f"{finding_id} numeric token(s) absent from supporting evidence: {', '.join(missing_numbers)}")
                item["rendered_text"] = f"У межах визначеного scope матеріали підтримують твердження: «{statement.strip()}»"
                if contradicted:
                    item["rendered_text"] += " Водночас VerificationResult зберігає і суперечні матеріали; цей статус не означає їх відсутності."
            elif kind == "refuted_within_scope":
                cited = set(refs_by_role["contradicting"])
                supports = set(refs_by_role["supporting"])
                if (not cited or cited != set(vr["contradicting_evidence_ids"])
                        or supports != set(vr["supporting_evidence_ids"])
                        or refs_by_role["context"] or refs_by_role["source_attributed"]):
                    raise ReportValidationError(f"{finding_id} evidence citations must exactly match VerificationResult")
                if _independent_roots(vr, "contradicting") < _minimum_roots(vr):
                    raise ReportValidationError(f"{finding_id} VerificationResult lacks its required independent contradiction roots")
                _validate_claim_language(finding_id, statement)
                citation_text = _cited_text(evidence_index, cited)
                missing_numbers = sorted(_numeric_tokens(statement) - _numeric_tokens(citation_text))
                if missing_numbers:
                    raise ReportValidationError(f"{finding_id} numeric token(s) absent from contradicting evidence: {', '.join(missing_numbers)}")
                item["rendered_text"] = f"У визначених межах доказів твердження спростоване: «{statement.strip()}»"
                if supports:
                    item["rendered_text"] += " Водночас VerificationResult зберігає і матеріали на підтримку твердження."
            else:
                supports = set(refs_by_role["supporting"])
                contradicts = set(refs_by_role["contradicting"])
                if not supports or not contradicts:
                    raise ReportValidationError(f"{finding_id} conflict requires supporting and contradicting citations")
                if (supports != set(vr["supporting_evidence_ids"])
                        or contradicts != set(vr["contradicting_evidence_ids"])
                        or refs_by_role["context"] or refs_by_role["source_attributed"]):
                    raise ReportValidationError(f"{finding_id} conflicting citations must exactly match VerificationResult")
                if not _independent_roots(vr, "supporting") or not _independent_roots(vr, "contradicting"):
                    raise ReportValidationError(f"{finding_id} conflict VerificationResult must retain evidence on both sides")
                missing_numbers = sorted(_numeric_tokens(statement) - _numeric_tokens(_cited_text(evidence_index, supports | contradicts)))
                if missing_numbers:
                    raise ReportValidationError(f"{finding_id} numeric token(s) absent from conflict citations: {', '.join(missing_numbers)}")
                item["rendered_text"] = f"Суперечливі матеріали щодо твердження «{statement.strip()}» залишають оцінку нерозв'язаною; звіт не обирає переможну версію."
            item["verification"] = vr
        elif kind == "hypothesis":
            if not normalized_refs:
                raise ReportValidationError(f"{finding_id} hypothesis requires cited evidence")
            cited = {ref["evidence_id"] for ref in normalized_refs}
            citation_text = _cited_text(evidence_index, cited)
            missing_numbers = sorted(_numeric_tokens(statement) - _numeric_tokens(citation_text))
            if missing_numbers:
                raise ReportValidationError(f"{finding_id} numeric token(s) absent from contextual evidence: {', '.join(missing_numbers)}")
            alternatives = item.get("alternatives") or []
            if not isinstance(alternatives, list) or any(not isinstance(alt, str) or alt not in ALTERNATIVE_TEXT for alt in alternatives):
                raise ReportValidationError(f"{finding_id}.alternatives must contain supported alternative codes")
            if len(alternatives) != len(set(alternatives)):
                raise ReportValidationError(f"{finding_id}.alternatives contains duplicate codes")
            item["alternative_texts"] = [ALTERNATIVE_TEXT[code] for code in alternatives]
            item["rendered_text"] = f"Гіпотеза для подальшої перевірки: «{statement.strip()}»"
            item["verification"] = None
        elif kind == "unresolved":
            if not isinstance(item.get("reason_code"), str) or item["reason_code"] not in UNRESOLVED_STATUSES:
                raise ReportValidationError(f"{finding_id} requires unresolved reason_code")
            verification_id = item.get("verification_result_id")
            if not isinstance(verification_id, str) or not verification_id:
                raise ReportValidationError(f"{finding_id} requires a matching bounded VerificationResult")
            vr = verification_index.get(verification_id) if verification_id else None
            if not vr or vr["claim_id"] != claim_id or _scope_key(vr["scope"]) != _scope_key(item["scope"]):
                raise ReportValidationError(f"{finding_id} requires a matching bounded VerificationResult")
            if vr and vr["status"] != item["reason_code"]:
                raise ReportValidationError(f"{finding_id} unresolved reason conflicts with VerificationResult status")
            if (set(refs_by_role["supporting"]) != set(vr["supporting_evidence_ids"])
                    or set(refs_by_role["contradicting"]) != set(vr["contradicting_evidence_ids"])
                    or refs_by_role["source_attributed"] or refs_by_role["context"]):
                raise ReportValidationError(f"{finding_id} unresolved evidence citations must exactly match VerificationResult")
            item["rendered_text"] = (
                f"Не встановлено: «{statement.strip()}». Брак достатніх доказів не є спростуванням "
                "і сам по собі не доводить неправдивість твердження."
            )
            item["verification"] = vr
        limitation_codes = item.get("limitation_codes") or []
        if not isinstance(limitation_codes, list) or any(not isinstance(code, str) or code not in LIMITATION_TEXT for code in limitation_codes):
            raise ReportValidationError(f"{finding_id}.limitation_codes must contain supported limitation codes")
        if len(limitation_codes) != len(set(limitation_codes)):
            raise ReportValidationError(f"{finding_id}.limitation_codes contains duplicate codes")
        item["limitation_texts"] = [LIMITATION_TEXT[code] for code in limitation_codes]
        prepared.append(item)
    return sorted(prepared, key=lambda item: (item["kind"], item["finding_id"]))


def _independent_roots(result: dict, side: str) -> int:
    field = "supporting_root_count" if side == "supporting" else "contradicting_root_count"
    value = result.get(field)
    if type(value) is int and value >= 0:
        return value
    field = "independent_support_roots" if side == "supporting" else "independent_contradiction_roots"
    value = result.get(field)
    return value if type(value) is int and value >= 0 else 0


def _minimum_roots(result: dict) -> int:
    value = result.get("minimum_independent_roots")
    return value if type(value) is int and value > 0 else 1


def _citation_line(evidence: dict, role: str) -> str:
    citation = evidence["citation"]
    source_title = _md(str(evidence.get("source_title") or evidence["source_id"]))
    uri = _md(evidence["source_uri"])
    artifact = _md(evidence["artifact_id"])
    sha = evidence["artifact_sha256"]
    if citation["kind"] == "text_span":
        locator_text = f"Unicode code points {citation['start_offset']}–{citation['end_offset']}"
        quote = _md(citation["quote"])
        quote_part = f" — «{quote}»"
    else:
        start = citation["start_ms"] / 1000
        end = citation["end_ms"] / 1000
        locator_text = f"original PTS {start:.3f}s–{end:.3f}s"
        quote_part = ""
    evidence_label = _md(f"[{evidence['evidence_id']}]")
    return (
        f"- {evidence_label} {source_title} (source ID {_md(evidence['source_id'])}; {uri}); "
        f"роль: {_md(role)}; captured {_md(evidence['captured_at_utc'])}; artifact {artifact} "
        f"SHA-256 {sha}; {locator_text}{quote_part}"
    )


def _render_markdown(context: dict, findings: list[dict]) -> str:
    lines = [
        f"# { _md(context['working_title']) } — аналітичний звіт",
        "",
        f"- Incident ID: {_md(context['incident_id'])}",
        f"- Run ID: {_md(context['run_id'])}",
        f"- Зріз as_of: {_md(context['as_of_utc'])}",
        f"- Сфера: {_md(str(context['scope'].get('description', 'визначена у run context'))) }",
        "",
        "Цей звіт формується з типізованих записів. Наявність посилання підтверджує лише можливість перевірити процитований фрагмент; вона не гарантує семантичної правильності оцінки.",
        "",
        "## Покриття джерел",
        "",
        "```json",
        _json_code_block(context["coverage"]),
        "```",
        "",
    ]
    section_titles = {
        "documented_fact": "Зафіксований вміст документа",
        "source_attributed": "Твердження, приписані джерелам",
        "supported_within_scope": "Підтримано у визначеній сфері",
        "refuted_within_scope": "Спростовано у визначеній сфері доказів",
        "conflicting": "Нерозв'язані суперечності",
        "hypothesis": "Гіпотези для перевірки",
        "unresolved": "Не встановлено",
        "methodological_limit": "Методологічні обмеження",
    }
    for kind in FINDING_ORDER:
        selected = [item for item in findings if item["kind"] == kind]
        if not selected:
            continue
        lines.extend([f"## {section_titles[kind]}", ""])
        for item in selected:
            lines.append(f"### {_md(item['finding_id'])}")
            lines.append("")
            lines.append(_md(item["rendered_text"]))
            if item.get("alternative_texts"):
                lines.extend(["", "Можливі альтернативи:"])
                lines.extend(f"- {_md(str(alt))}" for alt in item["alternative_texts"])
            if item.get("limitation_texts"):
                lines.extend(["", "Обмеження:"])
                lines.extend(f"- {_md(str(note))}" for note in item["limitation_texts"])
            if item["citations"]:
                lines.extend(["", "Доказові посилання:"])
                lines.extend(_citation_line(row["evidence"], row["role"]) for row in item["citations"])
            if item.get("scope"):
                lines.extend([
                    "", "Scope цього finding:", "```json",
                    _json_code_block(item["scope"]), "```",
                ])
            lines.append("")
    if not findings:
        lines.extend(["## Висновок", "", "У цьому запуску немає finding-записів. Це не є підтвердженням або спростуванням подій.", ""])
    lines.extend([
        "## Обмеження автоматизації",
        "",
        "Схемна й citation-перевірка підтверджує цілісність посилань і межі формату, але не семантичне слідування, повноту джерел або істинність описаних подій.",
        "",
    ])
    return "\n".join(lines)


def _jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n" for row in rows)


def _json_code_block(value: Any) -> str:
    # Keep untrusted JSON strings inside the Markdown fence even when they contain backticks.
    encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    return encoded.replace("```", r"\u0060\u0060\u0060")


def _atomic_bundle(output_dir: Path, outputs: dict[str, str], *, overwrite: bool) -> None:
    if output_dir.is_symlink():
        raise ReportValidationError("output directory must not be a symlink")
    names = set(outputs) | {"run_receipt.json"}
    if output_dir.exists():
        if not output_dir.is_dir():
            raise FileExistsError(f"output path is not a directory: {output_dir}")
        children = list(output_dir.iterdir())
        unknown = sorted(child.name for child in children if child.name not in names)
        if unknown:
            raise FileExistsError("refusing to replace directory with unrelated files: " + ", ".join(unknown))
        non_files = sorted(child.name for child in children if not child.is_file())
        if non_files:
            raise FileExistsError("report outputs must be regular files: " + ", ".join(non_files))
        if children and not overwrite:
            raise FileExistsError("refusing to overwrite existing report; pass overwrite=True explicitly")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    try:
        for name, content in outputs.items():
            (staging / name).write_text(content, encoding="utf-8")
        if output_dir.exists():
            _rename_exchange(staging, output_dir)
            shutil.rmtree(staging, ignore_errors=True)
        else:
            os.replace(staging, output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _rename_exchange(left: Path, right: Path) -> None:
    """Atomically exchange two directories on Linux (renameat2 RENAME_EXCHANGE)."""
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOTSUP, "atomic report replacement requires Linux renameat2")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise OSError(errno.ENOTSUP, "libc does not expose renameat2 for atomic report replacement") from exc
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), str(right))


def build_report_bundle(
    findings: list[dict],
    evidence_records: list[dict],
    verification_results: list[dict],
    context: dict,
    output_dir: str | Path,
    *,
    overwrite: bool = False,
    audience_aggregate: dict | None = None,
) -> dict:
    """Validate typed findings and write a deterministic evidence-linked report bundle."""
    if type(overwrite) is not bool:
        raise ReportValidationError("overwrite must be boolean")
    normalized_context = _validate_context(context)
    evidence_index = _make_evidence_index(evidence_records, normalized_context["as_of_utc"])
    verification_index = _verification_index(verification_results, evidence_index, normalized_context)
    prepared = _prepare_findings(findings, evidence_index, verification_index, normalized_context)
    if audience_aggregate is not None:
        validate_audience_aggregate(audience_aggregate)
    citation_count = sum(len(item["citations"]) for item in prepared)
    quality = {
        "schema_version": "1.0",
        "validated_finding_count": len(prepared),
        "citation_count": citation_count,
        "findings_by_kind": {kind: sum(item["kind"] == kind for item in prepared) for kind in sorted(FINDING_KINDS)},
        "unsupported_assertion_count": None,
        "assertion_check_status": "not_evaluated_semantic",
        "quality_scope": "structural citation and status checks only; not semantic entailment or truth",
        "audience_response_status": "included" if audience_aggregate is not None else "not_provided",
        "audience_semantic_status": (
            audience_aggregate["semantic_quality_status"] if audience_aggregate is not None else None
        ),
        "audience_group_count": len(audience_aggregate["groups"]) if audience_aggregate is not None else 0,
        "limitations": [
            "This quality report does not measure whether a source semantically entails a proposition.",
            "Finding completeness is limited by the supplied evidence corpus and upstream retrieval coverage.",
        ],
    }
    normalized_findings = []
    for item in prepared:
        row = {
            key: item[key] for key in (
                "finding_id", "kind", "claim_id", "statement", "scope", "evidence_refs",
                "verification_result_id", "limitation_code", "limitation_codes", "reason_code", "alternatives",
            ) if key in item and item[key] is not None
        }
        row["citation_ids"] = [record["evidence"]["evidence_id"] for record in item["citations"]]
        row["citations"] = [
            {
                "evidence_id": reference["evidence"]["evidence_id"],
                "role": reference["role"],
                "source_id": reference["evidence"]["source_id"],
                "source_title": reference["evidence"].get("source_title"),
                "source_uri": reference["evidence"]["source_uri"],
                "artifact_id": reference["evidence"]["artifact_id"],
                "artifact_sha256": reference["evidence"]["artifact_sha256"],
                "captured_at_utc": reference["evidence"]["captured_at_utc"],
                "locator": reference["evidence"]["citation"],
            }
            for reference in item["citations"]
        ]
        row["verification_result_id"] = (
            (item.get("verification") or {}).get("verification_result_id")
            if item.get("verification") else item.get("verification_result_id")
        )
        normalized_findings.append(row)
    input_payload = {
        "context": normalized_context,
        "findings": findings,
        "evidence": evidence_records,
        "verification_results": verification_results,
        "audience_aggregate": audience_aggregate,
    }
    input_sha256 = hashlib.sha256(_canonical(input_payload).encode("utf-8")).hexdigest()
    outputs = {
        "incident_report.md": _render_markdown(normalized_context, prepared),
        "findings.jsonl": _jsonl(normalized_findings),
        "report_quality.json": json.dumps(quality, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
    }
    if audience_aggregate is not None:
        outputs["audience_response.md"] = render_audience_response_markdown(audience_aggregate)
        outputs["audience_response.json"] = json.dumps(
            audience_aggregate, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
        ) + "\n"
    receipt = {
        "schema_version": "1.0",
        "incident_id": normalized_context["incident_id"],
        "run_id": normalized_context["run_id"],
        "as_of_utc": normalized_context["as_of_utc"],
        "git_commit": normalized_context.get("git_commit"),
        "input_sha256": input_sha256,
        "output_sha256": {name: hashlib.sha256(value.encode("utf-8")).hexdigest() for name, value in sorted(outputs.items())},
        "finding_count": len(prepared),
        "citation_count": citation_count,
        "audience_group_count": len(audience_aggregate["groups"]) if audience_aggregate is not None else 0,
        "producer": "social.analysis.report_generator@1.0",
        "limitations": quality["limitations"],
    }
    outputs["run_receipt.json"] = json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    _atomic_bundle(Path(output_dir), outputs, overwrite=overwrite)
    return {"receipt": receipt, "quality": quality, "output_dir": str(Path(output_dir).resolve())}
