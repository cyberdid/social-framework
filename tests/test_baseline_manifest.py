from src.analysis.baseline_manifest import build_manifest


def test_manifest_records_hashes_missing_inputs_and_repository_scope(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    tracked = repo / "report.json"
    tracked.write_text("{\"ok\":true}\n", encoding="utf-8")
    external = tmp_path / "external.jsonl"
    external.write_text("raw\n", encoding="utf-8")

    manifest = build_manifest(repo, "abcdef1234567", [tracked, external, repo / "missing.csv"])
    assert manifest["baseline_commit"] == "abcdef1234567"
    assert manifest["files"][0]["scope"] == "repository"
    assert manifest["files"][0]["sha256"]
    assert manifest["files"][1]["scope"] == "external_local_input"
    assert manifest["files"][2]["status"] == "not_present"


def test_manifest_does_not_read_or_copy_file_contents_into_output(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    data = repo / "secretish.txt"
    data.write_text("content stays outside the manifest", encoding="utf-8")
    manifest = build_manifest(repo, "abcdef1234567", [data])
    assert "content stays outside the manifest" not in str(manifest)
