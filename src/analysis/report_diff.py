"""Deterministic, evidence-reference-level comparison of Social report bundles."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from src.analysis.report_generator import EVIDENCE_ROLES
from src.analysis.incident_ops import (
    IncidentOpsError,
    _path_without_traversal,
    _reject_symlink_components,
    _utc,
)


class ReportDiffError(ValueError):
    """Malformed report bundle or unsafe output path."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    _reject_symlink_components(path)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportDiffError(f"cannot read {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReportDiffError(f"{label} must be a JSON object")
    return value


def _read_findings(contents: bytes) -> dict[str, dict[str, Any]]:
    findings: dict[str, dict[str, Any]] = {}
    try:
        lines = contents.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ReportDiffError(f"findings.jsonl is not valid UTF-8: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReportDiffError(f"invalid findings JSONL at line {line_number}: {exc.msg}") from exc
        if not isinstance(row, dict) or not isinstance(row.get("finding_id"), str) or not row["finding_id"]:
            raise ReportDiffError(f"finding at line {line_number} requires a non-empty finding_id")
        if row["finding_id"] in findings:
            raise ReportDiffError(f"duplicate finding_id: {row['finding_id']}")
        _evidence_refs(row)
        findings[row["finding_id"]] = row
    return findings


def _verify_bundle_outputs(root: Path, receipt: dict[str, Any]) -> dict[str, bytes]:
    hashes = receipt.get("output_sha256")
    required = {"findings.jsonl", "incident_report.md"}
    if not isinstance(hashes, dict) or not required.issubset(hashes):
        raise ReportDiffError("run receipt must include output_sha256 for findings.jsonl and incident_report.md")
    verified: dict[str, bytes] = {}
    for name, expected in sorted(hashes.items()):
        if (not isinstance(name, str) or Path(name).name != name or name in {"", ".", ".."}
                or not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None):
            raise ReportDiffError(f"invalid output hash entry in report receipt: {name!r}")
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ReportDiffError(f"report output is missing or unsafe: {name}")
        _reject_symlink_components(path)
        try:
            contents = path.read_bytes()
            actual = hashlib.sha256(contents).hexdigest()
        except OSError as exc:
            raise ReportDiffError(f"cannot read report output {name}: {exc}") from exc
        if actual != expected:
            raise ReportDiffError(f"report output SHA-256 mismatch: {name}")
        verified[name] = contents
    return verified


def _rendered_report_value(value: Any) -> str:
    """Mirror report_generator's `_md` escaping for its three metadata rows."""
    safe_lines = str(value).replace("\r\n", " ↵ ").replace("\r", " ↵ ").replace("\n", " ↵ ")
    escaped = "".join("\\" + char if char in "\\`*_{}[]()!|>~" else char for char in safe_lines)
    return html.escape(escaped, quote=True)


def _verify_report_receipt_metadata(contents: bytes, receipt: dict[str, Any], label: str) -> None:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReportDiffError(f"{label} incident_report.md is not valid UTF-8: {exc}") from exc
    expected = {
        "- Incident ID: ": receipt.get("incident_id"),
        "- Run ID: ": receipt.get("run_id"),
        "- Зріз as_of: ": receipt.get("as_of_utc"),
    }
    lines = text.splitlines()
    for prefix, value in expected.items():
        matches = [line[len(prefix):] for line in lines if line.startswith(prefix)]
        if len(matches) != 1 or not isinstance(value, str) or matches[0] != _rendered_report_value(value):
            raise ReportDiffError(f"{label} receipt metadata does not match verified incident_report.md ({prefix.strip()})")


def _bundle(path: str | Path, label: str) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    try:
        root = _path_without_traversal(path)
        _reject_symlink_components(root)
    except IncidentOpsError as exc:
        raise ReportDiffError(str(exc)) from exc
    if not root.is_dir():
        raise ReportDiffError(f"{label} report bundle directory does not exist: {root}")
    receipt = _read_json(root / "run_receipt.json", f"{label} run receipt")
    try:
        _utc(receipt.get("as_of_utc"), f"{label}.as_of_utc")
    except IncidentOpsError as exc:
        raise ReportDiffError(str(exc)) from exc
    if not isinstance(receipt.get("run_id"), str) or not receipt["run_id"]:
        raise ReportDiffError(f"{label} run receipt requires run_id")
    if not isinstance(receipt.get("incident_id"), str) or not receipt["incident_id"]:
        raise ReportDiffError(f"{label} run receipt requires incident_id")
    outputs = _verify_bundle_outputs(root, receipt)
    _verify_report_receipt_metadata(outputs["incident_report.md"], receipt, label)
    return receipt, _read_findings(outputs["findings.jsonl"])


def _evidence_refs(row: dict[str, Any]) -> list[str]:
    refs = row.get("evidence_refs")
    if refs is None:
        refs = row.get("citation_ids", [])
    if refs is None:
        refs = []
    if isinstance(refs, str):
        refs = [refs]
    if not isinstance(refs, list):
        raise ReportDiffError(
            f"finding {row.get('finding_id')} evidence references in evidence_refs/citation_ids must be a list"
        )
    identifiers = []
    typed_pairs = []
    for item in refs:
        if isinstance(item, str) and item and item == item.strip():
            identifiers.append(item)
        elif (isinstance(item, dict) and set(item) == {"evidence_id", "role"}
              and isinstance(item.get("evidence_id"), str) and item["evidence_id"]
              and item["evidence_id"] == item["evidence_id"].strip()
              and isinstance(item.get("role"), str) and item["role"] in EVIDENCE_ROLES):
            # ReportGenerator v1 emits typed references; report diff joins by
            # evidence identity while role changes are compared separately.
            identifiers.append(item["evidence_id"])
            typed_pairs.append((item["evidence_id"], item["role"]))
        else:
            raise ReportDiffError(
                f"finding {row.get('finding_id')} evidence references must be IDs or typed evidence references"
            )
    if len(typed_pairs) != len(set(typed_pairs)):
        raise ReportDiffError(f"finding {row.get('finding_id')} contains a duplicate evidence reference")
    return sorted(set(identifiers))


def _typed_evidence_roles(row: dict[str, Any]) -> list[tuple[str, str]] | None:
    refs = row.get("evidence_refs")
    if not isinstance(refs, list) or not all(isinstance(item, dict) for item in refs):
        return None
    return sorted((item["evidence_id"], item["role"]) for item in refs)


def _shared_evidence_roles_changed(before: list[tuple[str, str]] | None,
                                   after: list[tuple[str, str]] | None) -> bool:
    """Compare roles only for evidence IDs retained in both report versions."""
    if before is None or after is None:
        return False
    before_by_id: dict[str, list[str]] = {}
    after_by_id: dict[str, list[str]] = {}
    for evidence_id, role in before:
        before_by_id.setdefault(evidence_id, []).append(role)
    for evidence_id, role in after:
        after_by_id.setdefault(evidence_id, []).append(role)
    before_by_id = {key: sorted(value) for key, value in before_by_id.items()}
    after_by_id = {key: sorted(value) for key, value in after_by_id.items()}
    return any(before_by_id[evidence_id] != after_by_id[evidence_id]
               for evidence_id in before_by_id.keys() & after_by_id.keys())


def _citation_details(row: dict[str, Any]) -> list[dict[str, Any]] | None:
    citations = row.get("citations")
    if not isinstance(citations, list):
        return None
    details = []
    for item in citations:
        if not isinstance(item, dict):
            return citations  # Preserve the difference; the bundle parser handles JSON shape.
        details.append({key: value for key, value in item.items() if key != "role"})
    return sorted(details, key=lambda item: str(item.get("evidence_id", "")))


def _shared_citation_details_changed(before: list[dict[str, Any]] | None,
                                     after: list[dict[str, Any]] | None) -> bool:
    """Detect metadata edits for retained evidence, not normal evidence replacement.

    Added/removed evidence already has its own stable ``evidence_refs_changed``
    reason. Comparing whole citation objects would report redundant metadata
    changes whenever the evidence ID itself changes.
    """
    if before is None or after is None:
        return False
    before_by_id: dict[str, set[str]] = {}
    after_by_id: dict[str, set[str]] = {}
    for details, destination in ((before, before_by_id), (after, after_by_id)):
        for item in details:
            if not isinstance(item, dict) or not isinstance(item.get("evidence_id"), str):
                continue
            evidence_id = item["evidence_id"]
            destination.setdefault(evidence_id, set()).add(
                json.dumps(item, ensure_ascii=False, sort_keys=True)
            )
    return any(before_by_id[evidence_id] != after_by_id[evidence_id]
               for evidence_id in before_by_id.keys() & after_by_id.keys())


def _comparison_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = {key: value for key, value in row.items()
               if key not in {"evidence_refs", "citation_ids", "citations"} and value is not None}
    payload["evidence_ids"] = _evidence_refs(row)
    return payload


def _findings_equivalent(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if _comparison_payload(before) != _comparison_payload(after):
        return False
    if _shared_evidence_roles_changed(
        _typed_evidence_roles(before), _typed_evidence_roles(after)
    ):
        return False
    return not _shared_citation_details_changed(
        _citation_details(before), _citation_details(after)
    )


def _change_reasons(before: dict[str, Any], after: dict[str, Any]) -> tuple[list[str], str | None]:
    reasons = []
    if before.get("statement") != after.get("statement"):
        reasons.append("statement_changed")
    if before.get("kind") != after.get("kind"):
        reasons.append("finding_kind_changed")
    if _evidence_refs(before) != _evidence_refs(after):
        reasons.append("evidence_refs_changed")
    before_roles, after_roles = _typed_evidence_roles(before), _typed_evidence_roles(after)
    if _shared_evidence_roles_changed(before_roles, after_roles):
        reasons.append("evidence_roles_changed")
    before_details, after_details = _citation_details(before), _citation_details(after)
    if _shared_citation_details_changed(before_details, after_details):
        reasons.append("citation_details_changed")
    if before.get("verification_result_id") != after.get("verification_result_id"):
        reasons.append("verification_result_changed")
    correction_of = next((after.get(key) for key in
                          ("correction_of", "supersedes_finding_id", "supersedes")
                          if isinstance(after.get(key), str) and after[key]), None)
    if correction_of:
        reasons.append("explicit_correction")
    if not reasons:
        # Include all other changed payload fields without inventing a semantic cause.
        ignored = {"finding_id", "statement", "kind", "evidence_refs", "citation_ids", "citations",
                   "verification_result_id", "correction_of", "supersedes_finding_id", "supersedes"}
        if {key: value for key, value in before.items() if key not in ignored} != \
                {key: value for key, value in after.items() if key not in ignored}:
            reasons.append("other_report_fields_changed")
    reason_code = after.get("reason_code") if isinstance(after.get("reason_code"), str) else None
    order = {"explicit_correction": 0, "evidence_refs_changed": 1,
             "evidence_roles_changed": 2, "citation_details_changed": 3,
             "finding_kind_changed": 4, "statement_changed": 5,
             "verification_result_changed": 6, "other_report_fields_changed": 7}
    return sorted(set(reasons), key=lambda item: (order.get(item, 100), item)), correction_of or reason_code


def compare_report_bundles(before_dir: str | Path, after_dir: str | Path) -> dict[str, Any]:
    """Compare two as-of report bundles without asserting semantic truth or causation."""
    before_receipt, before_findings = _bundle(before_dir, "before")
    after_receipt, after_findings = _bundle(after_dir, "after")
    if before_receipt["incident_id"] != after_receipt["incident_id"]:
        raise ReportDiffError("before and after report bundles must refer to the same incident")
    before_as_of = _utc(before_receipt["as_of_utc"], "before.as_of_utc")
    after_as_of = _utc(after_receipt["as_of_utc"], "after.as_of_utc")
    if after_as_of < before_as_of:
        raise ReportDiffError("after report as_of_utc must be at or after before report as_of_utc")

    added = []
    removed = []
    changed = []
    unchanged = []
    for finding_id in sorted(before_findings.keys() | after_findings.keys()):
        old, new = before_findings.get(finding_id), after_findings.get(finding_id)
        if old is None:
            added.append(new)
        elif new is None:
            removed.append(old)
        elif not _findings_equivalent(old, new):
            reasons, explicit_link = _change_reasons(old, new)
            changed.append({"finding_id": finding_id, "before": old, "after": new,
                            "change_reasons": reasons,
                            "explicit_correction_of": explicit_link if "explicit_correction" in reasons else None,
                            "reason_code": new.get("reason_code")})
        else:
            unchanged.append(finding_id)
    return {
        "schema_version": "1.0",
        "comparison": {
            "incident_id": before_receipt["incident_id"],
            "known_then_run_id": before_receipt["run_id"],
            "known_then_as_of_utc": before_receipt["as_of_utc"],
            "known_now_run_id": after_receipt["run_id"],
            "known_now_as_of_utc": after_receipt["as_of_utc"],
        },
        "summary": {"added": len(added), "removed": len(removed),
                    "changed": len(changed), "unchanged": len(unchanged)},
        "added_findings": added,
        "removed_findings": removed,
        "changed_findings": changed,
        "unchanged_finding_ids": unchanged,
        "limitations": [
            "Diff describes changes between supplied report bundles; it does not infer why evidence changed or whether a statement is true.",
            "A correction is labeled explicit only when the later finding carries an explicit correction or supersession reference.",
            "Shared citation IDs do not establish source independence; this comparison does not reconstruct source lineage.",
            "Removed findings mean absent from the later report bundle, not necessarily refuted.",
        ],
    }


def _markdown(diff: dict[str, Any]) -> str:
    comparison = diff["comparison"]
    summary = diff["summary"]
    lines = [
        f"# Зміни звіту: {comparison['incident_id']}", "",
        f"Відомо на зрізі **{comparison['known_then_as_of_utc']}** (`{comparison['known_then_run_id']}`): "
        f"оновлено до **{comparison['known_now_as_of_utc']}** (`{comparison['known_now_run_id']}`).", "",
        f"Додано: {summary['added']}; змінено: {summary['changed']}; прибрано зі звіту: {summary['removed']}; без змін: {summary['unchanged']}.", "",
    ]
    if diff["changed_findings"]:
        lines.extend(["## Змінені висновки", ""])
        for item in diff["changed_findings"]:
            before, after = item["before"], item["after"]
            reason = ", ".join(item["change_reasons"]) or "other_report_fields_changed"
            lines.extend([
                f"### `{item['finding_id']}`",
                f"- Було: {html.escape(str(before.get('statement', '')))}",
                f"- Стало: {html.escape(str(after.get('statement', '')))}",
                f"- Зміни в записі: `{reason}`",
            ])
            if item.get("explicit_correction_of"):
                lines.append(f"- Явне посилання на виправлення: `{html.escape(str(item['explicit_correction_of']))}`")
            lines.append("")
    lines.extend(["## Обмеження", ""])
    lines.extend(f"- {text}" for text in diff["limitations"])
    return "\n".join(lines) + "\n"


def _write_new(path: Path, content: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def write_diff_bundle(diff: dict[str, Any], output_dir: str | Path) -> Path:
    try:
        output = _path_without_traversal(output_dir)
        _reject_symlink_components(output)
    except IncidentOpsError as exc:
        raise ReportDiffError(str(exc)) from exc
    if output.exists():
        raise ReportDiffError(f"output directory already exists; refusing to overwrite: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    _reject_symlink_components(output.parent)
    staging_name = None
    try:
        staging_name = tempfile.mkdtemp(prefix=f".{output.name}.staging-", dir=output.parent)
        staging = Path(staging_name)
        _write_new(staging / "report_diff.json", json.dumps(diff, ensure_ascii=False, sort_keys=True,
                                                              indent=2, allow_nan=False) + "\n")
        _write_new(staging / "report_diff.md", _markdown(diff))
        os.rename(staging, output)
        staging_name = None
        directory_fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        if staging_name:
            shutil.rmtree(staging_name, ignore_errors=True)
        raise ReportDiffError(f"cannot write report diff bundle at {output}: {exc}") from exc
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Explain differences between two Social report snapshots.")
    parser.add_argument("--before", required=True, help="earlier report bundle directory")
    parser.add_argument("--after", required=True, help="later report bundle directory")
    parser.add_argument("--output-dir", help="new directory for report_diff.json and report_diff.md")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    try:
        try:
            args = parser.parse_args(argv)
        except SystemExit as exc:
            if exc.code == 0:
                return 0
            raise
        diff = compare_report_bundles(args.before, args.after)
        if args.output_dir:
            output_dir = write_diff_bundle(diff, args.output_dir)
            print(json.dumps({"output_dir": str(output_dir), "summary": diff["summary"]},
                             ensure_ascii=False, sort_keys=True))
        else:
            print(json.dumps(diff, ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    except (IncidentOpsError, ReportDiffError, OSError) as exc:
        print(f"report-diff: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
