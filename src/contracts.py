"""Validate versioned machine-readable Social run and incident contracts."""
from __future__ import annotations

import json
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas"


class ContractValidationError(ValueError):
    """Raised when a Social contract fails schema or semantic validation."""


@lru_cache(maxsize=None)
def _validator(name: str) -> Draft202012Validator:
    schema_path = SCHEMA_DIR / name
    common_path = SCHEMA_DIR / "common.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    common = json.loads(common_path.read_text(encoding="utf-8"))
    registry = Registry().with_resource(common["$id"], Resource.from_contents(common))
    return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())


def validate_document(payload: dict, schema_name: str) -> None:
    errors = sorted(_validator(schema_name).iter_errors(payload), key=lambda error: list(error.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        raise ContractValidationError(f"{schema_name} at {location}: {error.message}")
    if schema_name == "transcript_correction.schema.json":
        interval = payload["audio_interval_ms"]
        if interval["end_ms"] <= interval["start_ms"]:
            raise ContractValidationError(
                "transcript correction audio interval end_ms must be greater than start_ms"
            )


def _parse_utc(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractValidationError(f"{field} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractValidationError(f"{field} must include a UTC timezone")
    if parsed.utcoffset().total_seconds() != 0:
        raise ContractValidationError(f"{field} must be UTC")
    return parsed


def validate_incident_spec(payload: dict) -> None:
    validate_document(payload, "incident_spec.schema.json")
    start = _parse_utc(payload["time_window_utc"][0], "time_window_utc[0]")
    end = _parse_utc(payload["time_window_utc"][1], "time_window_utc[1]")
    if start > end:
        raise ContractValidationError("time window start must be at or before its end")
    _parse_utc(payload["as_of_utc"], "as_of_utc")


def validate_run_manifest(payload: dict) -> None:
    validate_document(payload, "run_manifest.schema.json")
    start = _parse_utc(payload["started_at_utc"], "started_at_utc")
    finished = payload.get("finished_at_utc")
    if finished is not None and _parse_utc(finished, "finished_at_utc") < start:
        raise ContractValidationError("finished_at_utc must not precede started_at_utc")
