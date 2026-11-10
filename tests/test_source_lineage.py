from src.analysis.source_lineage import analyze_source_lineage


def evidence(eid, text, *, claim="claim-1", sha=None, parent=None, observed=None):
    return {
        "evidence_id": eid, "claim_id": claim, "source_id": f"source-{eid}", "text": text,
        "media_sha256": sha, "derived_from_evidence_ids": [parent] if parent else [],
        "observed_at_utc": observed or "2026-11-02T10:00:00Z",
    }


def test_one_hundred_reposts_of_same_media_remain_one_dependency_family():
    rows = [evidence(f"e{i}", f"post {i}", sha="a" * 64) for i in range(100)]
    result = analyze_source_lineage(rows)
    summary = result["claim_summaries"]["claim-1"]
    assert summary["evidence_count"] == 100
    assert summary["confirmed_dependency_family_count"] == 1
    assert summary["independent_roots_lower_bound"] == 0


def test_explicit_derivation_is_confirmed_but_direction_is_not_reversed():
    rows = [evidence("root", "First report"), evidence("copy", "Copied report", parent="root")]
    result = analyze_source_lineage(rows)
    edge = result["edges"][0]
    assert edge["relation"] == "derived_from"
    assert edge["parent_evidence_id"] == "root"
    assert edge["child_evidence_id"] == "copy"
    assert edge["certainty"] == "confirmed_by_explicit_reference"


def test_exact_text_match_is_only_a_candidate_dependency():
    rows = [evidence("a", "The same claim text"), evidence("b", "The same   claim text")]
    result = analyze_source_lineage(rows)
    edge = result["edges"][0]
    assert edge["relation"] == "possible_dependency"
    assert edge["certainty"] == "candidate"
    assert result["claim_summaries"]["claim-1"]["confirmed_dependency_family_count"] == 2


def test_claims_are_not_linked_globally_and_unknown_lineage_stays_unknown():
    rows = [evidence("a", "Same text", claim="c1"), evidence("b", "Same text", claim="c2")]
    result = analyze_source_lineage(rows)
    assert result["edges"] == []
    assert result["claim_summaries"]["c1"]["unknown_lineage_count"] == 1
    assert result["claim_summaries"]["c2"]["unknown_lineage_count"] == 1
