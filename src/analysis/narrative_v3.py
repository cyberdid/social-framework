"""Conservative, deterministic narrative claim and stance candidates (v3).

This module marks text for analyst review; it does not establish whether a
claim is true. Its small Ukrainian/Russian vocabularies deliberately favor
abstention over broad topic or sentiment coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
import re


Commitment = Literal[
    "none",
    "direct_assertion",
    "attributed",
    "quoted",
    "denial",
    "debunk",
    "reports",
    "unclear",
]
Stance = Literal[
    "support", "oppose", "unclear", "not_applicable", "reports_no_stance"
]
LIMITATION_CODES = (
    "deterministic_rules_only",
    "narrow_uk_ru_vocabulary",
    "candidate_only",
    "no_independent_gold_set_validation",
    "truth_unverified",
)
_TORTURE_FORMS = r"(?:пытк\w*|пыта(?:ть|л[аои]?|ли|ет|ют|ю|ем|ете|в|вш\w*|н\w*))"


@dataclass(frozen=True)
class Annotation:
    """A text-level candidate annotation, never a fact verification result."""

    source_text: str
    evidence_text: str
    evidence_start: int
    evidence_end: int
    limitations: tuple[str, ...]
    mentions: tuple[str, ...]
    claim: str | None
    speaker: str | None
    commitment: Commitment
    stance_to_claim: Stance
    truth_state: Literal["unverified"] = "unverified"
    classifier_status: Literal["candidate_only"] = "candidate_only"


# This is an issue/topic vocabulary, not a narrative taxonomy. Returned
# mentions preserve the literal surface form and order found in the text.
_MENTION = re.compile(
    r"\b(?:тцк\w*|нгу|медик\w*|медичн\w*|лікар\w*|врач\w*|"
    r"підвал\w*|подвал\w*|люди|людей|людям|людьми|людях|"
    r"человек\w*|заключ[её]н\w*|ув'язн\w*|увязн\w*|"
    r"поліц\w*|полици\w*|швидк\w*|скор(?:ая|ой|ую|ые|ых)?)\b",
    re.IGNORECASE,
)

# Only these concrete predicates qualify a sentence as a claim candidate.
# Unknown topics, verbs, and languages remain outside this MVP's coverage.
_CLAIM_PREDICATE = re.compile(
    r"\b(?:трим\w*|утрим\w*|держ\w*|удерж\w*|пуска\w*|впуска\w*|"
    rf"допуска\w*|кату\w*|{_TORTURE_FORMS}|побил\w*|избива\w*|загин\w*|"
    r"помер\w*|погиб\w*|убил\w*|убит\w*|прихов\w*|скрыва\w*|"
    r"замовчу\w*|замалчива\w*)\b",
    re.IGNORECASE,
)
_DEBUNK = re.compile(
    r"\b(?:фейк\w*|брехн\w*|ложь|неправд\w*|спростован\w*|"
    r"опровергнут\w*|не відповідає дійсності|не соответствует действительности)\b",
    re.IGNORECASE,
)
_NEGATED_DEBUNK = re.compile(
    r"\bне\s+(?:є\s+)?(?:фейк\w*|брехн\w*|ложь|неправд\w*)\b|"
    r"\bне\s+(?:спростован\w*|опровергнут\w*)\b",
    re.IGNORECASE,
)
_EXPLICIT_DENIAL = re.compile(
    r"\bне\s+(?P<verb>трим\w*|утрим\w*|держ\w*|удерж\w*|пуска\w*|впуска\w*|"
    rf"допуска\w*|кату\w*|{_TORTURE_FORMS}|побил\w*|избива\w*|прихов\w*|скрыва\w*)\b",
    re.IGNORECASE,
)
_ANY_NEGATION = re.compile(
    r"\b(?:не|нет|немає|нема|ні|ни)\b|\b(?:no|not)\b", re.IGNORECASE
)
_ATTRIBUTION = re.compile(
    r"\b(?:за\s+словами|по\s+словам|зі\s+слів|со\s+слов|"
    r"як\s+стверджують|как\s+утверждают|кажуть|говорят|"
    r"за\s+даними|по\s+данным)\b",
    re.IGNORECASE,
)
_REPORTING_FORMS = (
    r"сообщает|сообщают|сообщил|сообщила|сообщили|сообщало|"
    r"повідомляє|повідомляють|повідомляв|повідомляла|повідомляли|"
    r"повідомив|повідомила|повідомили"
)
_REPORTING = re.compile(
    rf"\b(?:{_REPORTING_FORMS})\b",
    re.IGNORECASE,
)
_SPEAKER = re.compile(
    r"\b(?:за\s+словами|по\s+словам|зі\s+слів|со\s+слов|"
    r"за\s+даними|по\s+данным)\s+([^,;:!?«»“”\"]+)",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"\b(?:можливо|может\s+быть|возможно|нібито|якобы|начебто|"
    r"похоже|схоже|ймовірно|вероятно)\b",
    re.IGNORECASE,
)
_QUOTE_MARKS = "«»“”\""
_QUOTED_CLAIM = re.compile(r"«([^»]+)»|“([^”]+)”|\"([^\"]+)\"", re.DOTALL)
_QUESTION = re.compile(r"[?？]")
_ATTRIBUTED_CLAIM = re.compile(
    r"^\s*(?:за\s+словами|по\s+словам|зі\s+слів|со\s+слов|"
    r"за\s+даними|по\s+данным)\s+[^,;:!?]+[,;:]\s*(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_REPORTED_CLAIM = re.compile(
    rf"^\s*(?:{_REPORTING_FORMS})\s*[,;:]?\s*(?:что|що)\s+(.+)$",
    re.IGNORECASE | re.DOTALL,
)
_DEBUNKED_CLAIM = re.compile(
    r"\b(?:что|що)\s+(.+?)\s*(?=[,;:]?\s*[—–-]?\s*(?:это\s+)?"
    r"(?:фейк\w*|брехн\w*|ложь|неправд\w*|спростован\w*|опровергнут\w*)\b)",
    re.IGNORECASE | re.DOTALL,
)


def _speaker(text: str) -> str | None:
    match = _SPEAKER.search(text)
    if not match:
        return None
    value = match.group(1).strip().strip(" \t\r\n.,;:—–-\")").strip()
    return value or None


def _candidate_claim(text: str) -> str:
    """Extract the proposition span when a simple wrapper is recognized."""

    for quoted in _QUOTED_CLAIM.finditer(text):
        quoted_text = next(group for group in quoted.groups() if group is not None).strip()
        if _MENTION.search(quoted_text) and _CLAIM_PREDICATE.search(quoted_text):
            return quoted_text

    if _DEBUNK.search(text) and not _NEGATED_DEBUNK.search(text):
        debunked = _DEBUNKED_CLAIM.search(text)
        if debunked:
            return debunked.group(1).strip(" \t\r\n.,;:—–-")

    reported = _REPORTED_CLAIM.search(text)
    if reported:
        return reported.group(1).strip()

    attributed = _ATTRIBUTED_CLAIM.search(text)
    if attributed:
        return attributed.group(1).strip()

    denial = _EXPLICIT_DENIAL.search(text)
    if denial:
        return _EXPLICIT_DENIAL.sub(lambda match: match.group("verb"), text, count=1)

    return text


def _has_multiple_sentences(text: str) -> bool:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text.strip())
    return sum(bool(part.strip()) for part in parts) > 1


def _annotation(
    source_text: str,
    mentions: tuple[str, ...],
    claim: str | None,
    speaker: str | None,
    commitment: Commitment,
    stance_to_claim: Stance,
) -> Annotation:
    """Build an annotation whose evidence span points into exact input text."""

    return Annotation(
        source_text=source_text,
        evidence_text=source_text,
        evidence_start=0,
        evidence_end=len(source_text),
        limitations=LIMITATION_CODES,
        mentions=mentions,
        claim=claim,
        speaker=speaker,
        commitment=commitment,
        stance_to_claim=stance_to_claim,
    )


def classify(text: str | None) -> Annotation:
    """Return conservative mention, claim, attribution, and stance candidates.

    A ``support`` result means only that the text directly asserts a recognized
    candidate proposition. Quotes, attribution, reporting, negation, debunking,
    and hedging cannot produce ``support``. ``truth_state`` is always
    ``unverified``.
    """

    source_text = text if isinstance(text, str) else ""
    value = source_text.strip()
    mentions = tuple(match.group(0) for match in _MENTION.finditer(value))
    predicate_matches = tuple(_CLAIM_PREDICATE.finditer(value))
    has_candidate_claim = bool(mentions and predicate_matches)
    has_multiple_predicates = has_candidate_claim and len(predicate_matches) > 1
    if not has_candidate_claim:
        claim = None
    elif has_multiple_predicates:
        claim = value
    else:
        claim = _candidate_claim(value)
    speaker = _speaker(value)

    if not claim:
        return _annotation(
            source_text, mentions, None, speaker, "none", "not_applicable"
        )

    if (
        has_multiple_predicates
        or _QUESTION.search(value)
        or _has_multiple_sentences(value)
    ):
        return _annotation(source_text, mentions, claim, speaker, "unclear", "unclear")

    if any(mark in value for mark in _QUOTE_MARKS):
        return _annotation(source_text, mentions, claim, speaker, "quoted", "unclear")

    # A reported or hedged proposition does not show the author's stance.
    if _REPORTING.search(value):
        return _annotation(
            source_text, mentions, claim, speaker, "reports", "reports_no_stance"
        )
    if _ATTRIBUTION.search(value):
        return _annotation(source_text, mentions, claim, speaker, "attributed", "unclear")
    if _HEDGE.search(value):
        return _annotation(source_text, mentions, claim, speaker, "unclear", "unclear")

    if _NEGATED_DEBUNK.search(value):
        return _annotation(source_text, mentions, claim, speaker, "unclear", "unclear")
    if _DEBUNK.search(value):
        return _annotation(source_text, mentions, claim, speaker, "debunk", "oppose")
    if _EXPLICIT_DENIAL.search(value):
        return _annotation(source_text, mentions, claim, speaker, "denial", "oppose")
    if _ANY_NEGATION.search(value):
        return _annotation(source_text, mentions, claim, speaker, "unclear", "unclear")

    return _annotation(
        source_text, mentions, claim, speaker, "direct_assertion", "support"
    )
