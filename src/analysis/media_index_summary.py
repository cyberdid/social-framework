"""Summarize a technical media index without treating observations as verification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"row {number} must be a JSON object")
            rows.append(row)
    return rows


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def summarize(records: list[dict]) -> dict:
    status_counts = Counter()
    platform_counts = Counter()
    role_counts = Counter()
    assets = {}
    model_observations = 0
    for record in records:
        source = record.get("source") or {}
        index = record.get("media_index") or {}
        facts = index.get("verified_media_facts") or {}
        status_counts[index.get("status", "missing_status")] += 1
        platform_counts[source.get("platform", "unknown")] += 1
        role_counts[source.get("role_in_pilot", source.get("role", "unknown"))] += 1
        model_observations += len(source.get("model_observations") or [])
        sha = facts.get("sha256")
        if sha:
            assets.setdefault(sha, {
                "sha256": sha,
                "size_bytes": facts.get("size_bytes"),
                "status": index.get("status", "missing_status"),
                "decode_status": (index.get("decode_check") or {}).get("status", "not_requested"),
                "probe_status": (index.get("probe") or {}).get("status", "missing"),
                "streams": (facts.get("ffprobe") or {}).get("streams", []),
                "duration": ((facts.get("ffprobe") or {}).get("format") or {}).get("duration"),
            })
    unique_assets = list(assets.values())
    stream_counts = Counter(stream.get("codec_type", "unknown") for asset in unique_assets
                            for stream in asset["streams"])
    decode_counts = Counter(asset["decode_status"] for asset in unique_assets)
    probe_counts = Counter(asset["probe_status"] for asset in unique_assets)
    return {
        "schema_version": "1.0",
        "record_count": len(records),
        "status_counts": dict(sorted(status_counts.items())),
        "platform_record_counts": dict(sorted(platform_counts.items())),
        "role_record_counts": dict(sorted(role_counts.items())),
        "unique_asset_count": len(unique_assets),
        "unique_asset_bytes": sum(int(asset["size_bytes"] or 0) for asset in unique_assets),
        "decode_status_counts_by_unique_asset": dict(sorted(decode_counts.items())),
        "probe_status_counts_by_unique_asset": dict(sorted(probe_counts.items())),
        "stream_type_counts_by_unique_asset": dict(sorted(stream_counts.items())),
        "model_observation_count_in_source_records": model_observations,
        "verification_claims_added": 0,
        "interpretation": "Technical file and stream checks do not establish event truth, recording time, location, identity, or caption accuracy.",
    }


def render_markdown(summary: dict, input_path: Path, input_sha256: str) -> str:
    try:
        resolved = input_path.expanduser().resolve()
        home = Path.home().resolve()
        display_path = "~/" + resolved.relative_to(home).as_posix()
    except (OSError, RuntimeError, ValueError):
        display_path = str(input_path)
    lines = [
        "# Звіт технічної перевірки відео",
        "",
        "Цей звіт підсумовує лише технічну цілісність і доступність потоків. Він не підтверджує, що відео зняте під час досліджуваного інциденту, у заявленому місці чи дату, або що підпис до нього точний.",
        "",
        "## Результат",
        "",
        f"- Записів у маніфесті: **{summary['record_count']}**.",
        f"- Унікальних медіа за SHA-256: **{summary['unique_asset_count']}**.",
        f"- Обсяг унікальних індексованих байтів: **{summary['unique_asset_bytes']}**.",
        f"- Повне декодування унікальних файлів: `{json.dumps(summary['decode_status_counts_by_unique_asset'], ensure_ascii=False)}`.",
        f"- Статус ffprobe: `{json.dumps(summary['probe_status_counts_by_unique_asset'], ensure_ascii=False)}`.",
        f"- Типи потоків унікальних файлів: `{json.dumps(summary['stream_type_counts_by_unique_asset'], ensure_ascii=False)}`.",
        f"- Кандидатних спостережень моделі в рядках джерела: **{summary['model_observation_count_in_source_records']}**; цей індекс не додає жодного статусу перевірки тверджень.",
        "",
        "## Покриття за платформою",
        "",
        "| Платформа | Записів |",
        "|---|---:|",
    ]
    for name, count in summary["platform_record_counts"].items():
        lines.append(f"| {name} | {count} |")
    lines.extend([
        "",
        "## Provenance",
        "",
        f"- Індекс: `{display_path}`",
        f"- SHA-256 маніфесту: `{input_sha256}`",
        "- Побудовано `src/analysis/media_index_summary.py` з JSONL, створеного `src/media_pipeline.py`.",
        "- SHA-256 файлу засвідчує тотожність байтів. Метадані контейнера та успішне декодування не засвідчують дату/геолокацію чи правдивість змісту.",
        "",
    ])
    return "\n".join(lines)


def write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--markdown", required=True, type=Path)
    args = parser.parse_args(argv)
    input_path = args.input.resolve()
    json_path, markdown_path = args.json.resolve(), args.markdown.resolve()
    if json_path == input_path or markdown_path == input_path or json_path == markdown_path:
        raise SystemExit("input and summary outputs must have distinct paths")
    records = read_jsonl(args.input)
    summary = summarize(records)
    write_atomic(args.json, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    write_atomic(args.markdown, render_markdown(summary, args.input, sha256_path(args.input)))
    print(f"summarized {summary['record_count']} media-index rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
