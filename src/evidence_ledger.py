"""Append-only, local evidence archive metadata backed by SQLite.

The ledger stores references and extracted passages; it never writes or edits
the archived source files themselves. A ``research_finding`` is an analyst
conclusion with traceable evidence, while a ``published_claim`` is attributed
to a source. Neither label declares a statement to be an unqualified fact.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
ASSESSMENT_STATUSES = frozenset({
    "unverified",
    "supported_within_scope",
    "refuted_within_scope",
    "conflicting_evidence",
    "insufficient_evidence",
    "not_verifiable",
    "superseded",
})
_ASSESSMENT_STATUSES_ALLOWING_NO_EVIDENCE = frozenset({
    "unverified", "insufficient_evidence", "not_verifiable", "superseded",
})

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_RECORD_FIELDS = {
    "Source": {"uri", "title", "publisher", "published_at"},
    "Artifact": {"source_id", "path", "sha256", "media_type", "captured_at"},
    "Passage": {"artifact_id", "text", "locator"},
    "Claim": {"statement", "claim_type", "source_id", "evidence_refs"},
    "ClaimMention": {"claim_id", "passage_id", "text"},
    "EvidenceAssessment": {
        "claim_id", "status", "evidence_refs", "rationale", "assessor", "supersedes"
    },
    "VerificationResult": {
        "claim_id", "scope", "as_of", "status", "supporting_evidence_ids",
        "contradicting_evidence_ids", "rationale", "supersedes"
    },
}
_COMMON_FIELDS = {"record_type", "schema_version", "record_id", "created_at"}
_PROVENANCE_FIELDS = {"producer", "run_id", "limitations"}
_DERIVED_RECORD_TYPES = {
    "Artifact", "Passage", "ClaimMention", "EvidenceAssessment", "VerificationResult"
}
_REQUIRED_FIELDS = {
    "Source": {"uri", "title"},
    "Artifact": {"source_id", "path", "sha256", "media_type", "captured_at"},
    "Passage": {"artifact_id", "text"},
    "Claim": {"statement", "claim_type"},
    "ClaimMention": {"claim_id", "passage_id"},
    "EvidenceAssessment": {"claim_id", "status", "evidence_refs"},
    "VerificationResult": {
        "claim_id", "scope", "as_of", "status", "supporting_evidence_ids",
        "contradicting_evidence_ids"
    },
}


class LedgerError(Exception):
    """Base class for ledger failures."""


class LedgerValidationError(LedgerError, ValueError):
    """A record is malformed or refers to an invalid record."""


class LedgerIntegrityError(LedgerError):
    """An append would change an existing immutable record."""


class EvidenceLedger:
    """Versioned JSON records stored as immutable rows in a SQLite database."""

    def __init__(self, database: str | Path = ":memory:"):
        self._connection = sqlite3.connect(str(database))
        self._connection.row_factory = sqlite3.Row
        try:
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._migrate_schema()
        except Exception:
            self._connection.close()
            raise

    def _migrate_schema(self) -> None:
        connection = self._connection
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise LedgerValidationError("unsupported database schema version")
        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        if not objects:
            if version != 0:
                raise LedgerValidationError("missing ledger schema")
            with connection:
                connection.execute("""CREATE TABLE records (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id TEXT NOT NULL UNIQUE,
                    record_type TEXT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )""")
                for action in ("UPDATE", "DELETE"):
                    connection.execute(f"""CREATE TRIGGER records_no_{action.lower()}
                        BEFORE {action} ON records BEGIN
                        SELECT RAISE(ABORT, 'evidence ledger records are append-only'); END""")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            return
        # Accept only the v1 layout and its append-only protections. Inspect before
        # writing anything so a malformed legacy database is left untouched.
        columns = connection.execute("PRAGMA table_info(records)").fetchall()
        actual = [(r[1], r[2].upper(), r[3], r[5]) for r in columns]
        expected = [("sequence", "INTEGER", 0, 1), ("record_id", "TEXT", 1, 0),
                    ("record_type", "TEXT", 1, 0), ("schema_version", "INTEGER", 1, 0),
                    ("created_at", "TEXT", 1, 0), ("payload", "TEXT", 1, 0)]
        entries = {(r[0], r[1]): r[2] for r in objects}
        indexes = connection.execute("PRAGMA index_list(records)").fetchall()
        unique_id = any(r[2] and [i[2] for i in connection.execute(
            'PRAGMA index_info("' + r[1].replace('"', '""') + '")')] == ["record_id"]
                        for r in indexes)
        table_sql = entries.get(("table", "records"), "") or ""
        triggers_ok = all(re.sub(r"\s+", " ", (entries.get(("trigger", "records_no_" + action.lower()), "") or "").strip()).lower()
                          == f"create trigger records_no_{action.lower()} before {action.lower()} on records begin select raise(abort, 'evidence ledger records are append-only'); end"
                          for action in ("UPDATE", "DELETE"))
        if (actual != expected or not unique_id or "AUTOINCREMENT" not in table_sql.upper()
                or not triggers_ok or set(entries) != {("table", "records"),
                    ("trigger", "records_no_update"), ("trigger", "records_no_delete")}):
            raise LedgerValidationError("invalid ledger schema")
        if version == 0:
            with connection:
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "EvidenceLedger":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def add_source(
        self, record_id: str, *, uri: str, title: str,
        publisher: str | None = None, published_at: str | datetime | None = None,
        producer: str | None = None, run_id: str | None = None,
        limitations: Iterable[str] | None = None,
        created_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"uri": uri, "title": title}
        if publisher is not None:
            data["publisher"] = publisher
        if published_at is not None:
            data["published_at"] = _timestamp(published_at, "published_at")
        data.update(_provenance_data(producer, run_id, limitations))
        return self._add("Source", record_id, data, created_at)

    def add_artifact(
        self, record_id: str, *, source_id: str, path: str, sha256: str,
        media_type: str, captured_at: str | datetime,
        producer: str | None = None, run_id: str | None = None,
        limitations: Iterable[str] | None = None,
        created_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        data = {
            "source_id": source_id,
            "path": path,
            "sha256": sha256,
            "media_type": media_type,
            "captured_at": _timestamp(captured_at, "captured_at"),
        }
        data.update(_provenance_data(producer, run_id, limitations))
        return self._add("Artifact", record_id, data, created_at)

    def add_passage(
        self, record_id: str, *, artifact_id: str, text: str,
        locator: Any = None, producer: str | None = None, run_id: str | None = None,
        limitations: Iterable[str] | None = None,
        created_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"artifact_id": artifact_id, "text": text}
        if locator is not None:
            data["locator"] = locator
        data.update(_provenance_data(producer, run_id, limitations))
        return self._add("Passage", record_id, data, created_at)

    def add_claim(
        self, record_id: str, *, statement: str, claim_type: str,
        source_id: str | None = None, evidence_refs: Iterable[str] = (),
        producer: str | None = None, run_id: str | None = None,
        limitations: Iterable[str] | None = None,
        created_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"statement": statement, "claim_type": claim_type}
        if source_id is not None:
            data["source_id"] = source_id
        refs = _collect_references(evidence_refs, "evidence_refs")
        if refs:
            data["evidence_refs"] = refs
        data.update(_provenance_data(producer, run_id, limitations))
        return self._add("Claim", record_id, data, created_at)

    def add_claim_mention(
        self, record_id: str, *, claim_id: str, passage_id: str,
        text: str | None = None, producer: str | None = None, run_id: str | None = None,
        limitations: Iterable[str] | None = None,
        created_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {"claim_id": claim_id, "passage_id": passage_id}
        if text is not None:
            data["text"] = text
        data.update(_provenance_data(producer, run_id, limitations))
        return self._add("ClaimMention", record_id, data, created_at)

    def add_assessment(
        self, record_id: str, *, claim_id: str, status: str,
        evidence_refs: Iterable[str] = (), rationale: str | None = None,
        assessor: str | None = None, supersedes: str | None = None,
        producer: str | None = None, run_id: str | None = None,
        limitations: Iterable[str] | None = None,
        created_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "claim_id": claim_id,
            "status": status,
            "evidence_refs": _collect_references(evidence_refs, "evidence_refs"),
        }
        if rationale is not None:
            data["rationale"] = rationale
        if assessor is not None:
            data["assessor"] = assessor
        if supersedes is not None:
            data["supersedes"] = supersedes
        data.update(_provenance_data(producer, run_id, limitations))
        return self._add("EvidenceAssessment", record_id, data, created_at)

    def add_verification_result(
        self, record_id: str, *, claim_id: str, scope: str,
        as_of: str | datetime, status: str,
        supporting_evidence_ids: Iterable[str] = (),
        contradicting_evidence_ids: Iterable[str] = (),
        rationale: str | None = None, supersedes: str | None = None,
        producer: str | None = None, run_id: str | None = None,
        limitations: Iterable[str] | None = None,
        created_at: str | datetime | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "claim_id": claim_id,
            "scope": scope,
            "as_of": _timestamp(as_of, "as_of"),
            "status": status,
            "supporting_evidence_ids": _collect_references(
                supporting_evidence_ids, "supporting_evidence_ids"
            ),
            "contradicting_evidence_ids": _collect_references(
                contradicting_evidence_ids, "contradicting_evidence_ids"
            ),
        }
        if rationale is not None:
            data["rationale"] = rationale
        if supersedes is not None:
            data["supersedes"] = supersedes
        data.update(_provenance_data(producer, run_id, limitations))
        return self._add("VerificationResult", record_id, data, created_at)

    def _add(
        self, record_type: str, record_id: str, data: Mapping[str, Any],
        created_at: str | datetime | None,
    ) -> dict[str, Any]:
        record = {
            "record_type": record_type,
            "schema_version": SCHEMA_VERSION,
            "record_id": record_id,
            "created_at": _timestamp(created_at, "created_at", default_now=True),
            **data,
        }
        with self._connection:
            return self._insert_record(record)

    def _insert_record(self, raw_record: Mapping[str, Any]) -> dict[str, Any]:
        record = self._validate_record(raw_record)
        encoded = _encode(record)
        previous = self._connection.execute(
            "SELECT payload FROM records WHERE record_id = ?", (record["record_id"],)
        ).fetchone()
        if previous:
            if previous["payload"] == encoded:
                return record
            raise LedgerIntegrityError(f"record id already exists with different content: {record['record_id']}")
        self._connection.execute(
            "INSERT INTO records(record_id, record_type, schema_version, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (record["record_id"], record["record_type"], record["schema_version"],
             record["created_at"], encoded),
        )
        return record

    def _validate_record(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise LedgerValidationError("each record must be a JSON object")
        record_type = raw.get("record_type")
        if not isinstance(record_type, str) or record_type not in _RECORD_FIELDS:
            raise LedgerValidationError(f"unknown record_type: {record_type!r}")
        allowed = _COMMON_FIELDS | _PROVENANCE_FIELDS | _RECORD_FIELDS[record_type]
        unknown = set(raw) - allowed
        missing = _COMMON_FIELDS - set(raw)
        missing_data = _REQUIRED_FIELDS[record_type]
        missing |= missing_data - set(raw)
        if missing:
            raise LedgerValidationError(f"missing fields for {record_type}: {', '.join(sorted(missing))}")
        if unknown:
            raise LedgerValidationError(f"unknown fields for {record_type}: {', '.join(sorted(unknown))}")

        record = dict(raw)
        if type(record["schema_version"]) is not int or record["schema_version"] != SCHEMA_VERSION:
            raise LedgerValidationError(f"unsupported schema version: {record['schema_version']!r}")
        record["record_id"] = _identifier(record["record_id"], "record_id")
        record["created_at"] = _timestamp(record["created_at"], "created_at")

        if record_type == "Source":
            record["uri"] = _nonempty(record["uri"], "uri")
            record["title"] = _nonempty(record["title"], "title")
            _optional_nonempty(record, "publisher")
            if "published_at" in record:
                record["published_at"] = _timestamp(record["published_at"], "published_at")

        elif record_type == "Artifact":
            record["source_id"] = self._require_type(record["source_id"], "Source")
            record["path"] = _nonempty(record["path"], "path")
            record["media_type"] = _nonempty(record["media_type"], "media_type")
            if not isinstance(record["sha256"], str) or not _SHA256.fullmatch(record["sha256"]):
                raise LedgerValidationError("sha256 must be 64 hexadecimal characters")
            record["sha256"] = record["sha256"].lower()
            record["captured_at"] = _timestamp(record["captured_at"], "captured_at")

        elif record_type == "Passage":
            record["artifact_id"] = self._require_type(record["artifact_id"], "Artifact")
            record["text"] = _nonempty(record["text"], "text")
            if "locator" in record:
                _ensure_json_value(record["locator"], "locator")

        elif record_type == "Claim":
            record["statement"] = _nonempty(record["statement"], "statement")
            claim_type = record["claim_type"]
            if not isinstance(claim_type, str) or claim_type not in {"published_claim", "research_finding"}:
                raise LedgerValidationError("claim_type must be 'published_claim' or 'research_finding'")
            if "source_id" in record:
                record["source_id"] = self._require_type(record["source_id"], "Source")
            refs = _references(record.get("evidence_refs", []), "evidence_refs")
            if claim_type == "published_claim" and "source_id" not in record:
                raise LedgerValidationError("published_claim requires source_id")
            if claim_type == "research_finding" and not refs:
                raise LedgerValidationError("research_finding requires at least one evidence reference")
            self._require_evidence(refs)
            if "evidence_refs" in record:
                record["evidence_refs"] = refs

        elif record_type == "ClaimMention":
            record["claim_id"] = _identifier(record["claim_id"], "claim_id")
            record["passage_id"] = _identifier(record["passage_id"], "passage_id")
            claim = self._get_record(record["claim_id"])
            passage = self._get_record(record["passage_id"])
            if claim is None or claim["record_type"] != "Claim" or claim["claim_type"] != "published_claim":
                raise LedgerValidationError("ClaimMention claim_id must reference a published_claim")
            if passage is None or passage["record_type"] != "Passage":
                raise LedgerValidationError("ClaimMention passage_id must reference a Passage")
            artifact = self._get_record(passage["artifact_id"])
            if artifact["source_id"] != claim["source_id"]:
                raise LedgerValidationError("ClaimMention passage must come from the claim's source")
            if "text" in record:
                record["text"] = _nonempty(record["text"], "text")

        elif record_type == "EvidenceAssessment":
            record["claim_id"] = _identifier(record["claim_id"], "claim_id")
            claim = self._get_record(record["claim_id"])
            if claim is None or claim["record_type"] != "Claim":
                raise LedgerValidationError("EvidenceAssessment claim_id must reference a Claim")
            if not isinstance(record["status"], str) or record["status"] not in ASSESSMENT_STATUSES:
                raise LedgerValidationError(f"unsupported assessment status: {record['status']!r}")
            refs = _references(record["evidence_refs"], "evidence_refs")
            if not refs and record["status"] not in _ASSESSMENT_STATUSES_ALLOWING_NO_EVIDENCE:
                raise LedgerValidationError("this assessment status requires evidence_refs")
            self._require_evidence(refs)
            record["evidence_refs"] = refs
            _optional_nonempty(record, "rationale")
            _optional_nonempty(record, "assessor")
            if "supersedes" in record:
                supersedes_id = _identifier(record["supersedes"], "supersedes")
                previous = self._get_record(supersedes_id)
                if previous is None or previous["record_type"] != "EvidenceAssessment":
                    raise LedgerValidationError("supersedes must reference an EvidenceAssessment")
                if previous["claim_id"] != record["claim_id"]:
                    raise LedgerValidationError("superseded assessment must target the same claim")
                child = self._connection.execute(
                    "SELECT record_id FROM records WHERE record_type = 'EvidenceAssessment' "
                    "AND json_extract(payload, '$.supersedes') = ? LIMIT 1", (supersedes_id,)
                ).fetchone()
                if child and child["record_id"] != record["record_id"]:
                    raise LedgerValidationError("assessment supersedes chain cannot branch")
                record["supersedes"] = supersedes_id

        elif record_type == "VerificationResult":
            record["claim_id"] = _identifier(record["claim_id"], "claim_id")
            claim = self._get_record(record["claim_id"])
            if claim is None or claim["record_type"] != "Claim":
                raise LedgerValidationError("VerificationResult claim_id must reference a Claim")
            record["scope"] = _nonempty(record["scope"], "scope")
            record["as_of"] = _timestamp(record["as_of"], "as_of")
            if not isinstance(record["status"], str) or record["status"] not in ASSESSMENT_STATUSES:
                raise LedgerValidationError(f"unsupported verification status: {record['status']!r}")
            supporting = _references(record["supporting_evidence_ids"], "supporting_evidence_ids")
            contradicting = _references(record["contradicting_evidence_ids"], "contradicting_evidence_ids")
            overlap = set(supporting) & set(contradicting)
            if overlap:
                raise LedgerValidationError("supporting and contradicting evidence ids must be distinct")
            self._require_evidence(supporting)
            self._require_evidence(contradicting)
            if record["status"] == "supported_within_scope" and not supporting:
                raise LedgerValidationError("supported_within_scope requires supporting evidence")
            if record["status"] == "refuted_within_scope" and not contradicting:
                raise LedgerValidationError("refuted_within_scope requires contradicting evidence")
            if record["status"] == "conflicting_evidence" and (not supporting or not contradicting):
                raise LedgerValidationError("conflicting_evidence requires both supporting and contradicting evidence")
            record["supporting_evidence_ids"] = supporting
            record["contradicting_evidence_ids"] = contradicting
            _optional_nonempty(record, "rationale")
            if "supersedes" in record:
                supersedes_id = _identifier(record["supersedes"], "supersedes")
                previous = self._get_record(supersedes_id)
                if previous is None or previous["record_type"] != "VerificationResult":
                    raise LedgerValidationError("supersedes must reference a VerificationResult")
                if previous["claim_id"] != record["claim_id"]:
                    raise LedgerValidationError("superseded VerificationResult must target the same claim")
                child = self._connection.execute(
                    "SELECT record_id FROM records WHERE record_type = 'VerificationResult' "
                    "AND json_extract(payload, '$.supersedes') = ? LIMIT 1", (supersedes_id,)
                ).fetchone()
                if child and child["record_id"] != record["record_id"]:
                    raise LedgerValidationError("VerificationResult supersedes chain cannot branch")
                record["supersedes"] = supersedes_id

        _validate_provenance(record, record_type)

        return record

    def _require_type(self, record_id: str, record_type: str) -> str:
        record_id = _identifier(record_id, "reference")
        target = self._get_record(record_id)
        if target is None or target["record_type"] != record_type:
            raise LedgerValidationError(f"reference {record_id!r} must point to {record_type}")
        return record_id

    def _require_evidence(self, refs: Iterable[str]) -> None:
        for ref in refs:
            target = self._get_record(ref)
            if target is None or target["record_type"] not in {"Artifact", "Passage"}:
                raise LedgerValidationError(f"evidence reference {ref!r} must point to an Artifact or Passage")

    def _get_record(self, record_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload FROM records WHERE record_id = ?", (record_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def get_record(self, record_id: str) -> dict[str, Any] | None:
        """Return a decoded record, or ``None`` when its id is unknown."""
        record = self._get_record(record_id)
        return dict(record) if record is not None else None

    def records(self) -> list[dict[str, Any]]:
        """Return records in append order."""
        rows = self._connection.execute("SELECT payload FROM records ORDER BY sequence").fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def assessments_for(self, claim_id: str) -> list[dict[str, Any]]:
        """Return every immutable assessment for one claim in append order."""
        return [record for record in self.records()
                if record["record_type"] == "EvidenceAssessment" and record["claim_id"] == claim_id]

    def to_jsonl(self) -> str:
        """Serialize the append log as canonical UTF-8-friendly JSON Lines."""
        records = self.records()
        return "".join(_encode(record) + "\n" for record in records)

    def import_jsonl(self, contents: str) -> int:
        """Append a JSONL stream atomically; return the number of new records."""
        if not isinstance(contents, str):
            raise LedgerValidationError("JSONL input must be text")
        inserted = 0
        try:
            with self._connection:
                for line_number, line in enumerate(contents.splitlines(), 1):
                    if not line.strip():
                        continue
                    try:
                        raw_record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise LedgerValidationError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
                    before = self._connection.total_changes
                    self._insert_record(raw_record)
                    inserted += self._connection.total_changes - before
        except sqlite3.IntegrityError as exc:
            raise LedgerIntegrityError(str(exc)) from exc
        return inserted


def _timestamp(value: str | datetime | None, field: str, *, default_now: bool = False) -> str:
    if value is None:
        if not default_now:
            raise LedgerValidationError(f"{field} must include a timezone-aware timestamp")
        parsed = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise LedgerValidationError(f"{field} must be an ISO 8601 timestamp") from exc
    else:
        raise LedgerValidationError(f"{field} must be a timezone-aware datetime or ISO 8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LedgerValidationError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat()


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise LedgerValidationError(f"{field} must be a non-empty, trimmed string")
    return value


def _nonempty(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerValidationError(f"{field} must be a non-empty string")
    return value


def _optional_nonempty(record: dict[str, Any], field: str) -> None:
    if field in record and record[field] is not None:
        record[field] = _nonempty(record[field], field)


def _references(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        raise LedgerValidationError(f"{field} must be a list of record ids")
    refs = [_identifier(item, field) for item in value]
    if len(set(refs)) != len(refs):
        raise LedgerValidationError(f"{field} cannot contain duplicates")
    return refs


def _collect_references(value: Iterable[str], field: str) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise LedgerValidationError(f"{field} must be a sequence of record ids, not a string")
    try:
        refs = list(value)
    except TypeError as exc:
        raise LedgerValidationError(f"{field} must be a sequence of record ids") from exc
    return [_identifier(item, field) for item in refs]


def _collect_strings(value: Iterable[str], field: str) -> list[str]:
    if isinstance(value, (str, bytes)):
        raise LedgerValidationError(f"{field} must be a sequence of strings, not a string")
    try:
        items = list(value)
    except TypeError as exc:
        raise LedgerValidationError(f"{field} must be a sequence of strings") from exc
    return [_nonempty(item, field) for item in items]


def _provenance_data(
    producer: str | None, run_id: str | None, limitations: Iterable[str] | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if producer is not None:
        data["producer"] = producer
    if run_id is not None:
        data["run_id"] = run_id
    if limitations is not None:
        data["limitations"] = _collect_strings(limitations, "limitations")
    return data


def _validate_provenance(record: dict[str, Any], record_type: str) -> None:
    if "producer" in record:
        record["producer"] = _nonempty(record["producer"], "producer")
    if "run_id" in record:
        record["run_id"] = _identifier(record["run_id"], "run_id")
    if "limitations" in record:
        record["limitations"] = _collect_strings(record["limitations"], "limitations")

    is_finding = record_type == "Claim" and record.get("claim_type") == "research_finding"
    if record_type in _DERIVED_RECORD_TYPES or is_finding:
        missing = _PROVENANCE_FIELDS - set(record)
        if missing:
            raise LedgerValidationError(
                f"{record_type} requires provenance fields: {', '.join(sorted(missing))}"
            )


def _ensure_json_value(value: Any, field: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LedgerValidationError(f"{field} must be JSON-serializable") from exc


def _encode(record: Mapping[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
