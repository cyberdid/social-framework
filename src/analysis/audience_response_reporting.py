"""Hard gates and plain-language rendering for descriptive audience aggregates."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any


class AudienceReportValidationError(ValueError):
    """Audience aggregate is incomplete or could be rendered misleadingly."""


_ROOT_FIELDS = {
    "schema_version", "frame", "unit", "time_bins", "groups", "sensitivity",
    "limitations", "semantic_quality_status", "views",
}
_GROUP_FIELDS = {
    "platform", "period_start_utc", "target_id", "dimension", "label", "numerator",
    "denominator", "proportion", "proportion_basis", "labels_may_overlap", "comments_n",
    "authors_n", "threads_n", "channels_n", "reaction_state_counts", "reaction_counts_by_state",
    "reaction_comments_n", "reaction_metrics_unit", "reaction_request_attempts_n",
    "reaction_request_status_counts", "reaction_comments_not_requested_n",
    "reaction_observed_at_min_utc", "reaction_observed_at_max_utc", "included_records_n",
    "frame_excluded_records_n", "frame_excluded_records_scope", "unit_missing_excluded_n",
    "sampling_frame_limitation",
}
_STATES = {"not_collected", "not_reported", "empty", "available", "current_user_excluded"}
_DIMENSIONS = {"topic", "claim", "stance", "sentiment", "emotion", "hostility"}
_UNQUALIFIED_POPULATION = re.compile(
    r"(?:\b\d{1,3}(?:[.,]\d+)?\s*%|population|public opinion|ukrainians|citizens|residents|majority of people|"
    r"\b(?:українц\w*|росіян\w*|россиян\w*|населен\w*|громадян\w*|граждан\w*|"
    r"жител\w*|мешканц\w*|громадськ\w*\s+думк\w*|суспільн\w*\s+думк\w*|"
    r"общественн\w*\s+мнени\w*|більшіст\w*\s+(?:людей|українц\w*|росіян\w*|громадян\w*)|"
    r"большинств\w*\s+(?:людей|українц\w*|росіян\w*|россиян\w*|граждан\w*|населен\w*)))",
    re.IGNORECASE | re.UNICODE,
)

_STANCE_VIEW_FIELDS = {
    "platform", "period_start_utc", "target_id", "unit", "denominator",
    "denominator_basis", "stance_counts", "labels_may_overlap",
}
_COOCCURRENCE_VIEW_FIELDS = {
    "platform", "target_id", "topic_label", "emotion_label", "unit",
    "numerator", "denominator", "denominator_basis", "labels_may_overlap",
}
_REACTION_RANK_FIELDS = {
    "platform", "rank", "unit", "reaction_count", "denominator", "denominator_basis",
}
_VIEW_FIELDS = {
    "target_stance_distribution", "topic_emotion_cooccurrence",
    "largest_available_reaction_comments",
}
_STANCE_DENOMINATOR_BASIS = {
    "comment": "comments with at least one stance output for this target",
    "author": "authors with at least one stance output for this target",
    "thread": "threads with at least one stance output for this target",
    "channel": "channels with at least one stance output for this target",
}
_COOCCURRENCE_DENOMINATOR_BASIS = (
    "comments with at least one non-abstained topic and emotion output for this same target"
)
_REACTION_RANK_DENOMINATOR_BASIS = (
    "comments whose latest selected reaction snapshot is available in this platform frame; latest current_user_excluded snapshots are excluded"
)


def _require_int(value: Any, field: str, minimum: int = 0) -> None:
    if type(value) is not int or value < minimum:
        raise AudienceReportValidationError(f"{field} must be an integer >= {minimum}")


def _require_text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise AudienceReportValidationError(f"{field} must be a non-empty string")


def validate_audience_aggregate(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate counts, provenance and per-platform semantics before report output."""
    if not isinstance(payload, dict) or set(payload) != _ROOT_FIELDS:
        missing = sorted(_ROOT_FIELDS - set(payload)) if isinstance(payload, dict) else sorted(_ROOT_FIELDS)
        extra = sorted(set(payload) - _ROOT_FIELDS) if isinstance(payload, dict) else []
        raise AudienceReportValidationError(f"aggregate fields mismatch; missing={missing}, extra={extra}")
    schema_version = payload["schema_version"]
    if schema_version not in {"1.0", "1.1"}:
        raise AudienceReportValidationError("unsupported audience aggregate schema_version")
    frame = payload["frame"]
    required_frame = {
        "sampling_frame", "platforms", "comments_input", "comments_included", "comments_excluded",
        "author_cap_dropped_group_memberships", "interpretation_rows_input", "model_id", "model_version",
    }
    if not isinstance(frame, dict) or set(frame) != required_frame:
        raise AudienceReportValidationError("frame fields mismatch or missing model provenance")
    if frame["sampling_frame"] != "observed_selected_comments":
        raise AudienceReportValidationError("only the explicit selected-comment frame is supported")
    _require_int(frame["comments_input"], "frame.comments_input")
    _require_int(frame["comments_included"], "frame.comments_included")
    _require_int(frame["author_cap_dropped_group_memberships"], "frame.author_cap_dropped_group_memberships")
    _require_int(frame["interpretation_rows_input"], "frame.interpretation_rows_input")
    if not isinstance(frame["comments_excluded"], dict) or set(frame["comments_excluded"]) != {"exact_duplicate", "candidate_coordination"}:
        raise AudienceReportValidationError("frame.comments_excluded must declare both sensitivity exclusions")
    for key, value in frame["comments_excluded"].items():
        _require_int(value, f"frame.comments_excluded.{key}")
    groups = payload["groups"]
    if not isinstance(groups, list):
        raise AudienceReportValidationError("groups must be a list")
    if groups:
        for field in ("model_id", "model_version"):
            _require_text(frame[field], f"frame.{field} provenance")
    elif any(frame[field] is not None for field in ("model_id", "model_version")):
        _require_text(frame["model_id"], "frame.model_id provenance")
        _require_text(frame["model_version"], "frame.model_version provenance")
    if payload["semantic_quality_status"] != "not_evaluated":
        raise AudienceReportValidationError("semantic quality must remain not_evaluated without an independent local gold set")
    if payload["unit"] not in {"comment", "author", "thread", "channel"}:
        raise AudienceReportValidationError("unsupported aggregation unit")
    width = payload["time_bins"]
    if width is not None:
        _require_int(width, "time_bins", minimum=1)
    sensitivity_keys = {"deduplicate_exact", "cap_author_contribution", "exclude_candidate_coordination"}
    sensitivity = payload["sensitivity"]
    if not isinstance(sensitivity, dict) or set(sensitivity) != sensitivity_keys or any(type(v) is not bool for v in sensitivity.values()):
        raise AudienceReportValidationError("sensitivity must declare three boolean scenarios")
    if not isinstance(payload["limitations"], list) or not payload["limitations"]:
        raise AudienceReportValidationError("limitations must be a non-empty list")
    platforms = frame["platforms"]
    if not isinstance(platforms, list) or any(not isinstance(p, str) or not p for p in platforms) or len(platforms) != len(set(platforms)):
        raise AudienceReportValidationError("frame.platforms must be unique non-empty strings")
    group_platforms = set()
    seen = set()
    for index, group in enumerate(groups):
        prefix = f"groups[{index}]"
        if not isinstance(group, dict) or set(group) != _GROUP_FIELDS:
            raise AudienceReportValidationError(f"{prefix} fields mismatch")
        for field in ("platform", "target_id", "dimension", "label"):
            _require_text(group[field], f"{prefix}.{field}")
        if group["platform"].casefold() in {"all", "cross-platform", "cross_platform", "multi-platform", "multi_platform"}:
            raise AudienceReportValidationError(f"{prefix}.platform cannot pool incompatible platform semantics")
        group_platforms.add(group["platform"])
        if group["dimension"] not in _DIMENSIONS:
            raise AudienceReportValidationError(f"{prefix}.dimension is unsupported")
        if _UNQUALIFIED_POPULATION.search(group["label"]):
            raise AudienceReportValidationError(f"{prefix}.label contains unqualified percentage or population language")
        _require_int(group["numerator"], f"{prefix}.numerator")
        _require_int(group["denominator"], f"{prefix}.denominator", minimum=1)
        if group["numerator"] > group["denominator"]:
            raise AudienceReportValidationError(f"{prefix}.numerator cannot exceed denominator")
        proportion = group["proportion"]
        if isinstance(proportion, bool) or not isinstance(proportion, (int, float)) or not math.isfinite(proportion):
            raise AudienceReportValidationError(f"{prefix}.proportion must be finite")
        if not math.isclose(proportion, group["numerator"] / group["denominator"], rel_tol=0.0, abs_tol=1e-12):
            raise AudienceReportValidationError(f"{prefix}.proportion disagrees with numerator/denominator")
        for field in ("comments_n", "authors_n", "threads_n", "channels_n", "reaction_comments_n",
                      "reaction_request_attempts_n", "reaction_comments_not_requested_n", "included_records_n",
                      "frame_excluded_records_n", "unit_missing_excluded_n"):
            _require_int(group[field], f"{prefix}.{field}")
        if group["reaction_metrics_unit"] != "comment":
            raise AudienceReportValidationError(f"{prefix}.reaction metrics must be explicitly comment-level")
        if group["reaction_comments_n"] != group["comments_n"]:
            raise AudienceReportValidationError(f"{prefix}.reaction comment denominator must match comments_n")
        if not isinstance(group["labels_may_overlap"], bool) or not group["labels_may_overlap"]:
            raise AudienceReportValidationError(f"{prefix}.labels_may_overlap must be explicit")
        if not isinstance(group["proportion_basis"], str) or not group["proportion_basis"].strip():
            raise AudienceReportValidationError(f"{prefix}.proportion_basis is required")
        state_counts = group["reaction_state_counts"]
        if not isinstance(state_counts, dict) or set(state_counts) != _STATES:
            raise AudienceReportValidationError(f"{prefix}.reaction_state_counts must include every data-availability state")
        for state, value in state_counts.items():
            _require_int(value, f"{prefix}.reaction_state_counts.{state}")
        if sum(state_counts.values()) != group["reaction_comments_n"]:
            raise AudienceReportValidationError(f"{prefix}.reaction state coverage does not match its comment denominator")
        reactions = group["reaction_counts_by_state"]
        if not isinstance(reactions, dict) or set(reactions) != {"available", "current_user_excluded"}:
            raise AudienceReportValidationError(f"{prefix}.reaction_counts_by_state must separate complete and partial vectors")
        for state, values in reactions.items():
            if not isinstance(values, dict):
                raise AudienceReportValidationError(f"{prefix}.reaction counts for {state} must be an object")
            for key, value in values.items():
                _require_text(key, f"{prefix}.reaction_counts_by_state.{state} key")
                _require_int(value, f"{prefix}.reaction_counts_by_state.{state}.{key}")
        requests = group["reaction_request_status_counts"]
        request_statuses = {"ok", "request_failed"}
        if schema_version == "1.1":
            request_statuses |= {"no_update", "wrong_peer_update"}
        if not isinstance(requests, dict) or set(requests) != request_statuses:
            raise AudienceReportValidationError(f"{prefix}.reaction_request_status_counts is malformed")
        for key, value in requests.items():
            _require_int(value, f"{prefix}.reaction_request_status_counts.{key}")
        if sum(requests.values()) != group["reaction_request_attempts_n"]:
            raise AudienceReportValidationError(f"{prefix}.request attempts disagree with status counts")
        if group["reaction_comments_not_requested_n"] > group["reaction_comments_n"]:
            raise AudienceReportValidationError(f"{prefix}.not-requested count exceeds reaction comment denominator")
        if group["frame_excluded_records_scope"] != "whole_input_frame":
            raise AudienceReportValidationError(f"{prefix}.excluded count scope must be explicit")
        if not isinstance(group["sampling_frame_limitation"], str) or "not a population estimate" not in group["sampling_frame_limitation"].lower():
            raise AudienceReportValidationError(f"{prefix}.sampling_frame_limitation must disclaim population inference")
        for field in ("period_start_utc", "reaction_observed_at_min_utc", "reaction_observed_at_max_utc"):
            value = group[field]
            if value is not None:
                if not isinstance(value, str):
                    raise AudienceReportValidationError(f"{prefix}.{field} must be UTC datetime or null")
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError as exc:
                    raise AudienceReportValidationError(f"{prefix}.{field} must be ISO datetime") from exc
                if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
                    raise AudienceReportValidationError(f"{prefix}.{field} must be UTC")
        identity = (group["platform"], group["period_start_utc"], group["target_id"], group["dimension"], group["label"])
        if identity in seen:
            raise AudienceReportValidationError(f"duplicate audience group: {identity}")
        seen.add(identity)
    if not group_platforms <= set(platforms):
        raise AudienceReportValidationError("every rendered platform group must belong to frame.platforms")
    _validate_views(payload["views"], platforms, payload["unit"])
    return payload


def _validate_views(views: Any, platforms: list[str], aggregation_unit: str) -> None:
    if not isinstance(views, dict) or set(views) != _VIEW_FIELDS:
        raise AudienceReportValidationError("views must contain target stance, topic-emotion, and reaction ranking views")
    for name in _VIEW_FIELDS:
        if not isinstance(views[name], list):
            raise AudienceReportValidationError(f"views.{name} must be a list")

    for index, row in enumerate(views["target_stance_distribution"]):
        prefix = f"views.target_stance_distribution[{index}]"
        if not isinstance(row, dict) or set(row) != _STANCE_VIEW_FIELDS:
            raise AudienceReportValidationError(f"{prefix} fields mismatch")
        _require_text(row["platform"], f"{prefix}.platform")
        _require_text(row["target_id"], f"{prefix}.target_id")
        if row["platform"] not in platforms:
            raise AudienceReportValidationError(f"{prefix}.platform is outside frame.platforms")
        if row["unit"] != aggregation_unit:
            raise AudienceReportValidationError(f"{prefix}.unit must match the aggregate unit")
        _require_int(row["denominator"], f"{prefix}.denominator", minimum=1)
        if row["denominator_basis"] != _STANCE_DENOMINATOR_BASIS[aggregation_unit]:
            raise AudienceReportValidationError(f"{prefix}.denominator_basis does not define the stance denominator")
        counts = row["stance_counts"]
        if not isinstance(counts, dict) or not counts:
            raise AudienceReportValidationError(f"{prefix}.stance_counts must be a non-empty label-count map")
        for label, count in counts.items():
            _require_text(label, f"{prefix}.stance_counts label")
            if _UNQUALIFIED_POPULATION.search(label):
                raise AudienceReportValidationError(f"{prefix}.stance_counts contains population language")
            _require_int(count, f"{prefix}.stance_counts.{label}")
            if count > row["denominator"]:
                raise AudienceReportValidationError(f"{prefix}.stance_counts.{label} exceeds its denominator")
        if row["labels_may_overlap"] is not True:
            raise AudienceReportValidationError(f"{prefix}.labels_may_overlap must be explicit")
        _validate_view_period(row.get("period_start_utc"), prefix)

    for index, row in enumerate(views["topic_emotion_cooccurrence"]):
        prefix = f"views.topic_emotion_cooccurrence[{index}]"
        if not isinstance(row, dict) or set(row) != _COOCCURRENCE_VIEW_FIELDS:
            raise AudienceReportValidationError(f"{prefix} fields mismatch")
        for field in ("platform", "target_id", "topic_label", "emotion_label"):
            _require_text(row[field], f"{prefix}.{field}")
        if row["platform"] not in platforms:
            raise AudienceReportValidationError(f"{prefix}.platform is outside frame.platforms")
        if row["unit"] != "comment":
            raise AudienceReportValidationError(f"{prefix}.unit must be comment")
        if row["denominator_basis"] != _COOCCURRENCE_DENOMINATOR_BASIS:
            raise AudienceReportValidationError(f"{prefix}.denominator_basis must condition on both dimensions for the same target")
        for field in ("numerator", "denominator"):
            _require_int(row[field], f"{prefix}.{field}", minimum=1)
        if row["numerator"] > row["denominator"]:
            raise AudienceReportValidationError(f"{prefix}.numerator cannot exceed its denominator")
        for field in ("topic_label", "emotion_label"):
            if row[field] == "abstain" or _UNQUALIFIED_POPULATION.search(row[field]):
                raise AudienceReportValidationError(f"{prefix}.{field} is not a renderable co-occurrence label")
        if row["labels_may_overlap"] is not True:
            raise AudienceReportValidationError(f"{prefix}.labels_may_overlap must be explicit")

    per_platform_ranks: dict[str, list[int]] = {}
    per_platform_denominators: dict[str, int] = {}
    for index, row in enumerate(views["largest_available_reaction_comments"]):
        prefix = f"views.largest_available_reaction_comments[{index}]"
        if not isinstance(row, dict) or set(row) != _REACTION_RANK_FIELDS:
            raise AudienceReportValidationError(f"{prefix} fields mismatch")
        _require_text(row["platform"], f"{prefix}.platform")
        if row["platform"] not in platforms:
            raise AudienceReportValidationError(f"{prefix}.platform is outside frame.platforms")
        _require_int(row["rank"], f"{prefix}.rank", minimum=1)
        if row["unit"] != "comment":
            raise AudienceReportValidationError(f"{prefix}.unit must be comment")
        _require_int(row["reaction_count"], f"{prefix}.reaction_count")
        _require_int(row["denominator"], f"{prefix}.denominator", minimum=1)
        if row["denominator_basis"] != _REACTION_RANK_DENOMINATOR_BASIS:
            raise AudienceReportValidationError(f"{prefix}.denominator_basis must identify complete snapshots")
        per_platform_ranks.setdefault(row["platform"], []).append(row["rank"])
        previous = per_platform_denominators.setdefault(row["platform"], row["denominator"])
        if previous != row["denominator"]:
            raise AudienceReportValidationError(f"{prefix}.denominator changes within a platform ranking")
    for platform, ranks in per_platform_ranks.items():
        if ranks != list(range(1, len(ranks) + 1)):
            raise AudienceReportValidationError(f"views reaction ranks must restart at 1 and be contiguous per platform ({platform})")
        if len(ranks) > 10 or len(ranks) > per_platform_denominators[platform]:
            raise AudienceReportValidationError(f"views reaction ranking exceeds the platform-local top ten or denominator")


def _validate_view_period(value: Any, prefix: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise AudienceReportValidationError(f"{prefix}.period_start_utc must be UTC datetime or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AudienceReportValidationError(f"{prefix}.period_start_utc must be ISO datetime") from exc
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        raise AudienceReportValidationError(f"{prefix}.period_start_utc must be UTC")


def render_audience_response_markdown(payload: dict[str, Any]) -> str:
    """Render validated aggregate values, not free-form model conclusions."""
    validate_audience_aggregate(payload)
    frame = payload["frame"]
    lines = [
        "# Аналіз реакції онлайн-аудиторії",
        "",
        "Показники описують зібрані коментарі. Вони не вимірюють громадську думку населення й не підтверджують істинність тверджень.",
        "",
        f"- Одиниця підрахунку: `{payload['unit']}`.",
        f"- Коментарів у вхідній рамці: {frame['comments_input']}; залишено після вибраних сценаріїв: {frame['comments_included']}.",
        f"- Модель міток: `{frame['model_id'] or 'not available'}` / `{frame['model_version'] or 'not available'}`.",
        "- Семантичну точність міток для локального корпусу не оцінено; утримання моделі показані як окремі мітки.",
        "",
        "Кожна частка має власний чисельник і знаменник. Реакції підраховано на рівні коментарів; їх не можна порівнювати між платформами як однакові сигнали підтримки.",
        "",
    ]
    platforms = sorted(payload["frame"]["platforms"])
    for platform in platforms:
        lines.extend([f"## Платформа: {platform}", "", "| Період UTC | Ціль | Вимір | Мітка | Частка одиниць | Коментарі з міткою | Стани реакцій / запитів |", "|---|---|---|---|---:|---:|---|"])
        for row in payload["groups"]:
            if row["platform"] != platform:
                continue
            state = ", ".join(f"{key}={value}" for key, value in row["reaction_state_counts"].items() if value)
            requests = row["reaction_request_status_counts"]
            no_update = requests.get("no_update", "not_recorded")
            wrong_peer_update = requests.get("wrong_peer_update", "not_recorded")
            request_state = (f"refresh: ok={requests['ok']}, no_update={no_update}, "
                             f"wrong_peer_update={wrong_peer_update}, request_failed={requests['request_failed']}, "
                             f"not_requested={row['reaction_comments_not_requested_n']}")
            fraction = f"{row['numerator']}/{row['denominator']} ({row['proportion']:.1%})"
            values = [row["period_start_utc"] or "all", row["target_id"], row["dimension"], row["label"], fraction,
                      str(row["comments_n"]), f"{state or 'no snapshot'}; {request_state}"]
            safe = [str(value).replace("|", "\\|").replace("\n", " ") for value in values]
            lines.append("| " + " | ".join(safe) + " |")
        lines.append("")
        stance_rows = [row for row in payload["views"]["target_stance_distribution"] if row["platform"] == platform]
        lines.extend(["### Target-by-stance distribution", "", "Counts use the selected aggregate unit; each denominator is units with at least one stance output for that target. Stance labels may overlap.", "", "| Period UTC | Target | Unit | Denominator | Stance label counts |", "|---|---|---|---:|---|"])
        for row in stance_rows:
            counts = ", ".join(f"{label}={count}" for label, count in sorted(row["stance_counts"].items()))
            values = [row["period_start_utc"] or "all", row["target_id"], row["unit"], str(row["denominator"]), counts]
            lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in values) + " |")
        if not stance_rows:
            lines.append("| — | — | — | 0 | no stance outputs |")
        cooccurrence_rows = [row for row in payload["views"]["topic_emotion_cooccurrence"] if row["platform"] == platform]
        lines.extend(["", "### Topic and emotion co-occurrence", "", "Each cell counts comments with both topic and emotion outputs for the same target. The denominator is all such comments for that platform and target; multilabel cells may overlap.", "", "| Target | Topic | Emotion | Comments with both labels | Denominator |", "|---|---|---|---:|---:|"])
        for row in cooccurrence_rows:
            values = [row["target_id"], row["topic_label"], row["emotion_label"], str(row["numerator"]), str(row["denominator"])]
            lines.append("| " + " | ".join(str(value).replace("|", "\\|").replace("\n", " ") for value in values) + " |")
        if not cooccurrence_rows:
            lines.append("| — | — | — | 0 | 0 |")
        reaction_rows = [row for row in payload["views"]["largest_available_reaction_comments"] if row["platform"] == platform]
        lines.extend(["", "### Comments with the largest available reaction counts", "", "Rank is within this platform among comments whose latest selected reaction snapshot is `available`. The denominator is all such comments in this platform frame. Counts are not exposure-adjusted engagement or population support; comment text and identifiers are omitted.", "", "| Rank | Reactions | Unit | Available-comment denominator |", "|---:|---:|---|---:|"])
        for row in reaction_rows:
            lines.append(f"| {row['rank']} | {row['reaction_count']} | {row['unit']} | {row['denominator']} |")
        if not reaction_rows:
            lines.append("| — | — | comment | 0 |")
        lines.append("")
    lines.extend([
        "## Як читати стани реакцій",
        "",
        "## Ворожість і розбіжність позицій",
        "",
        "Мітки спостережуваної ворожої мови та мітки позиції щодо цілі показані як окремі виміри. Ворожість сама по собі не є незгодою з позицією; з цих міток не обчислюється загальний бал поляризації.",
        "",
        "`not_collected` означає, що стан реакцій не спостерігався; `not_reported` — отримане повідомлення не містило об'єкта реакцій; `empty` — об'єкт був, але список реакцій був порожнім. Відсутній MTProto update (`no_update`) або update з іншим peer (`wrong_peer_update`) не є спостереженням реакцій і не замінюють попередній snapshot. `current_user_excluded` — неповний набір, який може не враховувати реакцію поточного акаунта. `request_failed` означає помилку запиту; це не означає, що реакцій немає.",
        "",
        "## Обмеження", "",
    ])
    lines.extend(f"- {item}" for item in payload["limitations"])
    lines.extend(["", "Реакційні лічильники є платформним зворотним зв’язком, не голосуванням за кожне твердження й не свідченням його істинності.", ""])
    return "\n".join(lines)
