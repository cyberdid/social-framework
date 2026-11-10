"""Normalize target-linked audience model outputs without asserting accuracy."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any


class InterpretationValidationError(ValueError):
    """An interpretation is malformed, ungrounded, or references unknown context."""


_DIMENSIONS = {"topic", "claim", "stance", "sentiment", "emotion", "hostility"}
_SCORE_KINDS = {"none", "raw_score", "uncalibrated_score", "calibrated_probability"}
_ALLOWED_FIELDS = {
    "target_id", "dimension", "label", "language", "model_id", "model_version",
    "context_ids", "evidence_span", "score_kind", "abstention_reason",
}
_OPTIONAL_FIELDS = {"context_evidence_span"}
_EMOJI_ONLY_RE = re.compile(r"^[\W_]+$", re.UNICODE)


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InterpretationValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _span(value: Any, comment_text: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"start", "end", "text"}:
        raise InterpretationValidationError("evidence_span must contain start, end, and text")
    start, end, text = value["start"], value["end"], value["text"]
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise InterpretationValidationError("evidence_span offsets must be increasing Unicode codepoint indices")
    if not isinstance(text, str) or comment_text is None or end > len(comment_text):
        raise InterpretationValidationError("evidence_span cannot be checked against comment text")
    if comment_text[start:end] != text:
        raise InterpretationValidationError("evidence_span text does not match the exact comment span")
    return {"start": start, "end": end, "text": text}


def _context_span(
    value: Any,
    *,
    contexts: set[str],
    context_texts: Mapping[str, str] | None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"context_id", "start", "end", "text"}:
        raise InterpretationValidationError(
            "context_evidence_span must contain context_id, start, end, and text"
        )
    context_id = _nonempty_string(value["context_id"], "context_evidence_span.context_id")
    if context_id not in contexts:
        raise InterpretationValidationError("context_evidence_span context_id does not resolve")
    if context_id not in (context_texts or {}):
        raise InterpretationValidationError("context_evidence_span requires text for its known context")
    start, end, text = value["start"], value["end"], value["text"]
    source_text = context_texts[context_id]
    if not isinstance(source_text, str):
        raise InterpretationValidationError("known context text must be a string")
    if type(start) is not int or type(end) is not int or start < 0 or end <= start:
        raise InterpretationValidationError("context_evidence_span offsets must be increasing Unicode codepoint indices")
    if not isinstance(text, str) or end > len(source_text) or source_text[start:end] != text:
        raise InterpretationValidationError("context_evidence_span text does not match the exact context span")
    if not text.strip() or _EMOJI_ONLY_RE.fullmatch(text.strip()):
        raise InterpretationValidationError("context_evidence_span must contain a text-bearing span")
    return {"context_id": context_id, "start": start, "end": end, "text": text}


def normalize_interpretations(
    rows: Sequence[Mapping[str, Any]],
    *,
    comment_id: str,
    comment_text: str | None,
    known_target_ids: Iterable[str],
    known_context_ids: Iterable[str],
    known_context_texts: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Validate and assign stable IDs to one row per comment/target/dimension/label/model.

    This adapter validates structure and provenance only. It does not verify the
    semantic correctness of a topic, claim, stance, emotion, or hostility label.
    ``known_target_ids`` must come from the versioned Social target registry; the
    caller supplies the source post and available parent/reply context IDs separately.
    """
    comment_id = _nonempty_string(comment_id, "comment_id")
    targets = set(known_target_ids)
    contexts = set(known_context_ids)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise InterpretationValidationError("interpretations must be a sequence of rows")

    normalized: list[dict[str, Any]] = []
    identities: set[tuple[str, str, str, str, str]] = set()
    for raw in rows:
        if not isinstance(raw, Mapping):
            raise InterpretationValidationError("each interpretation must be an object")
        extra = set(raw) - _ALLOWED_FIELDS - _OPTIONAL_FIELDS
        missing = _ALLOWED_FIELDS - set(raw)
        if extra or missing:
            raise InterpretationValidationError(
                f"interpretation fields mismatch; missing={sorted(missing)}, extra={sorted(extra)}"
            )

        target_id = _nonempty_string(raw["target_id"], "target_id")
        if target_id not in targets:
            raise InterpretationValidationError(f"target_id does not resolve in the Social registry: {target_id}")
        dimension = _nonempty_string(raw["dimension"], "dimension")
        if dimension not in _DIMENSIONS:
            raise InterpretationValidationError(f"unsupported interpretation dimension: {dimension}")
        label = _nonempty_string(raw["label"], "label")
        language = _nonempty_string(raw["language"], "language")
        model_id = _nonempty_string(raw["model_id"], "model_id")
        model_version = _nonempty_string(raw["model_version"], "model_version")
        score_kind = _nonempty_string(raw["score_kind"], "score_kind")
        if score_kind not in _SCORE_KINDS:
            raise InterpretationValidationError(f"unsupported score_kind: {score_kind}")

        abstention_reason = raw["abstention_reason"]
        if abstention_reason is not None:
            abstention_reason = _nonempty_string(abstention_reason, "abstention_reason")
        if label == "abstain" and abstention_reason is None:
            raise InterpretationValidationError("abstained interpretations require abstention_reason")
        if label != "abstain" and abstention_reason is not None:
            raise InterpretationValidationError("non-abstained interpretations cannot carry abstention_reason")

        context_ids = raw["context_ids"]
        if (not isinstance(context_ids, list) or
                any(not isinstance(value, str) or not value.strip() for value in context_ids) or
                len(set(context_ids)) != len(context_ids)):
            raise InterpretationValidationError("context_ids must be a unique list of non-empty IDs")
        unknown_contexts = set(context_ids) - contexts
        if unknown_contexts:
            raise InterpretationValidationError(f"context IDs do not resolve: {sorted(unknown_contexts)}")

        evidence_span = _span(raw["evidence_span"], comment_text)
        context_evidence_span = _context_span(
            raw.get("context_evidence_span"),
            contexts=contexts,
            context_texts=known_context_texts,
        )
        text = comment_text if isinstance(comment_text, str) else ""
        if not text.strip() and label != "abstain":
            raise InterpretationValidationError("empty or unavailable comment text requires abstention")
        if _EMOJI_ONLY_RE.fullmatch(text.strip()) and label != "abstain":
            if dimension in {"sentiment", "emotion"}:
                raise InterpretationValidationError(
                    f"emoji-only text cannot establish {dimension}; parent/context text does not establish the commenter's internal affect"
                )
            if (dimension in {"topic", "claim", "stance", "hostility"}
                    and context_evidence_span is None):
                raise InterpretationValidationError(
                    f"emoji-only text cannot establish {dimension}; abstain or provide a text-bearing context span"
                )
        if label != "abstain" and evidence_span is None and not context_ids:
            raise InterpretationValidationError("non-abstained interpretation needs a text span or context IDs")

        identity = (target_id, dimension, label, model_id, model_version)
        if identity in identities:
            raise InterpretationValidationError("duplicate interpretation row")
        identities.add(identity)
        identity_json = json.dumps(
            [comment_id, *identity], ensure_ascii=False, separators=(",", ":")
        )
        interpretation_id = "audience:" + hashlib.sha256(identity_json.encode("utf-8")).hexdigest()
        normalized.append({
            "interpretation_id": interpretation_id,
            "comment_id": comment_id,
            "target_id": target_id,
            "dimension": dimension,
            "label": label,
            "language": language,
            "model_id": model_id,
            "model_version": model_version,
            "context_ids": list(context_ids),
            "evidence_span": evidence_span,
            "context_evidence_span": context_evidence_span,
            "score_kind": score_kind,
            "abstention_reason": abstention_reason,
        })

    return sorted(normalized, key=lambda row: (
        row["target_id"], row["dimension"], row["label"], row["model_id"], row["model_version"]
    ))
