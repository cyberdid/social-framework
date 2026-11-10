from __future__ import annotations

import pytest
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

from src.analysis.event_reconstruction import reconstruct_timeline, resolve_entity_alias
from src.analysis.event_reconstruction_cli import build_bundle


def mention(
    mention_id: str,
    *,
    kind: str = "physical",
    predicate: str = "прибув",
    actor_ids: list[str] | None = None,
    place_id: str | None = "place_1",
    earliest: str | None = None,
    latest: str | None = None,
    published: str | None = None,
    cannot_link: list[str] | None = None,
) -> dict:
    time = None
    if earliest is not None or latest is not None:
        time = {
            "kind": "claimed_event_time",
            "earliest_utc": earliest,
            "latest_utc": latest,
            "precision": "interval",
            "anchor_evidence_ids": [f"evidence_{mention_id}"],
        }
    return {
        "event_mention_id": mention_id,
        "event_kind": kind,
        "predicate": predicate,
        "actor_entity_ids": actor_ids or ["actor_1"],
        "object_entity_ids": [],
        "place_id": place_id,
        "time": time,
        "publication_time_utc": published,
        "evidence_ids": [f"evidence_{mention_id}"],
        "cannot_link_event_mention_ids": cannot_link or [],
    }


def test_separates_physical_and_communication_times_and_never_inferrs_physical_time_from_posting_time():
    result = reconstruct_timeline([
        mention("physical", published="2026-11-02T18:00:00Z"),
        mention("statement", kind="communication", published="2026-11-02T18:05:00Z"),
    ])

    assert result["events"][0]["event_id"] == "event:physical"
    physical = next(row for row in result["events"] if row["event_id"] == "event:physical")
    communication = next(row for row in result["events"] if row["event_id"] == "event:statement")
    assert physical["event_time"] is None
    assert physical["publication_time_utc"] == "2026-11-02T18:00:00Z"
    assert communication["timeline_time_kind"] == "publication_time"
    assert len(result["timelines"]["physical"]) == 0
    assert len(result["timelines"]["communication"]) == 1


def test_source_references_are_not_copied_from_passage_evidence_ids():
    row = mention("source-linked")
    row["source_refs"] = ["source_official_1"]
    without_source = mention("evidence-only")

    result = reconstruct_timeline([row, without_source])
    linked = next(event for event in result["events"] if event["event_mention_id"] == "source-linked")
    evidence_only = next(event for event in result["events"] if event["event_mention_id"] == "evidence-only")

    assert linked["evidence_ids"] == ["evidence_source-linked"]
    assert linked["source_refs"] == ["source_official_1"]
    assert evidence_only["source_refs"] == []


def test_communication_timeline_uses_publication_timestamp_and_preserves_claimed_time():
    result = reconstruct_timeline([
        mention(
            "statement",
            kind="communication",
            earliest="2026-11-02T16:00:00Z",
            latest="2026-11-02T16:10:00Z",
            published="2026-11-02T18:00:00Z",
        )
    ])
    event = result["events"][0]
    assert event["event_time"]["earliest_utc"] == "2026-11-02T16:00:00Z"
    assert result["timelines"]["communication"][0]["earliest_utc"] == "2026-11-02T18:00:00Z"
    assert result["timelines"]["communication"][0]["time_kind"] == "publication_time"


def test_builds_interval_order_without_claiming_causality():
    result = reconstruct_timeline([
        mention("a", earliest="2026-11-02T16:00:00Z", latest="2026-11-02T16:10:00Z"),
        mention("b", earliest="2026-11-02T16:10:01Z", latest="2026-11-02T16:20:00Z"),
    ])

    assert result["temporal_relations"][0]["relation"] == "before"
    assert result["temporal_relations"][0]["causal"] is False
    assert result["temporal_relations"][0]["basis"] == "non_overlapping_time_intervals"


def test_overlapping_intervals_are_not_forced_into_a_strict_order():
    result = reconstruct_timeline([
        mention("a", earliest="2026-11-02T16:00:00Z", latest="2026-11-02T16:15:00Z"),
        mention("b", earliest="2026-11-02T16:10:00Z", latest="2026-11-02T16:20:00Z"),
    ])
    assert result["temporal_relations"][0]["relation"] == "overlaps"


def test_fractional_timestamps_sort_by_instant_not_iso_text():
    result = reconstruct_timeline([
        mention("fraction", earliest="2026-11-02T10:00:00.500000Z", latest="2026-11-02T10:00:00.500000Z"),
        mention("whole", earliest="2026-11-02T10:00:00Z", latest="2026-11-02T10:00:00Z"),
    ])
    assert [row["event_mention_id"] for row in result["timelines"]["physical"]] == ["whole", "fraction"]


def test_communication_relations_do_not_compare_publication_time_to_claimed_event_time():
    result = reconstruct_timeline([
        mention("published", kind="communication", published="2026-11-02T10:00:00Z"),
        mention("reported", kind="communication", earliest="2026-11-02T09:00:00Z", latest="2026-11-02T09:05:00Z"),
    ])
    assert result["temporal_relations"][0]["relation"] == "unknown"
    assert result["temporal_relations"][0]["basis"] == "incomparable_time_bases"
    assert len(result["candidate_links"]) == 1
    assert "incomparable_or_missing_time" in result["candidate_links"][0]["basis"]


def test_similar_mentions_remain_separate_and_are_only_possible_same_candidates():
    result = reconstruct_timeline([
        mention("a", earliest="2026-11-02T16:00:00Z", latest="2026-11-02T16:05:00Z"),
        mention("b", earliest="2026-11-02T16:00:00Z", latest="2026-11-02T16:05:00Z"),
    ])
    assert len(result["events"]) == 2
    assert len(result["candidate_links"]) == 1
    assert result["candidate_links"][0]["status"] == "possible_same"
    assert any("not merged" in item.lower() for item in result["candidate_links"][0]["limitations"])


def test_cannot_link_suppresses_possible_same_candidate():
    result = reconstruct_timeline([
        mention("a", cannot_link=["b"]),
        mention("b"),
    ])
    assert result["candidate_links"] == []


def test_dangling_cannot_link_is_preserved_as_unresolved_constraint():
    result = reconstruct_timeline([mention("a", cannot_link=["missing"])])
    assert result["unresolved_constraints"] == [{
        "event_mention_id": "a",
        "target_event_mention_id": "missing",
        "relation": "cannot_link",
        "status": "target_not_in_input",
    }]


def test_nearby_but_distinct_place_ids_do_not_produce_same_event_candidate():
    result = reconstruct_timeline([
        mention("petropavlivska", place_id="petropavlivska_borshchahivka"),
        mention("sofiivska", place_id="sofiivska_borshchahivka"),
    ])
    assert result["candidate_links"] == []


def test_unresolved_relative_time_is_preserved_but_not_placed_on_timeline():
    row = mention("relative")
    row["time"] = {
        "kind": "claimed_event_time",
        "raw_value": "увечері напередодні",
        "precision": "relative_part_of_day",
        "anchor_evidence_ids": ["evidence_relative"],
    }
    result = reconstruct_timeline([row])
    assert result["events"][0]["event_time"]["raw_value"] == "увечері напередодні"
    assert result["events"][0]["event_time"]["status"] == "unresolved"
    assert result["timelines"]["physical"] == []


def test_exact_alias_resolution_never_uses_fuzzy_names():
    entities = [
        {"entity_id": "person_1", "aliases": ["Олександр Коваленко"]},
        {"entity_id": "person_2", "aliases": ["Олександр Коваленко"]},
    ]
    assert resolve_entity_alias("Олександр Коваленко", entities)["status"] == "ambiguous"
    assert resolve_entity_alias("Олександр Коваленк", entities)["status"] == "unresolved"
    with pytest.raises(ValueError, match="aliases for person_1"):
        resolve_entity_alias("Олександр", [{"entity_id": "person_1", "aliases": "Олександр"}])


def test_rejects_invalid_or_naive_time_intervals():
    with pytest.raises(ValueError, match="timezone"):
        reconstruct_timeline([mention("a", earliest="2026-11-02T16:00:00", latest="2026-11-02T16:05:00Z")])
    with pytest.raises(ValueError, match="at or before"):
        reconstruct_timeline([mention("b", earliest="2026-11-02T16:05:00Z", latest="2026-11-02T16:00:00Z")])


def test_rejects_string_identifier_collections_and_non_string_place_ids():
    row = mention("a")
    row["actor_entity_ids"] = "actor_1"
    with pytest.raises(ValueError, match="actor_entity_ids"):
        reconstruct_timeline([row])
    row = mention("b")
    row["place_id"] = 42
    with pytest.raises(ValueError, match="place_id"):
        reconstruct_timeline([row])


def test_bundle_is_reproducible_and_contains_hashed_outputs(tmp_path: Path):
    source = tmp_path / "mentions.jsonl"
    source.write_text(json.dumps(mention("a", earliest="2026-11-02T16:00:00Z", latest="2026-11-02T16:05:00Z")) + "\n", encoding="utf-8")
    output = tmp_path / "bundle"
    receipt = build_bundle(source, output)

    assert receipt["input_event_mention_count"] == 1
    assert len(receipt["output_sha256"]) == 7
    assert (output / "events.jsonl").exists()
    assert (output / "timeline.md").read_text(encoding="utf-8").startswith("# Часова реконструкція")
    assert json.loads((output / "run_receipt.json").read_text(encoding="utf-8"))["input_sha256"] == receipt["input_sha256"]


def test_bundle_refuses_silent_overwrite(tmp_path: Path):
    source = tmp_path / "mentions.jsonl"
    source.write_text(json.dumps(mention("a")) + "\n", encoding="utf-8")
    output = tmp_path / "bundle"
    build_bundle(source, output)
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        build_bundle(source, output)


def test_explicit_overwrite_replaces_only_known_bundle_files_atomically(tmp_path: Path):
    source = tmp_path / "mentions.jsonl"
    source.write_text(json.dumps(mention("a")) + "\n", encoding="utf-8")
    output = tmp_path / "bundle"
    build_bundle(source, output)
    source.write_text(json.dumps(mention("b")) + "\n", encoding="utf-8")
    receipt = build_bundle(source, output, overwrite=True)
    rows = [json.loads(line) for line in (output / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["event_mention_id"] == "b"
    assert receipt["input_event_mention_count"] == 1
    (output / "analyst_notes.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError, match="unrelated files"):
        build_bundle(source, output, overwrite=True)
    assert (output / "analyst_notes.txt").read_text(encoding="utf-8") == "keep"


def test_cli_rejects_input_contained_in_output_directory_even_if_name_is_recognized(tmp_path: Path):
    output = tmp_path / "bundle"
    output.mkdir()
    source = output / "events.jsonl"
    source.write_text(json.dumps(mention("a")) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="input file must be outside"):
        build_bundle(source, output, overwrite=True)


def test_cli_rejects_symlink_input_inside_output_directory(tmp_path: Path):
    external_source = tmp_path / "source.jsonl"
    external_source.write_text(json.dumps(mention("a")) + "\n", encoding="utf-8")
    output = tmp_path / "bundle"
    output.mkdir()
    input_link = output / "events.jsonl"
    input_link.symlink_to(external_source)
    with pytest.raises(ValueError, match="input file must be outside"):
        build_bundle(input_link, output, overwrite=True)
    assert external_source.exists()


def test_cli_rejects_directory_masquerading_as_bundle_output(tmp_path: Path):
    source = tmp_path / "mentions.jsonl"
    source.write_text(json.dumps(mention("a")) + "\n", encoding="utf-8")
    output = tmp_path / "bundle"
    output.mkdir()
    (output / "events.jsonl").mkdir()
    with pytest.raises(FileExistsError, match="must be regular files"):
        build_bundle(source, output, overwrite=True)


def test_cli_receipt_preserves_git_commit_and_as_of_provenance(tmp_path: Path):
    source = tmp_path / "mentions.jsonl"
    source.write_text(json.dumps(mention("a")) + "\n", encoding="utf-8")
    receipt = build_bundle(
        source, tmp_path / "bundle", incident_id="case-1", run_id="run-1",
        as_of_utc="2026-11-07T00:00:00Z", git_commit="abcdef1234567",
    )
    assert receipt["git_commit"] == "abcdef1234567"
    assert receipt["provenance"]["git_commit"] == "abcdef1234567"
    assert receipt["as_of_utc"] == "2026-11-07T00:00:00Z"


def test_reconstruction_result_satisfies_versioned_schema():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads((root / "schemas/event_reconstruction.schema.json").read_text(encoding="utf-8"))
    result = reconstruct_timeline([
        mention("a", earliest="2026-11-02T16:00:00Z", latest="2026-11-02T16:05:00Z"),
        mention("b", earliest="2026-11-02T16:10:00Z", latest="2026-11-02T16:15:00Z"),
        mention("published", kind="communication", published="2026-11-02T18:00:00Z"),
        mention("reported", kind="communication", earliest="2026-11-02T17:00:00Z", latest="2026-11-02T17:05:00Z"),
    ])
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(result))
    assert errors == []


def test_physical_event_timeline_2026_11_09_artifact_verification():
    root = Path(__file__).resolve().parents[1]
    json_path = root / "reports/event-reconstruction/physical_event_timeline_2026-11-09.json"
    md_path = root / "reports/event-reconstruction/physical_event_timeline_2026-11-09.md"
    schema_path = root / "schemas/event_reconstruction.schema.json"

    assert json_path.exists(), f"Missing required artifact: {json_path}"
    assert md_path.exists(), f"Missing required artifact: {md_path}"

    data = json.loads(json_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    # Schema validity
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(data))
    assert errors == [], f"Schema errors: {errors}"

    # Physical timeline contains >= 5 events
    physical_timeline = data["timelines"]["physical"]
    assert len(physical_timeline) >= 5, f"Expected at least 5 physical events, got {len(physical_timeline)}"

    # Required 5 episode categories verified
    mention_ids = {row["event_mention_id"] for row in physical_timeline}
    expected_categories = {
        "phys_ev_01_ambulance_arrival_street",           # street confrontation
        "phys_ev_02_building_perimeter_clash",           # building confrontation
        "phys_ev_03_law_enforcement_arrival_bus_block",  # law enforcement arrival
        "phys_ev_04_pyrotechnics_and_flares_escalation", # fire ignition/flares
        "phys_ev_05_containment_and_detention",          # detention/containment
    }
    assert expected_categories.issubset(mention_ids), f"Missing categories from {mention_ids}"

    # Verify each physical event has valid bounds and participant positions
    physical_events = {ev["event_mention_id"]: ev for ev in data["events"] if ev["event_kind"] == "physical"}
    for cid in expected_categories:
        ev = physical_events[cid]
        assert ev["event_time"]["earliest_utc"] is not None
        assert ev["event_time"]["latest_utc"] is not None
        assert ev["publication_time_utc"] is None, "Physical event must not use publication time"
        assert ev["timeline_time_kind"] == "event_time"
        assert len(ev["evidence_ids"]) >= 1

        # Check participant positions in attributed_relations
        relations = ev.get("attributed_relations", [])
        position_relations = [r for r in relations if r.get("type") == "participant_position"]
        assert len(position_relations) >= 1, f"Missing participant positions for {cid}"
        evidence_assessments = [r for r in relations if r.get("type") == "evidence_assessment"]
        assert len(evidence_assessments) >= 1, f"Missing evidence assessment for {cid}"

    # Negative constraints (cannot_link) isolating Bucha warehouse fire
    unresolved_constraints = data["unresolved_constraints"]
    assert len(unresolved_constraints) >= 5
    bucha_targets = {
        uc["event_mention_id"]
        for uc in unresolved_constraints
        if uc.get("target_event_mention_id") == "phys_ev_bucha_warehouse_fire_0511"
        and uc.get("relation") == "cannot_link"
        and uc.get("status") == "target_not_in_input"
    }
    assert expected_categories.issubset(bucha_targets), f"Missing bucha isolation for {expected_categories - bucha_targets}"

    # Communication timeline separation
    communication_timeline = data["timelines"]["communication"]
    assert len(communication_timeline) >= 1
    for row in communication_timeline:
        assert row["time_kind"] == "publication_time"
        assert row["event_mention_id"] not in physical_events

    # Verify Markdown report content
    md_text = md_path.read_text(encoding="utf-8")
    assert "# Реконструкція фізичного інциденту" in md_text
    assert "phys_ev_01_ambulance_arrival_street" in md_text
    assert "phys_ev_bucha_warehouse_fire_0511" in md_text
    assert "cannot_link" in md_text


def test_physical_timeline_uses_analyst_curated_qualitative_evidence_tiers():
    root = Path(__file__).resolve().parents[1]
    source_path = root / "reports/event-reconstruction/source_event_mentions_2026-11-09.jsonl"
    result_path = root / "reports/event-reconstruction/physical_event_timeline_2026-11-09.json"
    report_path = root / "reports/event-reconstruction/physical_event_timeline_2026-11-09.md"

    source_mentions = [
        json.loads(line)
        for line in source_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    result = json.loads(result_path.read_text(encoding="utf-8"))

    physical_mentions = [mention for mention in source_mentions if mention["event_kind"] == "physical"]
    assert len(physical_mentions) == 5
    for mention in physical_mentions:
        assessments = [
            relation
            for relation in mention.get("attributed_relations", [])
            if relation.get("type") == "evidence_assessment"
        ]
        assert len(assessments) == 1
        assessment = assessments[0]
        assert "confidence_score" not in assessment
        assert "confidence_level" not in assessment
        assert assessment.get("evidence_tier") in {
            "corroborated_multi_source",
            "official_proceedings",
        }
        assert len(assessment.get("basis", "").strip()) >= 30

    for event in result["events"]:
        for relation in event.get("attributed_relations", []):
            assert "confidence_score" not in relation
            assert "confidence_level" not in relation
            assert "confidence" not in relation

    assert any("analyst-curated reconstruction" in item for item in result["limitations"])
    report = report_path.read_text(encoding="utf-8").lower()
    assert "аналітик вручну" in report
    assert "не є результатом автоматичного виявлення подій" in report
    assert "калібровані ймовірності" in report
