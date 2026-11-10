import json

import pytest

from src.analysis.channel_evidence_summary import main, render_markdown, summarize


def test_summary_counts_records_and_distinct_channels_separately():
    snapshots = {
        "one.jsonl": [
            {"channel": "a", "infrastructure_candidate": True,
             "origin": {"value": "unknown"}, "content_orientation": {"value": "unknown"},
             "legacy_group": "C"},
            {"channel": "b", "infrastructure_candidate": False,
             "origin": {"value": "unknown"}, "content_orientation": {"value": "unknown"},
             "legacy_group": "U"},
        ],
        "two.jsonl": [
            {"channel": "a", "infrastructure_candidate": True,
             "origin": {"value": "unknown"}, "content_orientation": {"value": "unknown"},
             "legacy_group": "C"},
        ],
    }
    result = summarize(snapshots)
    assert result["snapshot_records"] == 3
    assert result["unique_channels"] == 2
    assert result["infrastructure_candidate_records"] == 2
    assert result["origin_unknown_records"] == 3
    assert result["content_orientation_unknown_records"] == 3


def test_summary_rejects_resolved_json_markdown_collision_before_writing(tmp_path):
    source = tmp_path / "input.jsonl"
    source.write_text(json.dumps({"channel": "example"}) + "\n", encoding="utf-8")
    destination = tmp_path / "summary.json"
    alias = tmp_path / "summary-alias.json"
    destination.write_text("preserve this", encoding="utf-8")
    alias.symlink_to(destination)

    with pytest.raises(SystemExit, match="різні шляхи"):
        main([str(source), "--json", str(destination), "--markdown", str(alias)])

    assert destination.read_text(encoding="utf-8") == "preserve this"


def test_summary_json_and_markdown_content_is_ukrainian():
    result = summarize({"знімок.jsonl": [{"channel": "example", "origin": {"value": "unknown"},
                                         "content_orientation": {"value": "unknown"}}]})
    markdown = render_markdown(result, [{"path": "знімок.jsonl", "sha256": "abc"}])
    assert markdown.startswith("# Підсумок кандидатних ознак каналів Telegram")
    assert "## Охоплення" in markdown
    assert "## Входи та походження даних" in markdown
    assert result["interpretation"].startswith("Кількість кандидатів інфраструктури")
