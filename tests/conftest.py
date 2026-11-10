"""Tests that verify artifacts of the private case-study repository are skipped in the public code release."""
import pytest

NEEDS_PRIVATE_ARTIFACTS = {
    "test_cli_accepts_alternate_edge_graph_without_overwriting_default_report",
    "test_physical_event_timeline_2026_11_09_artifact_verification",
    "test_physical_timeline_uses_analyst_curated_qualitative_evidence_tiers",
    "test_dry_run_never_fetches_or_creates_raw_directory",
    "test_selects_corrected_known_urls_and_four_listings",
    "test_asr_correction_receipt_matches_invariants",
}


def pytest_collection_modifyitems(items):
    marker = pytest.mark.skip(reason="requires artifacts from the private case-study repository")
    for item in items:
        if item.name in NEEDS_PRIVATE_ARTIFACTS:
            item.add_marker(marker)
