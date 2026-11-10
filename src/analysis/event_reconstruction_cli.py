"""CLI to produce a deterministic event-reconstruction bundle from JSONL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Iterable

from src.analysis.event_reconstruction import reconstruct_timeline
from src.contracts import validate_document


def _jsonl(rows: Iterable[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)


def build_bundle(
    input_path: Path,
    output_dir: Path,
    *,
    overwrite: bool = False,
    incident_id: str | None = None,
    run_id: str | None = None,
    as_of_utc: str | None = None,
    git_commit: str | None = None,
) -> dict:
    if output_dir.is_symlink():
        raise ValueError("output directory must not be a symlink")
    absolute_input = Path(os.path.abspath(input_path))
    absolute_output = Path(os.path.abspath(output_dir))
    if absolute_input == absolute_output or absolute_output in absolute_input.parents:
        raise ValueError("input file must be outside the output directory")
    resolved_input = input_path.resolve(strict=True)
    resolved_output = output_dir.resolve()
    if resolved_input == resolved_output or resolved_output in resolved_input.parents:
        raise ValueError("input file must be outside the output directory")
    raw = input_path.read_bytes()
    mentions = []
    for line_number, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at line {line_number}: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL line {line_number} must contain an object")
        mentions.append(value)

    parameters = {
        "incident_id": incident_id,
        "run_id": run_id,
        "as_of_utc": as_of_utc,
        "git_commit": git_commit,
    }
    parameters_sha256 = hashlib.sha256(json.dumps(parameters, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = reconstruct_timeline(
        mentions,
        run_context={
            "incident_id": incident_id,
            "run_id": run_id,
            "as_of_utc": as_of_utc,
            "git_commit": git_commit,
            "parameters_sha256": parameters_sha256,
        },
    )
    validate_document(result, "event_reconstruction.schema.json")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    outputs = {
        "events.jsonl": _jsonl(result["events"]),
        "event_links.jsonl": _jsonl(result["candidate_links"]),
        "temporal_relations.jsonl": _jsonl(result["temporal_relations"]),
        "unresolved.jsonl": _jsonl(result["unresolved"]),
        "unresolved_constraints.jsonl": _jsonl(result["unresolved_constraints"]),
        "timeline.json": json.dumps(result["timelines"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        "timeline.md": _timeline_markdown(result),
    }
    known_names = set(outputs) | {"run_receipt.json"}
    if output_dir.exists():
        if not output_dir.is_dir():
            raise FileExistsError(f"output path is not a directory: {output_dir}")
        existing = {child.name for child in output_dir.iterdir()}
        unknown = sorted(existing - known_names)
        if unknown:
            raise FileExistsError("refusing to replace output directory containing unrelated files: " + ", ".join(unknown))
        non_files = sorted(child.name for child in output_dir.iterdir() if child.name in known_names and not child.is_file())
        if non_files:
            raise FileExistsError("bundle outputs must be regular files: " + ", ".join(non_files))
        if existing and not overwrite:
            raise FileExistsError("refusing to overwrite existing bundle; pass --overwrite explicitly")

    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.staging-", dir=output_dir.parent))
    hashes = {}
    try:
        for name, contents in outputs.items():
            (staging / name).write_text(contents, encoding="utf-8")
            hashes[name] = hashlib.sha256(contents.encode("utf-8")).hexdigest()
        receipt = {
            "schema_version": "1.0",
            "producer": "social.event_reconstruction_cli",
            "method": "deterministic_event_mention_timeline_v1",
            "input_path": str(input_path.resolve()),
            "input_sha256": hashlib.sha256(raw).hexdigest(),
            "incident_id": incident_id,
            "run_id": run_id,
            "as_of_utc": result["as_of_utc"],
            "git_commit": git_commit,
            "provenance": result["provenance"],
            "parameters_sha256": parameters_sha256,
            "input_event_mention_count": len(mentions),
            "output_sha256": hashes,
            "counts": {
                "event_candidates": len(result["events"]),
                "possible_same_links": len(result["candidate_links"]),
                "temporal_relations": len(result["temporal_relations"]),
                "unresolved": len(result["unresolved"]),
            "unresolved_constraints": len(result["unresolved_constraints"]),
            },
            "limitations": result["limitations"],
        }
        receipt_text = json.dumps(receipt, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        (staging / "run_receipt.json").write_text(receipt_text, encoding="utf-8")

        if output_dir.exists():
            backup = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.backup-", dir=output_dir.parent))
            backup.rmdir()
            os.replace(output_dir, backup)
            try:
                os.replace(staging, output_dir)
            except Exception:
                os.replace(backup, output_dir)
                raise
            shutil.rmtree(backup)
        else:
            os.replace(staging, output_dir)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return receipt


def _timeline_markdown(result: dict) -> str:
    lines = [
        "# Часова реконструкція згадок подій",
        "",
        "Це автоматично впорядковані згадки джерел, а не встановлена повна хронологія реальних подій.",
        "Час публікації показано окремо від заявленого часу події; часовий порядок не доводить причинність.",
        "",
    ]
    for kind, title in (("physical", "Фізичні події"), ("communication", "Комунікаційні події")):
        lines.extend([f"## {title}", ""])
        rows = result["timelines"][kind]
        if not rows:
            lines.extend(["Для цієї часової шкали немає записів із придатними часовими межами.", ""])
            continue
        for row in rows:
            lines.append(
                f"- `{row['event_id']}` — {row['earliest_utc']} … {row['latest_utc']} "
                f"({row['time_kind']}; докази: {', '.join(row['evidence_ids'])})"
            )
        lines.append("")
    lines.extend(["## Обмеження", ""])
    lines.extend(f"- {item}" for item in result["limitations"])
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_jsonl", type=Path, help="one event mention JSON object per line")
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--overwrite", action="store_true", help="replace this bundle's known output files")
    parser.add_argument("--incident-id")
    parser.add_argument("--run-id")
    parser.add_argument("--as-of-utc", help="UTC cutoff to record; inputs are not filtered by this command")
    parser.add_argument("--git-commit")
    args = parser.parse_args()
    receipt = build_bundle(
        args.input_jsonl, args.output_dir, overwrite=args.overwrite,
        incident_id=args.incident_id, run_id=args.run_id,
        as_of_utc=args.as_of_utc, git_commit=args.git_commit,
    )
    print(json.dumps(receipt["counts"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
