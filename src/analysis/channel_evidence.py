"""Формує записи-кандидати доказового профілю каналів.

Маркери інфраструктури є спостереженнями для перевірки. Вони не встановлюють
походження каналу, політичну орієнтацію, власність, державний контроль чи
участь в операції.
"""
from __future__ import annotations

import hashlib
import argparse
import csv
import json
import os
import re
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit


PROFILE_MARKERS = {
    "knd.gov.ru": re.compile(r"\bknd\.gov\.ru\b", re.I),
    "gosuslugi.ru": re.compile(r"\bgosuslugi(?:\.ru)?\b", re.I),
    "max.ru": re.compile(r"\bmax\.ru\b", re.I),
    "vk.com": re.compile(r"\bvk\.com\b", re.I),
    "dzen.ru": re.compile(r"\bdzen\.ru\b", re.I),
    "rutube.ru": re.compile(r"\brutube(?:\.ru)?\b", re.I),
    "ok.ru": re.compile(r"\bok\.ru\b", re.I),
    "roskomnadzor": re.compile(r"роскомнадзор|\bРКН\b", re.I),
}
KNOWN_DOMAINS = {"vk.com", "max.ru", "dzen.ru", "rutube.ru", "ok.ru"}


def _aware_iso(value: str, field_name: str = "as_of") -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.isoformat().replace("+00:00", "Z")


def _hostname(url: str) -> str | None:
    raw = str(url).strip()
    if not raw:
        return None
    parsed = urlsplit(raw if "://" in raw else "https://" + raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return None
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    return host


def _domain_feature(host: str) -> str | None:
    if host in KNOWN_DOMAINS:
        return host
    if host.endswith(".ru"):
        return "*.ru"
    if host.endswith(".su"):
        return "*.su"
    if host.endswith(".xn--p1ai"):
        return "*.рф"
    return None


def extract_infrastructure_features(profile_text: str, links: list[str]) -> list[dict]:
    """Return counted observations without assigning origin or orientation."""
    features: list[dict] = []
    profile = profile_text or ""
    for label, pattern in PROFILE_MARKERS.items():
        count = len(pattern.findall(profile))
        if count:
            features.append({"feature": f"profile_marker:{label}", "count": count,
                             "basis": "public_profile_text"})

    domains = Counter()
    for link in links or []:
        host = _hostname(link)
        feature = _domain_feature(host) if host else None
        if feature:
            domains[feature] += 1
    for domain, count in sorted(domains.items()):
        features.append({"feature": f"link_domain:{domain}", "count": count,
                         "basis": "observed_public_outbound_links"})
    return features


def _validate_attribution_axis(axis: dict | None, name: str) -> dict:
    value = dict(axis or {"value": "unknown", "status": "unknown", "evidence_ids": []})
    evidence_ids = list(value.get("evidence_ids") or [])
    if value.get("value") not in (None, "unknown"):
        if not evidence_ids:
            raise ValueError(f"non-unknown {name} requires evidence IDs")
        scope = value.get("scope")
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError(f"non-unknown {name} requires a non-empty scope")
        interval = value.get("validity_interval")
        if not isinstance(interval, dict) or "start" not in interval or "end" not in interval:
            raise ValueError(f"non-unknown {name} requires a validity_interval with start and end")
        start = _aware_iso(interval["start"], f"{name} validity_interval.start")
        end = (_aware_iso(interval["end"], f"{name} validity_interval.end")
               if interval["end"] is not None else None)
        if end is not None:
            start_datetime = datetime.fromisoformat(start.replace("Z", "+00:00"))
            end_datetime = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if end_datetime <= start_datetime:
                raise ValueError(f"{name} validity_interval.end must be later than start")
        value["validity_interval"] = {"start": start, "end": end}
    value["evidence_ids"] = evidence_ids
    return value


def build_channel_evidence(
    channel: str,
    profile_text: str,
    links: list[str],
    source_refs: list[str],
    as_of: str,
    *,
    origin: dict | None = None,
    entity_type: dict | None = None,
    operator: dict | None = None,
    content_orientation: dict | None = None,
    legacy_group: str | None = None,
) -> dict:
    """Create a conservative, versioned channel evidence record.

    Manually supplied positive attributions require evidence receipts, a scope,
    and a validity interval. `as_of` records generation time and is not the
    validity interval. Automatic infrastructure observations only set
    `infrastructure_candidate`.
    """
    handle = str(channel).strip().lstrip("@").lower()
    if not handle:
        raise ValueError("channel identifier is required")
    timestamp = _aware_iso(as_of)
    refs = sorted(set(str(ref) for ref in (source_refs or []) if str(ref)))
    features = extract_infrastructure_features(profile_text, links)
    link_count = len(links or [])
    infrastructure_link_count = sum(
        feature["count"] for feature in features if feature["feature"].startswith("link_domain:")
    )
    profile_signal = any(f["feature"].startswith("profile_marker:") for f in features)
    link_ratio_signal = link_count >= 5 and infrastructure_link_count / link_count >= 0.30
    infrastructure_candidate = bool(profile_signal or link_ratio_signal)

    origin_axis = _validate_attribution_axis(origin, "origin")
    entity_axis = _validate_attribution_axis(entity_type, "entity_type")
    operator_axis = _validate_attribution_axis(operator, "operator")
    orientation_axis = _validate_attribution_axis(content_orientation, "content_orientation")
    record_material = "\x1f".join((handle, timestamp, *refs))
    record_id = hashlib.sha256(record_material.encode("utf-8")).hexdigest()

    return {
        "schema_version": "1.0",
        "record_type": "ChannelEvidence",
        "record_id": record_id,
        "channel": handle,
        "as_of": timestamp,
        "source_refs": refs,
        "origin": origin_axis,
        "entity_type": entity_axis,
        "operator": operator_axis,
        "content_orientation": orientation_axis,
        "infrastructure_features": features,
        "infrastructure_candidate": infrastructure_candidate,
        "network_relations": [],
        "operational_attribution": {"status": "unverified", "evidence_ids": []},
        "confidence": "not_assessed",
        "contrary_evidence": [],
        "legacy_group": legacy_group,
        "limitations": [
            "Інфраструктурні маркери не встановлюють походження, власність, орієнтацію, державний контроль чи участь в операції.",
            "Цей запис містить набір кандидатних ознак, а не калібровану ймовірність чи висновок атрибуції.",
        ],
    }


def _jsonl(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError(f"{path}:{line_no}: expected a JSON object")
            records.append(item)
    return records


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_group_mapping(path: Path) -> dict[str, str]:
    groups = {}
    if not path or not path.is_file():
        return groups
    with path.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(line for line in stream if not line.lstrip().startswith("#"))
        for row in reader:
            channel, group = row.get("channel"), row.get("group")
            if channel and group:
                groups.setdefault(channel.lower().lstrip("@"), group)
    return groups


def _legacy_group_map(manual_paths: list[Path], auto_paths: list[Path]) -> dict[str, str]:
    """Merge mapping roles with manual values overriding automatic values."""
    groups = {}
    for path in auto_paths:
        for channel, group in _read_group_mapping(path).items():
            groups.setdefault(channel, group)
    manual_channels = set()
    for path in manual_paths:
        for channel, group in _read_group_mapping(path).items():
            if channel not in manual_channels:
                groups[channel] = group
                manual_channels.add(channel)
    return groups


def _group_mapping_refs(manual_paths: list[Path], auto_paths: list[Path]) -> list[str]:
    refs = []
    root = Path.cwd().resolve()
    for role, paths in (("manual", manual_paths), ("auto", auto_paths)):
        for priority, path in enumerate(paths, start=1):
            if not path or not path.is_file():
                continue
            resolved = path.resolve()
            try:
                label = resolved.relative_to(root).as_posix()
            except ValueError:
                label = resolved.name
            refs.append(
                f"group_mapping:{role}:priority:{priority}:{label}:sha256:{_sha256_path(resolved)}"
            )
    return refs


def build_channel_records(channels: list[dict], posts: list[dict], *, as_of: str,
                          channel_snapshot_ref: str, posts_snapshot_ref: str,
                          legacy_groups: dict[str, str] | None = None,
                          provenance_refs: list[str] | None = None) -> list[dict]:
    links_by_channel: dict[str, list[str]] = {}
    for post in posts:
        channel = str(post.get("channel") or "").strip().lower().lstrip("@")
        if not channel:
            continue
        links = links_by_channel.setdefault(channel, [])
        links.extend(url for url in (post.get("links") or [])
                     if "t.me/" not in str(url).lower())
    result = []
    for item in channels:
        handle = str(item.get("channel") or "").strip().lower().lstrip("@")
        if not handle:
            continue
        source_refs = [channel_snapshot_ref, posts_snapshot_ref,
                       f"channel:{handle}:profile_snapshot", *(provenance_refs or [])]
        profile = " ".join(str(item.get(key) or "") for key in ("title", "description"))
        result.append(build_channel_evidence(
            handle, profile, links_by_channel.get(handle, []), source_refs, as_of,
            legacy_group=(legacy_groups or {}).get(handle),
        ))
    return sorted(result, key=lambda row: row["channel"])


def export_channel_evidence(channels_path: Path, posts_path: Path, output_path: Path,
                            groups_paths: list[Path] | None = None, as_of: str | None = None,
                            *, manual_groups_paths: list[Path] | None = None,
                            auto_groups_paths: list[Path] | None = None) -> int:
    # `groups_paths` remains a compatibility alias for manual mappings.
    manual_paths = [*(groups_paths or []), *(manual_groups_paths or [])]
    auto_paths = list(auto_groups_paths or [])
    input_paths = [channels_path.resolve(), posts_path.resolve()]
    input_paths.extend(p.resolve() for p in [*manual_paths, *auto_paths] if p and p.exists())
    output_resolved = output_path.resolve()
    if output_resolved in input_paths:
        raise ValueError("output path must not overwrite an input file")
    channels = _jsonl(channels_path)
    posts = _jsonl(posts_path)
    timestamp = _aware_iso(as_of or datetime.now(timezone.utc).isoformat())
    source_refs = [f"sha256:{_sha256_path(channels_path)}", f"sha256:{_sha256_path(posts_path)}"]
    records = build_channel_records(channels, posts, as_of=timestamp,
                                    channel_snapshot_ref=f"channel_snapshot:{source_refs[0]}",
                                    posts_snapshot_ref=f"posts_snapshot:{source_refs[1]}",
                                    legacy_groups=_legacy_group_map(manual_paths, auto_paths),
                                    provenance_refs=_group_mapping_refs(manual_paths, auto_paths))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output_path.name}.", suffix=".tmp",
                                     dir=output_path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for row in records:
                stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, output_path)
    finally:
        Path(temp_name).unlink(missing_ok=True)
    return len(records)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channels", required=True, type=Path, help="Вхідний знімок каналів Telegram у JSONL")
    parser.add_argument("--posts", required=True, type=Path, help="Вхідний знімок дописів Telegram у JSONL")
    parser.add_argument("--output", required=True, type=Path, help="Шлях до вихідних записів ChannelEvidence у JSONL")
    parser.add_argument("--as-of", help="Час створення запису в ISO 8601 із часовим поясом; типово — поточний UTC")
    parser.add_argument("--manual-groups", type=Path, action="append", default=[],
                        help="CSV із ручними мітками груп; має пріоритет над автоматичними")
    parser.add_argument("--auto-groups", type=Path, action="append", default=[],
                        help="CSV із автоматично визначеними мітками груп")
    parser.add_argument("--groups", type=Path, action="append", default=[],
                        help="Застарілий сумісний параметр; трактує файл як ручну мапу груп")
    args = parser.parse_args(argv)
    count = export_channel_evidence(
        args.channels, args.posts, args.output, args.groups, args.as_of,
        manual_groups_paths=args.manual_groups, auto_groups_paths=args.auto_groups,
    )
    print(f"Експортовано записів-кандидатів: {count}; файл: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
