from pathlib import Path

from src.analysis.media_index_summary import render_markdown, summarize


def test_summary_counts_technical_results_without_promoting_observations():
    records = [
        {"source": {"platform": "telegram", "role_in_pilot": "incident_clip",
                    "model_observations": [{"status": "candidate"}]},
         "media_index": {"status": "ok", "decode_check": {"status": "passed"},
                         "probe": {"status": "ok"},
                         "verified_media_facts": {"size_bytes": 10, "sha256": "a" * 64,
                                                   "ffprobe": {"format": {"duration": "2.0"},
                                                               "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}}}},
        {"source": {"platform": "tiktok", "role_in_pilot": "cross_platform_pair"},
         "media_index": {"status": "decode_error", "decode_check": {"status": "error"},
                         "probe": {"status": "ok"},
                         "verified_media_facts": {"size_bytes": 20, "sha256": "b" * 64,
                                                   "ffprobe": {"format": {"duration": "3.0"},
                                                               "streams": [{"codec_type": "video"}]}}}},
    ]
    result = summarize(records)
    assert result["record_count"] == 2
    assert result["status_counts"] == {"decode_error": 1, "ok": 1}
    assert result["decode_status_counts_by_unique_asset"] == {"error": 1, "passed": 1}
    assert result["unique_asset_bytes"] == 30
    assert result["stream_type_counts_by_unique_asset"] == {"audio": 1, "video": 2}
    assert result["model_observation_count_in_source_records"] == 1
    assert result["verification_claims_added"] == 0


def test_markdown_provenance_shortens_home_directory_path():
    summary = summarize([])
    rendered = render_markdown(summary, Path.home() / "Downloads" / "index.jsonl", "a" * 64)
    assert "~/Downloads/index.jsonl" in rendered
    assert str(Path.home()) not in rendered
