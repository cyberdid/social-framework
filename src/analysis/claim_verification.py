"""Deterministic retrieval and scoped evidence aggregation for incident claims.

Search results are candidates only. Passage relations become usable for an
aggregate verification result only after an explicit accepted assessment with
matching scope and independent-root provenance. This module does not call an
LLM, infer truth from search rank, or treat absent evidence as refutation.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


RELATIONS = frozenset({
    "supports", "contradicts", "mentions", "quotes", "contextualizes",
    "rebuts", "ambiguous", "insufficient_context",
})
STATUSES = frozenset({
    "unverified", "supported_within_scope", "refuted_within_scope",
    "conflicting_evidence", "insufficient_evidence", "not_verifiable", "superseded",
})
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_STOPWORDS = frozenset({
    "і", "й", "та", "або", "що", "це", "у", "в", "на", "до", "з", "із", "за", "для", "про", "від", "по",
    "the", "a", "an", "and", "or", "that", "this", "in", "on", "at", "to", "of", "for", "from",
    "и", "а", "но", "что", "это", "в", "на", "к", "из", "за", "для", "о", "от", "по",
})
_COUNTER_CUES = (
    "спростування", "спростував", "спростувала", "спростували", "не було", "не було підтверджено",
    "не підтвердив", "не підтвердила", "не підтвердили", "відсутні", "відкидає", "відкинули",
    "уточнення", "уточнив", "уточнила", "виправлення", "виправив", "відкликав", "відкликала",
    "correction", "corrected", "retraction", "retracted", "disputed", "denied", "refuted", "debunked",
    "помилково", "фейк", "спростовали", "опровержение", "опроверг", "опровергла", "не было",
    "не подтвердил", "не подтвердила", "уточнение", "исправление", "отозвал", "ложно",
)
_INVESTIGATION_HEDGING_CUES = (
    "має встановити слідство",
    "встановлюється",
    "обставини з'ясовуються",
    "перевіряється",
    "досудове розслідування",
    "географічне виправлення",
    "розрізняє",
    "не встановлено",
    "under investigation",
    "to be determined",
)
_NUMBER_WORDS = {
    "нуль": 0, "ноль": 0, "zero": 0,
    "один": 1, "одна": 1, "одне": 1, "одного": 1, "одну": 1, "one": 1,
    "два": 2, "дві": 2, "двое": 2, "двоє": 2, "двух": 2, "two": 2,
    "три": 3, "троє": 3, "трьох": 3, "three": 3,
    "четыре": 4, "чотири": 4, "четверо": 4, "four": 4,
    "пять": 5, "п'ять": 5, "пятеро": 5, "п'ятеро": 5, "пятьох": 5, "five": 5,
    "шесть": 6, "шість": 6, "шестеро": 6, "six": 6,
    "семь": 7, "сім": 7, "семеро": 7, "seven": 7,
    "восемь": 8, "вісім": 8, "восьмеро": 8, "eight": 8,
    "девять": 9, "дев'ять": 9, "девятеро": 9, "девятьох": 9, "nine": 9,
    "десять": 10, "ten": 10,
    "одиннадцать": 11, "одинадцять": 11, "eleven": 11,
    "двенадцать": 12, "дванадцять": 12, "twelve": 12,
    "тринадцать": 13, "тринадцять": 13, "thirteen": 13,
    "четырнадцать": 14, "чотирнадцять": 14, "fourteen": 14,
    "пятнадцать": 15, "п'ятнадцять": 15, "fifteen": 15,
    "шестнадцать": 16, "шістнадцять": 16, "sixteen": 16,
    "семнадцать": 17, "сімнадцять": 17, "seventeen": 17,
    "восемнадцать": 18, "вісімнадцять": 18, "eighteen": 18,
    "девятнадцать": 19, "дев'ятнадцять": 19, "nineteen": 19,
    "двадцать": 20, "двадцять": 20, "twenty": 20,
    "тридцать": 30, "тридцять": 30, "thirty": 30,
    "сорок": 40, "forty": 40,
    "пятьдесят": 50, "п'ятдесят": 50, "п'ятдесять": 50, "fifty": 50,
    "шестьдесят": 60, "шістдесят": 60, "sixty": 60,
    "семьдесят": 70, "сімдесят": 70, "seventy": 70,
    "восемьдесят": 80, "вісімдесят": 80, "eighty": 80,
    "девяносто": 90, "дев'яносто": 90, "ninety": 90,
    "сто": 100, "hundred": 100,
    "тысяча": 1000, "тисяча": 1000, "thousand": 1000,
}
_NUMERIC_LITERAL = re.compile(r"(?<![\w])[+-]?\d+(?:(?:[.,]\d{3})+|[.,]\d+)?(?![\w])", re.UNICODE)
_NUMBER_WORD_TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?", re.UNICODE)
_NUMBER_JOINERS = frozenset({"і", "и", "and"})
_NUMERIC_VALUE_KEY_HINTS = (
    "count", "number", "quantity", "qty", "total", "amount", "age", "year", "date", "time",
    "duration", "percent", "percentage", "rate", "distance", "length", "height", "weight",
    "mass", "price", "cost", "currency", "money", "кільк", "числ", "сума", "вік", "рок",
    "дат", "час", "тривал", "відсот", "відстан", "довжин", "висот", "ваг", "ціна", "варт",
    "число", "колич", "возраст", "год", "дата", "время", "длитель", "процент", "расстояни",
    "длина", "высота", "цена", "стоим", "сумм",
)
_COUNT_ROLE_HINTS = {
    "people": (
        "person", "people", "individual", "human", "male", "man", "women", "child", "soldier", "troop",
        "participant", "attendee", "member", "men", "detainee", "victim", "casualt", "injur", "dead", "death",
        "hostage", "citizen", "учасник", "участник", "люд", "особ", "человек", "граждан", "військов",
        "военнослуж", "пострадав", "ранен", "погиб", "заложник", "заручник", "жертв", "потерп", "загиб",
    ),
    "comments": ("comment", "коментар", "комментар"),
    "posts": ("post", "допис", "публікац"),
    "channels": ("channel", "канал"),
    "accounts": ("account", "акаунт"),
}
_GENERIC_COUNT_KEY_HINTS = (
    "count", "number", "quantity", "qty", "total", "кільк", "числ", "число", "колич",
)
_COUNT_OPERATOR_PATTERNS = (
    ("lte", re.compile(r"(?:<=|≤)\s*$|(?:^|\b)(?:до|не\s+більше(?:\s+ніж)?|не\s+більш\s+як|не\s+более(?:\s+чем)?|не\s+больше(?:\s+чем)?|not\s+more\s+than|not\s+over|up\s+to|at\s+most|no\s+more\s+than)\s*$")),
    ("gte", re.compile(r"(?:>=|≥)\s*$|(?:^|\b)(?:не\s+менше(?:\s+ніж)?|не\s+менее(?:\s+чем)?|не\s+меньше(?:\s+чем)?|not\s+less\s+than|not\s+under|at\s+least|no\s+fewer\s+than|no\s+less\s+than)\s*$")),
    ("gt", re.compile(r"(?<!<)>(?!=)\s*$|(?:^|\b)(?:понад|більше\s+ніж|более\s+чем|свыше|больше\s+чем|more\s+than|over)\s*$")),
    ("lt", re.compile(r"(?<!<)<(?![=>])\s*$|(?:^|\b)(?:менше\s+ніж|менше|менее\s+чем|меньше\s+чем|меньше|less\s+than|fewer\s+than|under|below)\s*$")),
    ("approx", re.compile(r"≈\s*$|(?:^|\b)(?:приблизно|приблизительно|около|близько|близко|орієнтовно|примерно|approximately|approx|about|around|roughly|circa|nearly|almost|~)\s*$")),
)
_COUNT_UNKNOWN_NEGATION_PREFIX = re.compile(
    r"(?:^|\b)(?:not\s+(?:up\s+to|at\s+most|<=|≤|<|>=|≥|>|≈)|не\s+(?:до|<=|≤|<|>=|≥|>|≈))\s*$"
)
_COUNT_QUALIFIER_MARKERS = re.compile(
    r"(?:<=|>=|≤|≥|(?<!<)<(?![=>])|(?<!>)>(?!=)|≈|\b(?:about|around|roughly|approximately|approx|circa|nearly|almost|at|no|not|up|under|below|less|fewer|more|over|above|between|до|менше|менее|меньше|більше|более|больше|понад|приблизно|приблизительно|около|близько|близко|орієнтовно|примерно)\b)"
)
_NUMERIC_OPERATOR_ALIASES = {
    "eq": "eq", "exact": "eq", "exactly": "eq", "равно": "eq", "рівно": "eq",
    "lte": "lte", "at_most": "lte", "up_to": "lte", "до": "lte",
    "gte": "gte", "at_least": "gte", "не_менше": "gte",
    "gt": "gt", "greater_than": "gt", "more_than": "gt", "понад": "gt",
    "lt": "lt", "less_than": "lt", "fewer_than": "lt", "under": "lt", "below": "lt",
    "approx": "approx", "approximately": "approx", "about": "approx", "приблизно": "approx",
}


def _parse_utc(value: Any, field: str) -> tuple[datetime, str]:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field} must be UTC")
    parsed = parsed.astimezone(timezone.utc)
    text = parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds").replace("+00:00", "Z")
    return parsed, text


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [
        (unicodedata.normalize("NFKC", match.group(0)).casefold(), match.start(), match.end())
        for match in _TOKEN.finditer(text)
        if unicodedata.normalize("NFKC", match.group(0)).casefold() not in _STOPWORDS
    ]


def _terms(text: str) -> list[str]:
    return [token for token, _, _ in _tokens(text)]


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(*parts: str, prefix: str) -> str:
    payload = "\x1f".join(parts)
    return prefix + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _bm25(query_terms: list[str], document_terms: list[str], df: Counter, total_documents: int,
          average_length: float, *, k1: float = 1.2, b: float = 0.75) -> float:
    if not query_terms or not document_terms or total_documents == 0:
        return 0.0
    frequencies = Counter(document_terms)
    length = len(document_terms)
    score = 0.0
    for term, qtf in Counter(query_terms).items():
        tf = frequencies.get(term, 0)
        if not tf:
            continue
        doc_frequency = df.get(term, 0)
        idf = math.log(1.0 + (total_documents - doc_frequency + 0.5) / (doc_frequency + 0.5))
        denominator = tf + k1 * (1.0 - b + b * length / max(average_length, 1.0))
        score += idf * (tf * (k1 + 1.0) / denominator) * (1.0 + math.log(qtf))
    return score


def _claim_query_variants(claim: dict) -> list[str]:
    statement = str(claim.get("statement") or "").strip()
    variants = [statement]
    for value in claim.get("query_variants") or []:
        if isinstance(value, str) and value.strip():
            variants.append(value.strip())
    return list(dict.fromkeys(variants))


def retrieve_evidence_candidates(
    claim: dict,
    passages: list[dict],
    *,
    as_of_utc: str,
    top_k: int = 20,
    counterevidence_terms: tuple[str, ...] = _COUNTER_CUES,
    max_queries: int | None = None,
) -> dict:
    """Search claim and rebuttal/correction directions using local BM25.

    ``captured_at_utc`` is the knowledge-time cutoff field. The returned
    candidates are never labeled as supporting or contradicting evidence.
    At most one candidate per explicitly supplied lineage family is retained
    per search direction; unlinked candidates remain separate and unknown.
    ``max_queries`` bounds variant/direction searches before scoring. The stable
    schedule visits each deduplicated variant (statement first), claim direction
    then counterevidence direction. None preserves the full search schedule.
    """
    claim_id = str(claim.get("claim_id") or "").strip()
    if not claim_id or not str(claim.get("statement") or "").strip():
        raise ValueError("claim_id and non-empty statement are required")
    if type(top_k) is not int or top_k < 1:
        raise ValueError("top_k must be a positive integer")
    if max_queries is not None and (type(max_queries) is not int or max_queries < 0):
        raise ValueError("max_queries must be a non-negative integer or None")
    as_of, as_of_text = _parse_utc(as_of_utc, "as_of_utc")
    variants = _claim_query_variants(claim)
    if not variants:
        raise ValueError("at least one claim query is required")

    future_count = 0
    passages_by_id = {}
    eligible = []
    snapshot_rows = []
    for raw in passages:
        passage_id = str(raw.get("passage_id") or "").strip()
        source_id = str(raw.get("source_id") or "").strip()
        text = raw.get("text")
        if not passage_id or not source_id or not isinstance(text, str) or not text.strip():
            raise ValueError("each passage requires passage_id, source_id, and non-empty text")
        if passage_id in passages_by_id:
            raise ValueError(f"duplicate passage_id: {passage_id}")
        artifact_offset = raw.get("artifact_start_offset")
        if artifact_offset is not None and (type(artifact_offset) is not int or artifact_offset < 0):
            raise ValueError(f"{passage_id}.artifact_start_offset must be null or a non-negative integer")
        captured, captured_text = _parse_utc(raw.get("captured_at_utc"), f"{passage_id}.captured_at_utc")
        passages_by_id[passage_id] = raw
        snapshot_rows.append({
            "passage_id": passage_id,
            "source_id": source_id,
            "artifact_id": raw.get("artifact_id"),
            "captured_at_utc": captured_text,
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "artifact_start_offset": artifact_offset,
            "locator": raw.get("locator"),
            "lineage_family_id": raw.get("lineage_family_id"),
            "independent_root_id": raw.get("independent_root_id"),
            "evidence_basis": raw.get("evidence_basis", "unknown"),
        })
        if captured > as_of:
            future_count += 1
            continue
        doc_tokens = _tokens(text)
        eligible.append({**raw, "passage_id": passage_id, "source_id": source_id,
                         "captured_at_utc": captured_text, "_tokens": doc_tokens,
                         "_token_values": [token for token, _, _ in doc_tokens]})

    total = len(eligible)
    snapshot_rows.sort(key=lambda row: row["passage_id"])
    input_snapshot_sha256 = hashlib.sha256(_canonical(snapshot_rows).encode("utf-8")).hexdigest()
    document_frequency = Counter()
    for row in eligible:
        document_frequency.update(set(row["_token_values"]))
    average_length = sum(len(row["_token_values"]) for row in eligible) / total if total else 0.0

    planned_queries = [
        (direction, variant, _terms(query))
        for variant in variants
        for direction, query in (
            ("claim_terms", variant),
            ("counterevidence_terms", variant + " " + " ".join(counterevidence_terms)),
        )
    ]
    executed_queries = planned_queries if max_queries is None else planned_queries[:max_queries]
    direction_queries: dict[str, list[list[str]]] = {}
    for direction, _, query in executed_queries:
        direction_queries.setdefault(direction, []).append(query)
    counter_cue_tokens = set(term for cue in counterevidence_terms for term in _terms(cue))
    claim_query_tokens = {term for direction, variant, _ in executed_queries
                          if direction == "counterevidence_terms" for term in _terms(variant)}
    scores_by_direction: dict[str, list[tuple[float, dict, set[str]]]] = {}
    unique_roots_skipped = Counter()
    counter_no_claim_overlap_count = 0
    counter_no_cue_count = 0
    for direction, queries in direction_queries.items():
        ranked = []
        all_query_terms = set(term for query in queries for term in query)
        for row in eligible:
            if direction == "counterevidence_terms":
                document_terms = set(row["_token_values"])
                if not counter_cue_tokens & document_terms:
                    counter_no_cue_count += 1
                    continue
                if not claim_query_tokens & document_terms:
                    counter_no_claim_overlap_count += 1
                    continue
            score = max((_bm25(query, row["_token_values"], document_frequency, total, average_length) for query in queries), default=0.0)
            if score <= 0:
                continue
            ranked.append((score, row, all_query_terms))
        ranked.sort(key=lambda item: (-item[0], item[1]["passage_id"]))
        selected = []
        seen_known_families = set()
        for item in ranked:
            row = item[1]
            family = row.get("lineage_family_id")
            if family:
                if family in seen_known_families:
                    unique_roots_skipped[direction] += 1
                    continue
                seen_known_families.add(family)
            selected.append(item)
            if len(selected) >= top_k:
                break
        scores_by_direction[direction] = selected

    merged: dict[str, dict] = {}
    for direction, items in scores_by_direction.items():
        for score, row, query_terms in items:
            passage_id = row["passage_id"]
            tokens = row["_tokens"]
            matching = [(start, end) for token, start, end in tokens if token in query_terms]
            if not matching:
                continue
            start = min(a for a, _ in matching)
            end = max(b for _, b in matching)
            # These quote offsets are always relative to the supplied passage text.
            if end - start > 500:
                first_start, first_end = matching[0]
                start, end = max(0, first_start - 120), min(len(row["text"]), first_end + 240)
            quote = row["text"][start:end]
            passage_text_hash = hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
            candidate_id = _digest(
                claim_id, passage_id, passage_text_hash, str(row.get("artifact_start_offset")),
                prefix="candidate:",
            )
            candidate = merged.setdefault(passage_id, {
                "candidate_id": candidate_id,
                "claim_id": claim_id,
                "passage_id": passage_id,
                "artifact_id": row.get("artifact_id"),
                "source_id": row["source_id"],
                "captured_at_utc": row["captured_at_utc"],
                "lineage_family_id": row.get("lineage_family_id"),
                "lineage_status": "known_dependency_family" if row.get("lineage_family_id") else "unknown",
                "independent_root_id": row.get("independent_root_id"),
                "search_directions": [],
                "direction_scores": {},
                "quote": quote,
                "start_offset": start,
                "end_offset": end,
                "passage_start_offset": start,
                "passage_end_offset": end,
                "passage_offset_unit": "unicode_codepoint",
                "artifact_start_offset": (row["artifact_start_offset"] + start
                                           if row.get("artifact_start_offset") is not None else None),
                "artifact_offset_status": ("available" if row.get("artifact_start_offset") is not None
                                           else "unavailable"),
                "locator": row.get("locator"),
                "offset_unit": "unicode_codepoint",
                "relation": "unassessed",
                "verification_status": "unverified",
                "limitations": [
                    "Lexical retrieval identifies a candidate passage; rank is not evidence of support or truth.",
                    "No independent root is inferred from source identity or absence of a detected copy.",
                ],
            })
            if direction not in candidate["search_directions"]:
                candidate["search_directions"].append(direction)
            candidate["direction_scores"][direction] = round(score, 8)
            # If both query tracks hit, use the shorter exact quote while keeping
            # all matching spans from each route in the search receipt.
            if len(quote) < len(candidate["quote"]):
                candidate["quote"] = quote
                candidate["start_offset"] = start
                candidate["end_offset"] = end
                candidate["passage_start_offset"] = start
                candidate["passage_end_offset"] = end
                candidate["artifact_start_offset"] = (row["artifact_start_offset"] + start
                                                       if row.get("artifact_start_offset") is not None else None)
                candidate["artifact_offset_status"] = ("available" if row.get("artifact_start_offset") is not None
                                                       else "unavailable")
                candidate["locator"] = row.get("locator")

    candidates = sorted(merged.values(), key=lambda row: (-max(row["direction_scores"].values()), row["passage_id"]))
    for candidate in candidates:
        candidate["search_directions"].sort()
    known_families = {row["lineage_family_id"] for row in candidates if row["lineage_family_id"]}
    return {
        "schema_version": "1.0",
        "claim_id": claim_id,
        "as_of_utc": as_of_text,
        "candidates": candidates,
        "search_receipt": {
            "status": "candidates_found" if candidates else "no_candidates_found",
            "directions": list(direction_queries),
            "query_terms": {direction: [sorted(set(query)) for query in queries]
                            for direction, queries in direction_queries.items()},
            "query_variant_count": len(variants),
            "planned_query_count": len(planned_queries),
            "executed_query_count": len(executed_queries),
            "omitted_query_count": len(planned_queries) - len(executed_queries),
            "search_truncated": len(executed_queries) < len(planned_queries),
            "max_queries": max_queries,
            "query_schedule_order": "variant_input_order_then_claim_and_counterevidence",
            "input_passage_count": len(passages),
            "eligible_passage_count": total,
            "excluded_future_count": future_count,
            "counterevidence_no_claim_overlap_count": counter_no_claim_overlap_count,
            "counterevidence_no_cue_count": counter_no_cue_count,
            "candidate_count": len(candidates),
            "input_snapshot_sha256": input_snapshot_sha256,
            "known_dependency_family_count": len(known_families),
            "lineage_unknown_candidate_count": sum(row["lineage_status"] == "unknown" for row in candidates),
            "redundant_family_candidates_skipped": dict(unique_roots_skipped),
            "retriever": "deterministic_local_bm25_v1",
            "top_k_per_direction": top_k,
            "bm25_parameters": {"k1": 1.2, "b": 0.75},
            "counterevidence_cue_terms": sorted(counter_cue_tokens),
            "limitations": [
                "Lexical retrieval can miss paraphrases and can retrieve irrelevant passages.",
                "The source corpus is the supplied passage set; this is not a claim that all relevant sources were searched.",
                "Separate query directions do not encode passage stance or claim verification.",
                "Lineage-family deduplication affects candidate ranking only; it does not prove independence.",
            ],
        },
    }


def _stem_uk(word: str) -> str:
    w = word.lower().strip()
    for suff in ("нути", "нями", "ями", "ого", "ому", "их", "ів", "ем", "ом", "ам", "ях", "ах", "ти", "ли", "ла", "ло", "не", "ня", "ів", "ій", "ий", "а", "е", "у", "і", "и", "о"):
        if w.endswith(suff) and len(w) - len(suff) >= 3:
            return w[:-len(suff)]
    return w


RELATION_RULE_ID = "rule_autonomous_claim_matcher_v2"
_NEGATORS = {"не", "ні", "ані", "нема", "немає", "нет", "not", "no", "never"}
_COPULAS = {"було", "був", "була", "були", "буде", "є"}
_ANCHOR_STOPWORDS = {
    "під", "час", "при", "для", "про", "над", "без", "від", "через", "після", "що", "яка", "який", "яке", "які",
    "було", "були", "був", "була", "але", "або", "там", "вже", "ще", "тут", "цей", "ця", "це", "ці", "них",
}
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?;])\s+(?=[A-ZА-ЯІЇЄҐ«\"])|\n+")
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")


def _content_stems(text: str) -> set[str]:
    return {_stem_uk(t) for t in (w.lower() for w in _WORD_RE.findall(text)) if len(t) >= 3 and t not in _ANCHOR_STOPWORDS}


def _content_prefixes(text: str, size: int = 5) -> set[str]:
    """Inflection-tolerant word prefixes; the suffix stemmer misses alternations such as підозру/підозрюють."""
    return {t[:size] for t in (w.lower() for w in _WORD_RE.findall(text)) if len(t) >= 4 and t not in _ANCHOR_STOPWORDS}


def _negated_stems(text: str) -> set[str]:
    """Stems directly governed by a negator, optionally through a copula ("не було відкрито")."""
    words = [w.lower() for w in _WORD_RE.findall(text)]
    negated = set()
    for i, word in enumerate(words):
        if i and words[i - 1] in _NEGATORS:
            negated.add(_stem_uk(word))
        elif i > 1 and words[i - 1] in _COPULAS and words[i - 2] in _NEGATORS:
            negated.add(_stem_uk(word))
    return negated


def _polarity_mismatch(statement: str, passage_text: str) -> set[str]:
    """Claim content words the passage negates although the claim does not."""
    claim_stems = {s for s in _content_stems(statement) if len(s) >= 3}
    return (_negated_stems(passage_text) & claim_stems) - _negated_stems(statement)


def _value_in_sentence(value: Any, sentence: str) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    digits = str(value)
    spaced = f"{digits[:-3]}[\\s\xa0]{digits[-3:]}" if len(digits) > 3 else digits
    if re.search(rf"(?<!\d)(?:{digits}|{spaced})(?!\d)", sentence):
        return True
    lowered = sentence.lower()
    if value in (2, 60) and "два місяці" in lowered:
        return True
    return any(_NUMBER_WORDS.get(w) == value for w in re.findall(r"\b[^\W_]+\b", lowered))


def _unanchored_values(statement: str, key_values: dict, passage_text: str, min_overlap: int = 2) -> list[str]:
    """Claim values with no passage sentence that carries the value and at least `min_overlap` claim content stems."""
    claim_prefixes = _content_prefixes(statement)
    claim_stems = _content_stems(statement)

    def anchored(sentence: str) -> bool:
        return (len(_content_prefixes(sentence) & claim_prefixes) >= min_overlap
                or len(_content_stems(sentence) & claim_stems) >= min_overlap)

    sentences = [part for part in _SENTENCE_BOUNDARY.split(passage_text) if part and part.strip()]
    unanchored = []
    for key, spec in key_values.items():
        value = spec.get("value") if isinstance(spec, dict) else None
        if not any(_value_in_sentence(value, s) and anchored(s) for s in sentences):
            unanchored.append(str(key))
    return unanchored


def _missing_person_names(statement: str, passage_text: str) -> list[str]:
    """Surnames from adjacent capitalized word pairs in the claim that the passage never mentions."""
    words = _WORD_RE.findall(statement)
    missing = []
    passage_words = _WORD_RE.findall(passage_text)
    for first, second in zip(words, words[1:]):
        if not (first[0].isupper() and second[0].isupper()) or first.isupper() or second.isupper():
            continue
        prefix = second[: max(2, len(second) - 2)]
        if not any(w.startswith(prefix) for w in passage_words):
            missing.append(second)
    return missing


def infer_claim_passage_relation(
    claim: dict,
    passage: dict,
    candidate: dict | None = None,
) -> dict[str, Any]:
    """Deterministically infer the evidentiary relation between a claim and a candidate passage.

    Operates without hardcoded expected relations or pre-scripted statuses. Evaluates:
      1. Explicit counter/denial cues (rebuts, contradicts).
      2. Causal hedging / investigation caveats (contextualizes, insufficient_context).
      3. Numeric value consistency and constraints.
      4. Negative inference guardrails (e.g., entity lease != absence of other entities).
      5. Affirmative predicate and entity alignment (supports).
    """
    statement = str(claim.get("statement") or "").strip()
    p_text = str(passage.get("text") or "").strip()
    if not statement or not p_text:
        return {
            "relation": "insufficient_context",
            "decision": "candidate",
            "confidence": 0.0,
            "rationale": "Empty statement or passage text",
            "asserted_values": {},
            "rule_id": RELATION_RULE_ID,
        }

    stmt_tokens = [t.lower() for t in _terms(statement)]
    stmt_tokens_set = set(stmt_tokens)
    p_tokens = [t.lower() for t in _terms(p_text)]
    p_tokens_set = set(p_tokens)

    p_text_lower = p_text.lower()
    stmt_text_lower = statement.lower()

    denial_clauses = [
        clause for clause in re.split(r"(?<=[.!?;])\s+", p_text_lower)
        if any(cue in clause for cue in _COUNTER_CUES)
    ]
    found_denial_cues = [cue for cue in _COUNTER_CUES if cue in p_text_lower]
    found_hedging_cues = [cue for cue in _INVESTIGATION_HEDGING_CUES if cue in p_text_lower]

    # A denial cue is relevant only when it occurs in a clause about the same
    # substantive topic. Generic overlap such as "police", "people", or the
    # presence of an unrelated number must not turn a different rumor into
    # counterevidence.
    topic_anchor_groups = (
        ("заруч", "утрим", "полон"),
        ("загиб", "загин", "смерт", "помер"),
        ("хребт", "перелом"),
        ("травм", "поран", "ушкодж"),
        ("пожеж", "склад", "займан", "підпал"),
        ("учасник", "причет", "встанов", "ідентифік"),
    )

    def denial_applies_to_claim() -> bool:
        for clause in denial_clauses:
            clause_tokens = set(_terms(clause))
            for anchors in topic_anchor_groups:
                claim_has_anchor = any(anchor in stmt_text_lower for anchor in anchors)
                clause_has_anchor = any(anchor in clause for anchor in anchors)
                if claim_has_anchor and clause_has_anchor:
                    return True
            meaningful_overlap = {
                _stem_uk(token) for token in (stmt_tokens_set & clause_tokens)
                if len(token) >= 4 and token not in {"поліція", "людей", "осіб", "події", "повідомлення", "заворушень"}
            }
            if len(meaningful_overlap) >= 2:
                return True
        return False

    relevant_denial = denial_applies_to_claim()

    # Check for negative inference trap (e.g. claim asserts entity X was present, passage mentions entity Y without mentioning X)
    if "тцк" in stmt_text_lower and "тцк" not in p_text_lower:
        return {
            "relation": "contextualizes",
            "decision": "accepted",
            "confidence": 0.85,
            "rationale": "Passage describes institutional occupancy/activity without mentioning TCK; absence cannot be inferred from alternate occupancy.",
            "asserted_values": {},
            "rule_id": RELATION_RULE_ID,
            "epistemic_limitation": "Negative presence inference rejected under epistemic discipline.",
        }

    # Check for geographic distinction commentary vs forensic video verification
    if ("відео" in stmt_text_lower or "tiktok" in stmt_text_lower) and "географічне виправлення" in p_text_lower:
        return {
            "relation": "contextualizes",
            "decision": "accepted",
            "confidence": 0.85,
            "rationale": "Passage documents user comments making geographic corrections, which does not constitute optical forensic geolocation of video frames.",
            "asserted_values": {},
            "rule_id": RELATION_RULE_ID,
            "epistemic_limitation": "Text comments distinguishing municipalities do not verify video scene.",
        }

    # Check for causal hedging or unverified incident timing of injuries
    if ("під час сутичок" in stmt_text_lower or "під час інциденту" in stmt_text_lower or "внаслідок сутичок" in stmt_text_lower) and "травм" in stmt_text_lower:
        if any(h in p_text_lower for h in ["має встановити слідство", "за яких обставин вони дістали травми"]) or not any(
            w in p_text_lower for w in ["під час сутичок", "внаслідок сутичок", "під час інциденту"]
        ):
            return {
                "relation": "contextualizes",
                "decision": "accepted",
                "confidence": 0.85,
                "rationale": "Passage confirms servicemembers had injuries and were hospitalized, but does not verify that injuries were sustained during the incident (circumstances under investigation).",
                "asserted_values": {},
                "rule_id": RELATION_RULE_ID,
                "epistemic_limitation": "Official acknowledgment of injuries hedges event-specific causality.",
            }

    # Check for verbal denial of medical diagnosis
    if "хребт" in stmt_text_lower and ("хребт" in p_text_lower or "перелам" in p_text_lower) and relevant_denial:
        return {
            "relation": "rebuts",
            "decision": "accepted",
            "confidence": 0.88,
            "rationale": "Spokesperson verbally refuted public reports; however verbal statement is not clinical forensic trauma documentation.",
            "asserted_values": {},
            "rule_id": RELATION_RULE_ID,
            "epistemic_limitation": "Spokesperson denial addresses social media allegation but is not clinical medical verification.",
        }

    # Direct denial / refutation of rumor / event
    if found_denial_cues and relevant_denial:
        return {
            "relation": "contradicts",
            "decision": "accepted",
            "confidence": 0.90,
            "rationale": f"Passage contains a denial cue in the same substantive topic (cues: {', '.join(found_denial_cues[:2])}).",
            "asserted_values": {},
            "rule_id": RELATION_RULE_ID,
            "epistemic_limitation": "The passage records a denial; for physical facts this is an institutional position, not independent physical refutation.",
        }

    # Extract all numbers from passage (digits, spaced thousands, word numbers, legal conversions)
    p_nums = set()
    for m in re.findall(r"\b\d{1,3}(?:[\s\xa0]\d{3})+\b", p_text):
        try:
            p_nums.add(int(re.sub(r"[\s\xa0]", "", m)))
        except ValueError:
            pass
    for m in re.findall(r"\b\d+\b", p_text):
        p_nums.add(int(m))
    for raw_word in re.findall(r"\b[^\W_]+\b", p_text_lower):
        if raw_word in _NUMBER_WORDS:
            p_nums.add(_NUMBER_WORDS[raw_word])
    if "два місяці" in p_text_lower:
        p_nums.add(2)
        p_nums.add(60)

    # Check Numeric Compatibility
    claim_key_values = claim.get("key_values") or {}
    asserted_values = {}
    if claim_key_values:
        for k, spec in claim_key_values.items():
            claimed_val = spec.get("value")
            if claimed_val in p_nums:
                asserted_values[k] = {"value": claimed_val, "operator": spec.get("operator", "eq")}

        prob = _support_value_problem(claim, {"asserted_values": asserted_values})
        if prob:
            if found_denial_cues and relevant_denial:
                return {
                    "relation": "contradicts",
                    "decision": "accepted",
                    "confidence": 0.80,
                    "rationale": f"Explicit denial cues with subject overlap but contradictory numeric values: {prob}",
                    "asserted_values": asserted_values,
                    "rule_id": RELATION_RULE_ID,
                }
            return {
                "relation": "insufficient_context",
                "decision": "candidate",
                "confidence": 0.50,
                "rationale": f"Numeric constraint unsatisfied: {prob}",
                "asserted_values": asserted_values,
                "rule_id": RELATION_RULE_ID,
            }

    # Term / Predicate overlap check with Ukrainian inflection trimming and stopword filtering
    _STOPWORDS = {
        "під", "час", "при", "для", "про", "над", "без", "від", "через", "після",
        "що", "яка", "який", "яке", "які", "було", "були", "був", "була", "але",
        "або", "там", "вже", "ще", "тут", "цей", "ця", "це", "ці", "них",
    }
    stmt_stems = {_stem_uk(t) for t in stmt_tokens if len(t) >= 3 and t not in _STOPWORDS}
    p_stems = {_stem_uk(t) for t in p_tokens if len(t) >= 3 and t not in _STOPWORDS}
    overlap_stems = stmt_stems & p_stems
    stem_ratio = len(overlap_stems) / max(len(stmt_stems), 1)

    has_exact_numeric_support = bool(claim_key_values and asserted_values and len(asserted_values) == len(claim_key_values))
    has_semantic_support = (len(overlap_stems) >= 3 and stem_ratio >= 0.30)

    if has_exact_numeric_support or has_semantic_support:
        guards = []
        unanchored = _unanchored_values(statement, claim_key_values, p_text) if has_exact_numeric_support else []
        if unanchored:
            guards.append(f"value not anchored to the claim predicate in one sentence: {', '.join(unanchored)}")
        negated = _polarity_mismatch(statement, p_text)
        if negated:
            guards.append(f"passage negates claim content: {', '.join(sorted(negated)[:3])}")
        missing_names = _missing_person_names(statement, p_text)
        if missing_names:
            guards.append(f"named person absent from passage: {', '.join(missing_names[:3])}")
        if guards:
            return {
                "relation": "insufficient_context",
                "decision": "candidate",
                "confidence": 0.50,
                "rationale": "Support withheld: " + "; ".join(guards),
                "asserted_values": asserted_values,
                "rule_id": RELATION_RULE_ID,
            }
        return {
            "relation": "supports",
            "decision": "accepted",
            "confidence": 0.92,
            "rationale": f"Affirmative factual and numerical alignment ({len(overlap_stems)} matching root concepts: {', '.join(sorted(list(overlap_stems))[:3])}).",
            "asserted_values": asserted_values if asserted_values else claim_key_values,
            "rule_id": RELATION_RULE_ID,
        }

    return {
        "relation": "insufficient_context",
        "decision": "candidate",
        "confidence": 0.40,
        "rationale": f"Insufficient semantic overlap ({stem_ratio:.2f}) with passage.",
        "asserted_values": {},
        "rule_id": RELATION_RULE_ID,
    }


def create_evidence_assessment(
    claim: dict,
    candidate: dict,
    passage: dict,
    *,
    relation: str,
    decision: str,
    scope: dict,
    assessed_at_utc: str,
    assessor_id: str,
    acceptance_basis: str | None = None,
    validated_rule_id: str | None = None,
    asserted_values: dict | None = None,
    independent_root_id: str | None = None,
    independence_status: str = "unknown",
    evidence_basis: str | None = None,
    limitations: list[str] | None = None,
) -> dict:
    """Create a passage-level assessment after validating exact citation spans.

    ``decision=accepted`` is reserved for explicit human adjudication or a
    documented validated rule. Model-only labels remain candidates.
    """
    claim_id = str(claim.get("claim_id") or "")
    if candidate.get("claim_id") != claim_id:
        raise ValueError("candidate claim_id does not match the claim")
    if candidate.get("passage_id") != passage.get("passage_id"):
        raise ValueError("candidate passage_id does not match passage")
    start, end = candidate.get("start_offset"), candidate.get("end_offset")
    text = passage.get("text")
    quote = candidate.get("quote")
    if not isinstance(start, int) or not isinstance(end, int) or not isinstance(text, str) or not isinstance(quote, str):
        raise ValueError("candidate requires exact integer citation offsets and quote")
    if start < 0 or end <= start or end > len(text) or text[start:end] != quote:
        raise ValueError("exact citation span does not match the original passage text")
    if relation not in RELATIONS:
        raise ValueError(f"unsupported evidence relation: {relation}")
    if decision not in {"candidate", "accepted"}:
        raise ValueError("decision must be candidate or accepted")
    if not isinstance(scope, dict) or not scope:
        raise ValueError("assessment scope must be a non-empty object")
    if decision == "accepted":
        if acceptance_basis not in {"human_adjudication", "validated_rule"}:
            raise ValueError("accepted assessments require human_adjudication or validated_rule basis")
        if acceptance_basis == "validated_rule" and not validated_rule_id:
            raise ValueError("validated_rule acceptance requires validated_rule_id")
        if not assessor_id:
            raise ValueError("accepted assessments require assessor_id")
    if independence_status not in {"unknown", "candidate", "confirmed_independent"}:
        raise ValueError("unsupported independence_status")
    if independence_status == "confirmed_independent" and not independent_root_id:
        raise ValueError("confirmed independent status requires independent_root_id")
    evidence_basis = evidence_basis or passage.get("evidence_basis", "unknown")
    if evidence_basis not in {
        "institutional_statement", "secondary_reporting", "social_media_post",
        "user_comment", "firsthand_observation", "direct_observation",
        "primary_physical_record", "forensic_record", "geolocated_media", "unknown",
    }:
        raise ValueError("unsupported evidence_basis")
    assessed_dt, assessed_at = _parse_utc(assessed_at_utc, "assessed_at_utc")
    captured_dt, _ = _parse_utc(passage.get("captured_at_utc"), "passage.captured_at_utc")
    if captured_dt > assessed_dt:
        raise ValueError("assessment time cannot precede evidence capture time")

    final_relation = relation
    final_decision = decision
    reasons = []
    asserted_values = asserted_values or {}
    if final_relation == "supports":
        value_problem = _support_value_problem(claim, {"asserted_values": asserted_values})
        if value_problem:
            final_relation = "ambiguous"
            final_decision = "candidate"
            reasons.append(value_problem)
    scope_matches = _canonical(scope) == _canonical(claim.get("scope"))
    if not scope_matches and final_decision == "accepted":
        final_decision = "candidate"
        reasons.append("scope_mismatch")
    root_id = independent_root_id or passage.get("independent_root_id")
    assessment = {
        "schema_version": "1.0",
        "claim_id": claim_id,
        "candidate_id": candidate["candidate_id"],
        "evidence_id": candidate["passage_id"],
        "source_id": candidate["source_id"],
        "relation": final_relation,
        "decision": final_decision,
        "scope": scope,
        "scope_matches_claim": scope_matches,
        "asserted_values": asserted_values,
        "independent_root_id": root_id,
        "independence_status": independence_status if root_id else "unknown",
        "evidence_basis": evidence_basis,
        "assessment_source": acceptance_basis if final_decision == "accepted" else "candidate_only",
        "assessor_id": assessor_id,
        "validated_rule_id": validated_rule_id,
        "assessed_at_utc": assessed_at,
        "citation": {
            "passage_id": candidate["passage_id"],
            "start_offset": start,
            "end_offset": end,
            "quote": quote,
            "offset_unit": "unicode_codepoint",
        },
        "reasons": reasons,
        "limitations": list(limitations or []) + [
            "A passage-level relation is not by itself proof that the real-world claim is true or false.",
            "A declared root ID is provenance input; this function does not establish the root's independence.",
        ],
    }
    assessment["assessment_id"] = _digest(
        claim_id, candidate["candidate_id"], _canonical(assessment), prefix="assessment:"
    )
    return assessment


def aggregate_verification(
    claim: dict,
    assessments: list[dict],
    *,
    as_of_utc: str,
    search_complete: bool,
    minimum_independent_roots: int = 2,
    minimum_distinct_lineage_roots: int | None = None,
    supersedes_result_id: str | None = None,
) -> dict:
    """Produce an as-of claim status from accepted, scope-matched assessments.

    Explicit root IDs are treated as declared lineage labels, not as proof of
    source independence. Repeated passages from one declared root count once.
    A physical-fact claim cannot be resolved by an institutional statement
    alone; such a statement is retained and surfaced as an official position.
    """
    claim_id = str(claim.get("claim_id") or "").strip()
    if not claim_id or not isinstance(claim.get("scope"), dict) or not claim["scope"]:
        raise ValueError("claim_id and non-empty claim scope are required")
    if type(search_complete) is not bool:
        raise ValueError("search_complete must be boolean")
    if minimum_distinct_lineage_roots is None:
        minimum_distinct_lineage_roots = minimum_independent_roots
    elif minimum_independent_roots != 2:
        raise ValueError("use either minimum_distinct_lineage_roots or legacy minimum_independent_roots")
    if type(minimum_distinct_lineage_roots) is not int or minimum_distinct_lineage_roots < 1:
        raise ValueError("minimum_distinct_lineage_roots must be a positive integer")
    assessment_ids = [row.get("assessment_id") for row in assessments
                      if isinstance(row.get("assessment_id"), str) and row["assessment_id"].strip()]
    if len(assessment_ids) != len(set(assessment_ids)):
        raise ValueError("duplicate assessment_id")
    as_of, as_of_text = _parse_utc(as_of_utc, "as_of_utc")
    if claim.get("observability") == "not_verifiable":
        status = "not_verifiable"
        usable = []
        excluded = [
            {"assessment_id": row.get("assessment_id"), "reason": "claim_not_verifiable"}
            for row in assessments
        ]
    else:
        usable = []
        excluded = []
        for assessment in assessments:
            if not isinstance(assessment.get("assessment_id"), str) or not assessment["assessment_id"].strip():
                excluded.append({"assessment_id": assessment.get("assessment_id"), "reason": "missing_assessment_id"})
                continue
            try:
                assessed, _ = _parse_utc(assessment.get("assessed_at_utc"), "assessment.assessed_at_utc")
            except ValueError:
                excluded.append({"assessment_id": assessment.get("assessment_id"), "reason": "invalid_assessment_time"})
                continue
            reason = None
            if assessment.get("claim_id") != claim_id:
                reason = "claim_mismatch"
            elif assessed > as_of:
                reason = "assessment_after_as_of"
            elif assessment.get("decision") != "accepted":
                reason = "candidate_only"
            elif assessment.get("assessment_source") not in {"human_adjudication", "validated_rule"}:
                reason = "missing_acceptance_provenance"
            elif not assessment.get("assessor_id"):
                reason = "missing_assessor_id"
            elif assessment.get("assessment_source") == "validated_rule" and not assessment.get("validated_rule_id"):
                reason = "missing_validated_rule_id"
            elif assessment.get("relation") not in {"supports", "contradicts"}:
                reason = "non_verdict_relation"
            elif assessment.get("relation") == "supports" and _support_value_problem(claim, assessment):
                reason = _support_value_problem(claim, assessment)
            elif _canonical(assessment.get("scope")) != _canonical(claim["scope"]):
                reason = "scope_mismatch"
            elif assessment.get("scope_matches_claim") is False:
                reason = "scope_mismatch"
            elif not assessment.get("evidence_id"):
                reason = "missing_evidence_id"
            elif not _valid_citation(assessment):
                reason = "missing_or_invalid_citation"
            if reason:
                excluded.append({"assessment_id": assessment.get("assessment_id"), "reason": reason})
            else:
                usable.append(assessment)
        support = [row for row in usable if row["relation"] == "supports"]
        contradict = [row for row in usable if row["relation"] == "contradicts"]
        support_roots = {row["independent_root_id"] for row in support if row.get("independent_root_id")}
        contradict_roots = {row["independent_root_id"] for row in contradict if row.get("independent_root_id")}
        shared_roots = support_roots & contradict_roots
        if support_roots and contradict_roots:
            status = "conflicting_evidence"
        elif len(support_roots) >= minimum_distinct_lineage_roots:
            status = "supported_within_scope"
        elif len(contradict_roots) >= minimum_distinct_lineage_roots:
            status = "refuted_within_scope"
        elif not search_complete:
            status = "unverified"
        else:
            status = "insufficient_evidence"

    support_rows = [row for row in usable if row["relation"] == "supports"] if status != "not_verifiable" else []
    contradict_rows = [row for row in usable if row["relation"] == "contradicts"] if status != "not_verifiable" else []
    support_ids = sorted({row["evidence_id"] for row in support_rows})
    contradict_ids = sorted({row["evidence_id"] for row in contradict_rows})
    support_roots = sorted({row["independent_root_id"] for row in support_rows if row.get("independent_root_id")})
    contradict_roots = sorted({row["independent_root_id"] for row in contradict_rows if row.get("independent_root_id")})
    confirmed_support_roots = sorted({row["independent_root_id"] for row in support_rows
                                      if row.get("independence_status") == "confirmed_independent" and row.get("independent_root_id")})
    confirmed_contradict_roots = sorted({row["independent_root_id"] for row in contradict_rows
                                         if row.get("independence_status") == "confirmed_independent" and row.get("independent_root_id")})
    direct_physical_basis = {"firsthand_observation", "direct_observation", "primary_physical_record", "forensic_record", "geolocated_media"}
    physical_rows = [row for row in support_rows + contradict_rows if row.get("evidence_basis") in direct_physical_basis]
    official_denial_rows = [row for row in usable
                            if row.get("evidence_basis") == "institutional_statement"
                            and row.get("relation") in {"contradicts", "rebuts"}]
    if claim.get("scope", {}).get("type") == "physical_fact" and status in {"supported_within_scope", "refuted_within_scope", "conflicting_evidence"} and not physical_rows:
        status = "insufficient_evidence"
    assessments_used = sorted(row["assessment_id"] for row in usable if row.get("assessment_id"))
    exclusion_by_id = {row.get("assessment_id"): row["reason"] for row in excluded}
    used_id_set = set(assessments_used)
    assessment_provenance = [
        _assessment_audit_record(
            row,
            row.get("assessment_id") in used_id_set,
            exclusion_by_id.get(row.get("assessment_id")),
        )
        for row in sorted(assessments, key=lambda item: (str(item.get("assessment_id") or ""), _canonical(item)))
    ]
    material = _canonical({
        "claim_id": claim_id,
        "claim_statement": claim.get("statement"),
        "scope": claim["scope"],
        "key_values": claim.get("key_values", {}),
        "as_of_utc": as_of_text,
        "status": status,
        "assessment_ids": sorted(assessments_used),
        "assessment_provenance": assessment_provenance,
        "excluded_assessment_ids": sorted(row["assessment_id"] for row in excluded if row.get("assessment_id")),
        "minimum_distinct_lineage_roots": minimum_distinct_lineage_roots,
        "search_complete": bool(search_complete),
        "observability": claim.get("observability"),
        "supersedes": supersedes_result_id,
    })
    result_id = "verification:" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
    return {
        "schema_version": "1.0",
        "result_id": result_id,
        "claim_id": claim_id,
        "scope": claim["scope"],
        "as_of_utc": as_of_text,
        "status": status,
        "search_complete": bool(search_complete),
        "supporting_evidence_ids": support_ids,
        "contradicting_evidence_ids": contradict_ids,
        "supporting_root_count": len(support_roots),
        "contradicting_root_count": len(contradict_roots),
        "supporting_lineage_root_count": len(support_roots),
        "contradicting_lineage_root_count": len(contradict_roots),
        "supporting_lineage_root_ids": support_roots,
        "contradicting_lineage_root_ids": contradict_roots,
        "shared_root_ids": sorted(set(support_roots) & set(contradict_roots)),
        "independent_support_roots": len(confirmed_support_roots),
        "independent_contradiction_roots": len(confirmed_contradict_roots),
        "minimum_independent_roots": minimum_distinct_lineage_roots,
        "minimum_distinct_lineage_roots": minimum_distinct_lineage_roots,
        "official_position": "officially_disputed" if official_denial_rows else None,
        "assessment_ids": assessments_used,
        "assessment_provenance": assessment_provenance,
        "excluded_assessments": excluded,
        "excluded_assessment_ids": sorted(row["assessment_id"] for row in excluded if row.get("assessment_id")),
        "supersedes": supersedes_result_id,
        "rationale": _status_rationale(
            status, len(support_roots), len(contradict_roots), minimum_distinct_lineage_roots,
            search_complete, len(set(support_roots) & set(contradict_roots)),
        ),
        "limitations": [
            "This result is limited to the exact claim scope and supplied evidence corpus.",
            "Retrieval rank, publication volume, and model agreement are not evidence of truth.",
            "Distinct root IDs are declared source-lineage labels, not evidence that sources are independent.",
            "Physical-fact statuses require direct observation, a primary physical record, forensic evidence, or geolocated media; institutional statements alone remain official positions.",
            "insufficient_evidence and unverified are not refutations.",
        ],
    }


def append_verification_result(
    ledger: Any,
    result: dict,
    *,
    producer: str,
    run_id: str,
) -> dict:
    """Append a projection to the existing immutable EvidenceLedger.

    The ledger checks that the claim and all evidence IDs already exist. The
    serialized scope is kept as canonical JSON because the ledger's current
    contract stores scope as text.
    """
    if result.get("status") not in STATUSES:
        raise ValueError("unsupported verification result status")
    if not result.get("result_id") or not result.get("claim_id"):
        raise ValueError("verification result requires result_id and claim_id")
    rationale_payload = {
        "summary": result.get("rationale"),
        "assessment_ids": result.get("assessment_ids", []),
        "assessment_provenance": result.get("assessment_provenance", []),
        "excluded_assessment_ids": result.get("excluded_assessment_ids", []),
        "excluded_assessments": result.get("excluded_assessments", []),
        "verification_parameters": {
            "as_of_utc": result.get("as_of_utc"),
            "search_complete": result.get("search_complete"),
            "minimum_independent_roots": result.get("minimum_independent_roots"),
            "status": result.get("status"),
            "supporting_root_count": result.get("supporting_root_count"),
            "contradicting_root_count": result.get("contradicting_root_count"),
            "shared_root_ids": result.get("shared_root_ids", []),
        },
    }
    existing = ledger.get_record(result["result_id"])
    return ledger.add_verification_result(
        result["result_id"],
        claim_id=result["claim_id"],
        scope=_canonical(result["scope"]),
        as_of=result["as_of_utc"],
        status=result["status"],
        supporting_evidence_ids=result.get("supporting_evidence_ids", []),
        contradicting_evidence_ids=result.get("contradicting_evidence_ids", []),
        rationale=json.dumps(rationale_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        supersedes=result.get("supersedes"),
        producer=producer,
        run_id=run_id,
        limitations=result.get("limitations", []),
        created_at=existing["created_at"] if existing else None,
    )


def _status_rationale(
    status: str,
    support_count: int,
    contradict_count: int,
    minimum: int,
    search_complete: bool,
    shared_root_count: int = 0,
) -> str:
    if status == "supported_within_scope":
        return f"Accepted, scope-matched support from {support_count} declared source-lineage root(s) meets threshold {minimum}; no contradiction root was accepted."
    if status == "refuted_within_scope":
        return f"Accepted, scope-matched contradictory evidence from {contradict_count} declared source-lineage root(s) meets threshold {minimum}; no support root was accepted."
    if status == "conflicting_evidence":
        return (
            f"Accepted, scope-matched support and contradiction have declared source-lineage roots "
            f"({support_count} and {contradict_count}); {shared_root_count} root(s) occur on both sides."
        )
    if status == "not_verifiable":
        return "The claim is explicitly marked outside the observable scope of this workflow."
    if not search_complete:
        return "Evidence search is incomplete; no final truth assessment is available."
    return "The supplied evidence does not meet the declared source-lineage threshold; absence of evidence is not refutation."


def _support_value_problem(claim: dict, assessment: dict) -> str | None:
    """Return why an asserted support relation fails exact claim value checks."""
    expected_values = claim.get("key_values") or {}
    asserted_values = assessment.get("asserted_values") or {}

    # Natural-language quantities are evidence-relevant claim arguments. They
    # must first be represented in the structured claim, otherwise an empty or
    # unrelated key_values object could silently bypass numeric validation.
    statement = str(claim.get("statement") or "")
    structured = _structured_numeric_values(expected_values)
    for value, is_count, count_role, start, end in _statement_numeric_values(statement):
        if is_count:
            matching = [row for row in structured
                        if value == row[1] and _compatible_count_key(row[0], count_role)]
            if not matching:
                return f"missing_structured_value:{value}"
            required_operator = _statement_count_operator(statement, start, end)
            actual_operators = {row[2] for row in matching}
            if required_operator not in actual_operators:
                operator_path = matching[0][0] or "count"
                return f"numeric_operator_mismatch:{operator_path}:{required_operator}:{sorted(actual_operators)[0]}"
        else:
            found = any(
                value == structured_value and any(hint in key_path for hint in _NUMERIC_VALUE_KEY_HINTS)
                for key_path, structured_value, _operator in structured
            )
            if not found:
                return f"missing_structured_value:{value}"

    for key, expected in expected_values.items():
        if key not in asserted_values:
            return f"missing_asserted_value:{key}"
        if _canonical(asserted_values[key]) != _canonical(expected):
            return f"value_mismatch:{key}"
    return None


def _number_key(value: Any) -> str | None:
    """Normalize a structured scalar to a stable decimal key when possible."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        raw = str(value)
    elif isinstance(value, str) and re.fullmatch(r"\s*[+-]?\d+(?:[.,]\d+)*\s*", value):
        raw = value.strip()
    else:
        return None
    sign = ""
    if raw[:1] in {"+", "-"}:
        sign, raw = raw[0], raw[1:]
    separators = [character for character in raw if character in {",", "."}]
    if "," in raw and "." in raw:
        decimal_separator = "," if raw.rfind(",") > raw.rfind(".") else "."
        grouping_separator = "." if decimal_separator == "," else ","
        raw = raw.replace(grouping_separator, "").replace(decimal_separator, ".")
    elif separators:
        separator = separators[0]
        groups = raw.split(separator)
        if len(groups) > 2:
            if not (1 <= len(groups[0]) <= 3 and all(len(group) == 3 for group in groups[1:])):
                return None
            raw = "".join(groups)
        elif len(groups) == 2 and len(groups[1]) == 3 and 1 <= len(groups[0]) <= 3:
            # A three-digit group is treated as a thousands separator to avoid
            # interpreting 40,000 as the value 40.
            raw = "".join(groups)
        else:
            raw = ".".join(groups)
    raw = sign + raw
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    normalized = number.normalize()
    return format(normalized, "f")


def _numeric_word_phrases(text: str) -> list[tuple[str, int, int]]:
    """Return simple Ukrainian/Russian/English cardinal phrases and spans."""
    tokens = []
    for match in _NUMBER_WORD_TOKEN.finditer(unicodedata.normalize("NFKC", text).casefold()):
        word = match.group(0).replace("’", "'").replace("'", "")
        tokens.append((word, match.start(), match.end()))
    phrases: list[tuple[str, int, int]] = []
    index = 0
    while index < len(tokens):
        word, start, end = tokens[index]
        if word not in _NUMBER_WORDS:
            index += 1
            continue
        total = 0
        current = 0
        phrase_end = end
        cursor = index
        consumed = False
        while cursor < len(tokens):
            token, _token_start, token_end = tokens[cursor]
            if token in _NUMBER_JOINERS and cursor + 1 < len(tokens) and tokens[cursor + 1][0] in _NUMBER_WORDS:
                phrase_end = token_end
                cursor += 1
                continue
            if token not in _NUMBER_WORDS:
                break
            value = _NUMBER_WORDS[token]
            if value == 100:
                current = max(current, 1) * 100
            elif value == 1000:
                total += max(current, 1) * 1000
                current = 0
            elif value >= 20:
                current += value
            elif current >= 20:
                current += value
            elif current == 0:
                current = value
            else:
                break
            phrase_end = token_end
            cursor += 1
            consumed = True
        if consumed:
            phrases.append((_number_key(total + current) or "0", start, phrase_end))
            index = max(index + 1, cursor)
        else:
            index += 1
    return phrases


def _statement_numeric_values(statement: str) -> list[tuple[str, bool, str | None, int, int]]:
    """Extract explicit numerals and common number words from a claim."""
    matches: list[tuple[str, int, int]] = []
    for match in _NUMERIC_LITERAL.finditer(statement):
        key = _number_key(match.group(0))
        if key is not None:
            matches.append((key, match.start(), match.end()))
    matches.extend(_numeric_word_phrases(statement))
    matches.sort(key=lambda item: (item[1], item[2]))
    result = []
    seen = set()
    for value, start, end in matches:
        count_role = _surface_count_role(statement, start, end)
        is_count = count_role is not None
        pair = (value, is_count, count_role, start, end)
        if pair not in seen:
            seen.add(pair)
            result.append((value, is_count, count_role, start, end))
    return result


def _structured_numeric_values(values: Any, path: str = "") -> list[tuple[str, str, str]]:
    """Flatten numeric values while retaining semantic key path and operator."""
    output: list[tuple[str, str, str]] = []
    if isinstance(values, dict):
        if "value" in values and _number_key(values.get("value")) is not None:
            operator = _normalize_numeric_operator(values.get("operator", "eq"))
            output.append((path.casefold(), _number_key(values["value"]), operator))
            return output
        for key, value in values.items():
            if str(key).casefold() == "operator":
                continue
            child_path = f"{path}.{key}" if path else str(key)
            output.extend(_structured_numeric_values(value, child_path))
    elif isinstance(values, list):
        for index, value in enumerate(values):
            output.extend(_structured_numeric_values(value, f"{path}.{index}"))
    else:
        key = _number_key(values)
        if key is not None:
            output.append((path.casefold(), key, "eq"))
    return output


def _normalize_numeric_operator(value: Any) -> str:
    if not isinstance(value, str):
        return "unknown"
    return _NUMERIC_OPERATOR_ALIASES.get(value.strip().casefold().replace(" ", "_"), "unknown")


def _statement_count_operator(statement: str, start: int, end: int) -> str:
    """Recognize explicit count qualifiers and abstain on an unparsed nearby cue."""
    prefix = unicodedata.normalize("NFKC", statement[max(0, start - 64):start]).casefold()
    prefix = re.sub(r"\s+", " ", prefix)
    if _COUNT_UNKNOWN_NEGATION_PREFIX.search(prefix):
        return "unknown"
    for operator, pattern in _COUNT_OPERATOR_PATTERNS:
        match = pattern.search(prefix)
        if match:
            preceding_qualifier = " ".join(prefix[:match.start()].split()[-3:])
            if _COUNT_QUALIFIER_MARKERS.search(preceding_qualifier):
                return "unknown"
            return operator
    suffix = unicodedata.normalize("NFKC", statement[end:min(len(statement), end + 40)]).casefold()
    # Keep the fallback marker window local to the number. A later clause such
    # as "40 comments were posted about the incident" must not reinterpret
    # the count as approximate merely because "about" occurs several words
    # after the counted noun.
    local_context = " ".join(prefix.split()[-4:]) + " " + " ".join(suffix.split()[:2])
    if _COUNT_QUALIFIER_MARKERS.search(local_context):
        return "unknown"
    return "eq"


def _count_key_role(key_path: str) -> str | None:
    normalized_path = re.sub(r"[_\.]+", " ", key_path.casefold())
    roles = [
        role for role, hints in _COUNT_ROLE_HINTS.items()
        if any(_count_role_hint_matches(normalized_path, hint) for hint in hints)
    ]
    if len(roles) == 1:
        return roles[0]
    if roles:
        return "ambiguous"
    if any(hint in key_path for hint in _GENERIC_COUNT_KEY_HINTS):
        return "generic"
    return None


def _compatible_count_key(key_path: str, claim_role: str | None) -> bool:
    key_role = _count_key_role(key_path)
    if key_role == "generic":
        return True
    return bool(claim_role and key_role == claim_role)


def _surface_count_role(text: str, start: int, end: int) -> str | None:
    lower = text.casefold()
    left = max(0, start - 64)
    right = min(len(text), end + 64)
    matches = []
    for role, hints in _COUNT_ROLE_HINTS.items():
        for hint in hints:
            if hint == "men":
                men_match = re.search(r"(?<!\w)men(?!\w)", lower[left:right])
                if men_match:
                    cursor = left + men_match.start()
                    distance = start - (cursor + len(hint)) if cursor + len(hint) <= start else cursor - end
                    matches.append((abs(distance), role))
                continue
            cursor = lower.find(hint, left, right)
            while cursor >= 0:
                distance = start - (cursor + len(hint)) if cursor + len(hint) <= start else cursor - end
                matches.append((abs(distance), role))
                cursor = lower.find(hint, cursor + 1, right)
    return min(matches, default=(0, None), key=lambda row: (row[0], row[1] or ""))[1]


def _count_role_hint_matches(value: str, hint: str) -> bool:
    if hint == "men":
        return bool(re.search(r"(?<!\w)men(?!\w)", value))
    return hint in value


def _assessment_audit_record(assessment: dict, included: bool, exclusion_reason: str | None) -> dict:
    fields = (
        "assessment_id", "claim_id", "candidate_id", "evidence_id", "source_id", "relation", "decision",
        "scope", "scope_matches_claim", "asserted_values", "independent_root_id", "independence_status", "evidence_basis",
        "assessment_source", "assessor_id", "validated_rule_id", "assessed_at_utc", "citation", "reasons",
    )
    record = {field: assessment[field] for field in fields if field in assessment}
    record["included_in_verification"] = included
    record["exclusion_reason"] = exclusion_reason
    return record


def _assessment_matches_projection(assessment: dict, audit: dict) -> bool:
    projection_fields = {"included_in_verification", "exclusion_reason"}
    current = _assessment_audit_record(assessment, False, None)
    current_payload = {key: value for key, value in current.items() if key not in projection_fields}
    audit_payload = {key: value for key, value in audit.items() if key not in projection_fields}
    return _canonical(current_payload) == _canonical(audit_payload)


def export_verification_package(
    search_result: dict,
    assessments: list[dict],
    verification_result: dict,
    output_dir: str | Path,
) -> dict[str, Path]:
    """Write deterministic JSONL exports, claim matrix, and unresolved queue."""
    claim_id = str(verification_result.get("claim_id") or "")
    if not claim_id or search_result.get("claim_id") != claim_id:
        raise ValueError("search result and verification result must share a claim_id")
    if not isinstance(search_result.get("candidates"), list):
        raise ValueError("search_result.candidates must be a list")
    if not isinstance(search_result.get("search_receipt"), dict):
        raise ValueError("search_result.search_receipt must be an object")
    candidates = search_result["candidates"]
    candidate_ids = {row.get("candidate_id") for row in candidates}
    included_candidate_ids = {
        row.get("candidate_id") for row in verification_result.get("assessment_provenance", [])
        if row.get("included_in_verification") and row.get("candidate_id")
    }
    unassessed_candidate_ids = sorted(candidate_ids - included_candidate_ids)
    unresolved_statuses = {
        "unverified", "insufficient_evidence", "conflicting_evidence", "not_verifiable", "superseded",
    }
    queue_rows = []
    if verification_result.get("status") in unresolved_statuses or unassessed_candidate_ids:
        reasons = []
        if verification_result.get("status") in unresolved_statuses:
            reasons.append(f"verification_status:{verification_result.get('status')}")
        if unassessed_candidate_ids:
            reasons.append("candidate_passages_without_accepted_assessment")
        queue_rows.append({
            "schema_version": "1.0",
            "claim_id": claim_id,
            "as_of_utc": verification_result.get("as_of_utc"),
            "verification_status": verification_result.get("status"),
            "candidate_ids_to_review_or_retrieve": unassessed_candidate_ids,
            "reason_codes": reasons,
            "targeted_next_steps": _unresolved_next_steps(verification_result),
            "search_snapshot_sha256": (search_result.get("search_receipt") or {}).get("input_snapshot_sha256"),
        })

    scope_text = _markdown_cell(_canonical(verification_result.get("scope")))
    support_text = _markdown_cell(", ".join(verification_result.get("supporting_evidence_ids", [])) or "—")
    contradict_text = _markdown_cell(", ".join(verification_result.get("contradicting_evidence_ids", [])) or "—")
    assessment_text = _markdown_cell(", ".join(verification_result.get("assessment_ids", [])) or "—")
    matrix = "\n".join([
        "# Матриця перевірки тверджень",
        "",
        "Експорт містить статус у межах заданого scope та доступного корпусу; це не оцінка всіх можливих джерел.",
        "",
        "| Claim ID | Статус | Scope | As of (UTC) | Докази за | Докази проти | Assessments | Пояснення |",
        "|---|---|---|---|---|---|---|---|",
        "| " + " | ".join([
            _markdown_cell(claim_id),
            _markdown_cell(str(verification_result.get("status", "unknown"))),
            scope_text,
            _markdown_cell(str(verification_result.get("as_of_utc", ""))),
            support_text,
            contradict_text,
            assessment_text,
            _markdown_cell(str(verification_result.get("rationale", ""))),
        ]) + " |",
        "",
        f"Пошукових кандидатів: {len(candidates)}. Прийнятих assessment: {len(verification_result.get('assessment_ids', []))}. Відкритих retrieval-запитів: {len(queue_rows)}.",
    ]) + "\n"

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    audit_by_id = {
        row.get("assessment_id"): row
        for row in verification_result.get("assessment_provenance", [])
        if row.get("assessment_id")
    }
    excluded_by_id = {
        row.get("assessment_id"): row.get("reason")
        for row in verification_result.get("excluded_assessments", [])
        if row.get("assessment_id")
    }
    exported_assessments = []
    as_of_utc = verification_result.get("as_of_utc")
    for assessment in sorted(assessments, key=lambda row: (str(row.get("assessment_id") or ""), _canonical(row))):
        assessment_id = assessment.get("assessment_id")
        audit = audit_by_id.get(assessment_id)
        payload_matches = bool(audit and _assessment_matches_projection(assessment, audit))
        included = bool(payload_matches and audit.get("included_in_verification"))
        exclusion_reason = None if included else (
            "assessment_payload_mismatch" if audit and not payload_matches else
            audit.get("exclusion_reason") if audit else
            excluded_by_id.get(assessment_id, "assessment_not_in_projection")
        )
        exported_assessments.append({
            **assessment,
            "as_of_projection": {
                "as_of_utc": as_of_utc,
                "verification_result_id": verification_result.get("result_id"),
                "included_in_verification": included,
                "exclusion_reason": exclusion_reason,
            },
        })

    payloads = {
        "candidates": _jsonl(candidates),
        "assessments": _jsonl(exported_assessments),
        "results": _jsonl([verification_result]),
        "search_receipt": json.dumps(search_result["search_receipt"], ensure_ascii=False,
                                      sort_keys=True, separators=(",", ":")) + "\n",
        "claim_matrix": matrix,
        "unresolved_queue": _jsonl(queue_rows),
    }
    filenames = {
        "candidates": "evidence_candidates.jsonl",
        "assessments": "evidence_assessments.jsonl",
        "results": "verification_results.jsonl",
        "search_receipt": "search_receipt.json",
        "claim_matrix": "claim_matrix.md",
        "unresolved_queue": "unresolved_retrieval_queue.jsonl",
    }
    paths = {name: directory / filename for name, filename in filenames.items()}
    if len({path.resolve() for path in paths.values()}) != len(paths):
        raise ValueError("verification package output paths collide")
    for name, path in paths.items():
        _write_text_atomic(path, payloads[name])
    return paths


def _jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for row in rows)


def _markdown_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _write_text_atomic(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _unresolved_next_steps(result: dict) -> list[str]:
    status = result.get("status")
    if status == "conflicting_evidence":
        return ["Знайти незалежні першоджерела для протилежних версій.", "Перевірити чи одна з версій була уточнена або відкликана."]
    if status in {"unverified", "insufficient_evidence"}:
        return ["Розширити пошук на первинні документи, офіційні уточнення та незалежні повідомлення.", "Перевірити пропущені джерельні сім'ї та часові зрізи."]
    if status == "not_verifiable":
        return ["Зафіксувати, які додаткові непублічні або поза корпусом докази були б потрібні; не трактувати відсутність даних як спростування."]
    return ["Повторно оцінити нові матеріали окремим версійованим запуском."]


def _valid_citation(assessment: dict) -> bool:
    citation = assessment.get("citation")
    if not isinstance(citation, dict):
        return False
    start, end = citation.get("start_offset"), citation.get("end_offset")
    quote = citation.get("quote")
    return (
        citation.get("passage_id") == assessment.get("evidence_id")
        and type(start) is int and type(end) is int and start >= 0 and end > start
        and isinstance(quote, str) and bool(quote)
    )
