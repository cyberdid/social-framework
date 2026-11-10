import hashlib
import json

import pytest

from src.analysis.channel_evidence import (
    build_channel_evidence,
    export_channel_evidence,
    extract_infrastructure_features,
    main,
)


def test_russian_links_are_only_an_infrastructure_candidate():
    record = build_channel_evidence(
        "ua_osint",
        profile_text="Український OSINT-проєкт",
        links=[f"https://news{i}.ru/story" for i in range(6)],
        source_refs=["profile:1", "posts:1-6"],
        as_of="2026-11-07T12:00:00Z",
    )
    assert record["infrastructure_candidate"] is True
    assert record["origin"]["value"] == "unknown"
    assert record["content_orientation"]["value"] == "unknown"
    assert record["operational_attribution"]["status"] == "unverified"
    assert record["legacy_group"] is None
    assert record["limitations"][0].startswith("Інфраструктурні маркери")


def test_profile_markers_are_recorded_as_features_not_operator_attribution():
    features = extract_infrastructure_features(
        "https://vk.com and https://max.ru links appear here", ["https://vk.com/example"]
    )
    assert {x["feature"] for x in features} >= {"profile_marker:vk.com", "profile_marker:max.ru", "link_domain:vk.com"}


def test_low_volume_russian_domain_links_do_not_trigger_candidate_flag():
    record = build_channel_evidence(
        "small_source", "Independent reporting", ["https://one.ru/a"], ["post:1"],
        "2026-11-07T12:00:00Z",
    )
    assert record["infrastructure_candidate"] is False
    assert record["origin"]["value"] == "unknown"


def test_record_requires_evidence_receipt_for_any_non_unknown_attribution():
    try:
        build_channel_evidence(
            "example", "", [], [], "2026-11-07T12:00:00Z",
            origin={"value": "Russia", "status": "confirmed", "evidence_ids": []},
        )
    except ValueError as exc:
        assert "evidence" in str(exc).lower()
    else:
        raise AssertionError("unsupported attribution must be rejected")


@pytest.mark.parametrize("axis_name", ["origin", "entity_type", "operator", "content_orientation"])
def test_positive_attribution_requires_and_preserves_scope_and_validity(axis_name):
    axis = {
        "value": f"assessed-{axis_name}",
        "status": "assessed",
        "evidence_ids": ["evidence-1"],
        "scope": "channel publishing activity",
        "validity_interval": {
            "start": "2020-01-01T00:00:00Z",
            "end": "2021-01-01T00:00:00Z",
        },
    }
    record = build_channel_evidence(
        "example", "", [], [], "2026-11-07T12:00:00Z", **{axis_name: axis},
    )
    assert record[axis_name]["scope"] == "channel publishing activity"
    assert record[axis_name]["validity_interval"] == {
        "start": "2020-01-01T00:00:00Z",
        "end": "2021-01-01T00:00:00Z",
    }
    assert record[axis_name]["evidence_ids"] == ["evidence-1"]


@pytest.mark.parametrize("axis_name", ["origin", "entity_type", "operator", "content_orientation"])
def test_positive_attribution_rejects_missing_scope_or_validity_interval(axis_name):
    base = {"value": f"assessed-{axis_name}", "status": "assessed", "evidence_ids": ["evidence-1"]}
    with pytest.raises(ValueError, match="scope"):
        build_channel_evidence("example", "", [], [], "2026-11-07T12:00:00Z",
                               **{axis_name: {**base, "validity_interval": {
                                   "start": "2020-01-01T00:00:00Z", "end": None}}})
    with pytest.raises(ValueError, match="validity_interval"):
        build_channel_evidence("example", "", [], [], "2026-11-07T12:00:00Z",
                               **{axis_name: {**base, "scope": "channel publishing activity"}})


def test_unknown_attribution_can_remain_unassessed_without_scope_or_interval():
    record = build_channel_evidence("example", "", [], [], "2026-11-07T12:00:00Z")
    assert record["origin"]["value"] == "unknown"
    assert "scope" not in record["origin"]
    assert "validity_interval" not in record["origin"]


def test_exporter_preserves_legacy_group_as_separate_axis_and_hashes_inputs(tmp_path):
    channels = tmp_path / "channels.jsonl"
    posts = tmp_path / "posts.jsonl"
    groups = tmp_path / "groups.csv"
    automatic = tmp_path / "auto.csv"
    output = tmp_path / "channel_evidence.jsonl"
    channels.write_text(json.dumps({"channel": "@ua_osint", "title": "UA OSINT",
                                    "description": "Example"}) + "\n", encoding="utf-8")
    post_rows = [{"channel": "ua_osint", "links": [f"https://source{i}.ru/a"]}
                 for i in range(6)]
    posts.write_text("".join(json.dumps(row) + "\n" for row in post_rows), encoding="utf-8")
    groups.write_text("channel,group\nua_osint,B\n", encoding="utf-8")
    automatic.write_text("channel,group\nua_osint,C\n", encoding="utf-8")

    assert export_channel_evidence(
        channels, posts, output, as_of="2026-11-07T12:00:00Z",
        manual_groups_paths=[groups], auto_groups_paths=[automatic],
    ) == 1
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["legacy_group"] == "B"
    assert record["infrastructure_candidate"] is True
    assert record["origin"]["value"] == "unknown"
    assert all(ref.startswith(("channel_snapshot:sha256:", "posts_snapshot:sha256:", "channel:"))
               or ref.startswith("group_mapping:") for ref in record["source_refs"])
    expected_manual_receipt = f"group_mapping:manual:priority:1:groups.csv:sha256:{hashlib.sha256(groups.read_bytes()).hexdigest()}"
    expected_auto_receipt = f"group_mapping:auto:priority:1:auto.csv:sha256:{hashlib.sha256(automatic.read_bytes()).hexdigest()}"
    assert expected_manual_receipt in record["source_refs"]
    assert expected_auto_receipt in record["source_refs"]


def test_manual_group_mapping_wins_independent_of_cli_flag_order(tmp_path):
    channels = tmp_path / "channels.jsonl"
    posts = tmp_path / "posts.jsonl"
    manual = tmp_path / "manual.csv"
    automatic = tmp_path / "automatic.csv"
    output = tmp_path / "channel_evidence.jsonl"
    channels.write_text('{"channel":"example"}\n', encoding="utf-8")
    posts.write_text("", encoding="utf-8")
    manual.write_text("channel,group\nexample,B\n", encoding="utf-8")
    automatic.write_text("channel,group\nexample,C\n", encoding="utf-8")
    common = ["--channels", str(channels), "--posts", str(posts), "--output", str(output),
              "--as-of", "2026-11-07T12:00:00Z"]

    main(common + ["--auto-groups", str(automatic), "--manual-groups", str(manual)])
    first = json.loads(output.read_text(encoding="utf-8"))
    main(common + ["--manual-groups", str(manual), "--auto-groups", str(automatic)])
    second = json.loads(output.read_text(encoding="utf-8"))

    assert first["legacy_group"] == second["legacy_group"] == "B"
    assert first["record_id"] == second["record_id"]


def test_changing_group_mapping_changes_record_id_and_receipt(tmp_path):
    channels = tmp_path / "channels.jsonl"
    posts = tmp_path / "posts.jsonl"
    groups = tmp_path / "groups.csv"
    output = tmp_path / "channel_evidence.jsonl"
    channels.write_text('{"channel":"example"}\n', encoding="utf-8")
    posts.write_text("", encoding="utf-8")
    groups.write_text("channel,group\nexample,B\n", encoding="utf-8")
    kwargs = dict(as_of="2026-11-07T12:00:00Z", manual_groups_paths=[groups])
    export_channel_evidence(channels, posts, output, **kwargs)
    first = json.loads(output.read_text(encoding="utf-8"))
    groups.write_text("channel,group\nexample,C\n", encoding="utf-8")
    export_channel_evidence(channels, posts, output, **kwargs)
    second = json.loads(output.read_text(encoding="utf-8"))
    assert first["record_id"] != second["record_id"]
    assert first["source_refs"] != second["source_refs"]


@pytest.mark.parametrize("role", ["manual", "auto"])
def test_group_mapping_receipts_encode_precedence_order(role, tmp_path):
    channels = tmp_path / "channels.jsonl"
    posts = tmp_path / "posts.jsonl"
    higher_priority = tmp_path / "first.csv"
    lower_priority = tmp_path / "second.csv"
    output = tmp_path / "channel_evidence.jsonl"
    channels.write_text('{"channel":"example"}\n', encoding="utf-8")
    posts.write_text("", encoding="utf-8")
    higher_priority.write_text("channel,group\nexample,A\n", encoding="utf-8")
    lower_priority.write_text("channel,group\nexample,B\n", encoding="utf-8")
    path_arg = f"{role}_groups_paths"

    export_channel_evidence(
        channels, posts, output, as_of="2026-11-07T12:00:00Z",
        **{path_arg: [higher_priority, lower_priority]},
    )
    first_order = json.loads(output.read_text(encoding="utf-8"))
    export_channel_evidence(
        channels, posts, output, as_of="2026-11-07T12:00:00Z",
        **{path_arg: [lower_priority, higher_priority]},
    )
    reversed_order = json.loads(output.read_text(encoding="utf-8"))

    assert first_order["legacy_group"] == "A"
    assert reversed_order["legacy_group"] == "B"
    assert first_order["record_id"] != reversed_order["record_id"]
    assert first_order["source_refs"] != reversed_order["source_refs"]
    assert any(f"group_mapping:{role}:priority:1:first.csv:sha256:" in ref
               for ref in first_order["source_refs"])
    assert any(f"group_mapping:{role}:priority:2:first.csv:sha256:" in ref
               for ref in reversed_order["source_refs"])


def test_exporter_rejects_overwriting_an_input(tmp_path):
    channels = tmp_path / "channels.jsonl"
    posts = tmp_path / "posts.jsonl"
    channels.write_text("{}\n", encoding="utf-8")
    posts.write_text("{}\n", encoding="utf-8")
    try:
        export_channel_evidence(channels, posts, channels)
    except ValueError as exc:
        assert "overwrite" in str(exc)
    else:
        raise AssertionError("input file must not be overwritten")
