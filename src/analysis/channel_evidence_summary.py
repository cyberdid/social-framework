"""Підсумовує версійовані експорти ChannelEvidence JSONL без оцінювання атрибуції."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def summarize(snapshots: dict[str, list[dict]]) -> dict:
    records = [record for batch in snapshots.values() for record in batch]
    handles = {str(record.get("channel", "")).lower().lstrip("@") for record in records
               if record.get("channel")}
    groups = Counter(record.get("legacy_group") or "unknown" for record in records)
    return {
        "schema_version": "1.0",
        "snapshot_count": len(snapshots),
        "snapshot_records": len(records),
        "unique_channels": len(handles),
        "infrastructure_candidate_records": sum(bool(r.get("infrastructure_candidate")) for r in records),
        "origin_unknown_records": sum((r.get("origin") or {}).get("value", "unknown") == "unknown"
                                       for r in records),
        "content_orientation_unknown_records": sum(
            (r.get("content_orientation") or {}).get("value", "unknown") == "unknown" for r in records),
        "legacy_group_records": dict(sorted(groups.items())),
        "per_snapshot": {
            name: {
                "records": len(batch),
                "unique_channels": len({str(r.get("channel", "")).lower().lstrip("@")
                                         for r in batch if r.get("channel")}),
                "infrastructure_candidate_records": sum(bool(r.get("infrastructure_candidate"))
                                                         for r in batch),
            }
            for name, batch in sorted(snapshots.items())
        },
        "interpretation": "Кількість кандидатів інфраструктури використовується для маршрутизації перевірки й не є висновком про походження, політичну орієнтацію, державний контроль або участь в операції.",
    }


def render_markdown(summary: dict, input_receipts: list[dict]) -> str:
    lines = [
        "# Підсумок кандидатних ознак каналів Telegram",
        "",
        "У звіті наведено версійовані експорти кандидатних ознак для наданих знімків панелі Telegram. Він не визначає походження каналів, політичну орієнтацію, оператора, державний контроль чи участь в операціях.",
        "",
        "## Охоплення",
        "",
        f"- Записів у знімках: **{summary['snapshot_records']}** у **{summary['snapshot_count']}** файлах.",
        f"- Різних назв каналів у цих знімках: **{summary['unique_channels']}**.",
        f"- Записів із `infrastructure_candidate=true`: **{summary['infrastructure_candidate_records']}**.",
        f"- Походження лишається `unknown` у **{summary['origin_unknown_records']}** записах.",
        f"- Орієнтація вмісту лишається `unknown` у **{summary['content_orientation_unknown_records']}** записах.",
        "",
        "## Кількість записів за знімками",
        "",
        "| Файл знімка | Записів | Унікальних назв каналів | Кандидатів інфраструктури |",
        "|---|---:|---:|---:|",
    ]
    for name, values in summary["per_snapshot"].items():
        lines.append(f"| `{name}` | {values['records']} | {values['unique_channels']} | {values['infrastructure_candidate_records']} |")
    lines.extend([
        "",
        "## Входи та походження даних",
        "",
        "| Експорт кандидатних ознак | SHA-256 |",
        "|---|---|",
    ])
    for receipt in input_receipts:
        lines.append(f"| `{receipt['path']}` | `{receipt['sha256']}` |")
    lines.extend([
        "",
        "## Межі інтерпретації",
        "",
        "Кандидат інфраструктури визначається за маркерами у відкритому профілі або за часткою спостережених зовнішніх посилань, що перевищує поріг. Такі сигнали можуть траплятися в українських OSINT- і фактчекінгових каналах, цитатах, архівних посиланнях та мати інші пояснення, що не пов'язані з атрибуцією. Прапорець кандидата не встановлює походження чи контроль. Старі мітки A–U збережено в окремому полі сумісності; вони не заповнюють нові осі доказів.",
        "",
        "**Походження даних:** кількість обчислено скриптом `src/analysis/channel_evidence_summary.py` на основі наведених експортів JSONL. Експорти створено скриптом `src/analysis/channel_evidence.py` із зазначених знімків каналів і дописів. Панель сформовано цільовим добором; вона не представляє всі канали Telegram.",
        "",
    ])
    return "\n".join(lines)


def write_atomic(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Створити підсумок експортів кандидатних ознак каналів")
    parser.add_argument("inputs", nargs="+", type=Path, help="Вхідні експорти JSONL")
    parser.add_argument("--json", required=True, type=Path, help="Шлях до підсумку JSON")
    parser.add_argument("--markdown", required=True, type=Path, help="Шлях до підсумку Markdown")
    args = parser.parse_args(argv)
    resolved_inputs = {path.resolve() for path in args.inputs}
    json_output = args.json.resolve()
    markdown_output = args.markdown.resolve()
    if json_output == markdown_output:
        raise SystemExit("JSON- і Markdown-підсумки мають мати різні шляхи виводу")
    if json_output in resolved_inputs or markdown_output in resolved_inputs:
        raise SystemExit("шляхи виводу підсумку не мають збігатися зі шляхами вхідних експортів")
    batches = {str(path): read_jsonl(path) for path in args.inputs}
    summary = summarize(batches)
    receipts = [{"path": str(path), "sha256": sha256_path(path)} for path in args.inputs]
    write_atomic(args.json, json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    write_atomic(args.markdown, render_markdown(summary, receipts))
    print(f"Підсумовано записів: {summary['snapshot_records']}; файлів знімків: {summary['snapshot_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
