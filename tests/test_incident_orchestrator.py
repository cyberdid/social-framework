"""Boundary tests for the bounded incident runner; network is replaced at fetch only."""
import importlib
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import source_watch_collect
from src.contracts import validate_run_manifest


def orchestrator():
    return importlib.import_module("src.analysis.incident_orchestrator")


def inputs(tmp_path, *, platforms=None, budget=10):
    spec = json.loads((Path(__file__).parent / "fixtures/incident_spec.valid.json").read_text())
    spec["platforms"] = platforms or ["web", "telegram", "youtube"]
    spec["budgets"]["max_new_sources_per_run"] = budget
    spec["as_of_utc"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat().replace("+00:00", "Z")
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    audit = tmp_path / "audit.json"
    audit.write_text(json.dumps({"rows": [{"url": "https://mvs.gov.ua/news/example", "status": 200}]}))
    return spec_path, audit


def run(tmp_path, **options):
    spec, audit = inputs(tmp_path)
    return orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
                                       git_commit="abcdef1234567", **options)


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def set_watch_time(monkeypatch, moment):
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(source_watch_collect, "datetime", FrozenDateTime)


def article_response(text="Unique candidate phrase appears in this public notice. " * 4):
    body = ("<html><title>Public notice</title><article><h1>Public notice</h1><p>"
            + text + "</p></article></html>").encode()
    return {"status": 200, "final_url": "https://mvs.gov.ua/news/example", "body": body,
            "content_type": "text/html", "truncated": False, "headers": {}}


def response(status=200, **extra):
    return {"status": status, "final_url": "https://mvs.gov.ua/news/example",
            "body": b"<html><title>Public record</title><article>Bounded public record</article></html>",
            "content_type": "text/html", "truncated": False, "headers": {}, **extra}


def test_dry_run_writes_valid_auditable_artifacts_without_network(tmp_path, monkeypatch):
    def network_forbidden(*args, **kwargs):
        pytest.fail("dry-run performed network collection")
    monkeypatch.setattr(source_watch_collect, "fetch_public", network_forbidden)
    manifest = run(tmp_path)
    validate_run_manifest(manifest)
    assert manifest["state"] == "completed_bounded"
    folder = tmp_path / "run"
    assert all((folder / name).exists() for name in ["run_manifest.json", "coverage_plan.json",
                                                   "task_receipts.jsonl", "unresolved_queue.jsonl"])
    receipts = jsonl(folder / "task_receipts.jsonl")
    watch = next(row for row in receipts if row["task_id"] == "source_watch")
    assert watch["status"] == "planned" and watch["executed"] is False
    assert not (folder / "raw").exists()
    assert {row["stage"] for row in receipts if row["status"] == "not_run"} >= {
        "normalized", "extracting", "verifying", "reconstructing", "reporting"}
    coverage = json.loads((folder / "coverage_plan.json").read_text())
    assert [(row["platform"], row["status"], row["reason"]) for row in coverage["platforms"]
            if row["platform"] != "web"] == [
        ("telegram", "not_run", "unsupported_adapter"), ("youtube", "not_run", "unsupported_adapter")]
    assert any(row["reason"] == "unsupported_adapter" for row in jsonl(folder / "unresolved_queue.jsonl"))


def test_execute_archives_only_fixed_plan_with_explicit_bounds_and_hashes(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path, budget=2)
    before = {path: path.read_bytes() for path in (spec, audit)}
    observed = []
    def fetch(url, *, timeout, max_bytes):
        source_watch_collect.validate_public_url(url)
        assert timeout == 3 and max_bytes == 2048
        observed.append(url)
        return response()
    monkeypatch.setattr(source_watch_collect, "fetch_public", fetch)
    manifest = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", execute=True, timeout=3, pause=0, max_bytes=2048)
    validate_run_manifest(manifest)
    assert manifest["state"] == "completed_bounded"
    assert len(observed) == 2
    snapshots = manifest["source_snapshots"]
    assert len(snapshots) == 1 and snapshots[0]["access_status"] == "complete"
    archived = tmp_path / "run" / snapshots[0]["snapshot_artifact_path"]
    raw = json.loads(archived.read_text())
    assert raw["network_policy"]["timeout_seconds"] == 3
    assert all((archived.parent / row["body_file"]).exists() for row in raw["rows"])
    assert (tmp_path / "run/reports/source_watch/latest.json").exists()
    assert all(path.read_bytes() == content for path, content in before.items())
    coverage = json.loads((tmp_path / "run/coverage_plan.json").read_text())
    assert coverage["deferred_target_count"] == 3


@pytest.mark.parametrize("status,extra,want", [(403, {}, "blocked"), (404, {}, "not_found"),
    (500, {}, "failed"), (200, {"truncated": True}, "partial")])
def test_http_failures_have_bounded_receipts_and_failed_state(tmp_path, monkeypatch, status, extra, want):
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *args, **kwargs: response(status, **extra))
    manifest = run(tmp_path, execute=True, pause=0, max_targets=1)
    assert manifest["state"] == "failed_with_receipt"
    assert manifest["source_snapshots"][0]["access_status"] == want
    receipts = jsonl(tmp_path / "run/task_receipts.jsonl")
    assert any(row["task_id"] == "source_watch:target:0" and row["status"] == want for row in receipts)
    assert any(row["status"] == want for row in jsonl(tmp_path / "run/unresolved_queue.jsonl"))


def test_collector_exception_is_receipted_without_logging_exception_content(tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise TimeoutError("PRIVATE-CONTENT-MUST-NOT-BE-LOGGED")
    monkeypatch.setattr(source_watch_collect, "run", fail)
    manifest = run(tmp_path, execute=True, pause=0)
    assert manifest["state"] == "failed_with_receipt"
    receipts = jsonl(tmp_path / "run/task_receipts.jsonl")
    assert any(row["status"] == "failed" and row.get("error_type") == "TimeoutError" for row in receipts)
    assert all("PRIVATE-CONTENT" not in path.read_text() for path in (tmp_path / "run").glob("*.json*"))


@pytest.mark.parametrize("configuration", [{"command": "touch /tmp/unsafe"},
    {"collector": "src.archive_sources"}, {"host_allowlist": ["evil.invalid"]}])
def test_arbitrary_execution_configuration_is_rejected_before_artifacts(tmp_path, configuration):
    spec, audit = inputs(tmp_path)
    payload = json.loads(spec.read_text())
    payload.update(configuration)
    spec.write_text(json.dumps(payload))
    with pytest.raises(orchestrator().OrchestratorError):
        orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit, git_commit="abcdef1234567")
    assert not (tmp_path / "run").exists()


def test_existing_run_is_refused_without_overwrite_even_for_same_inputs(tmp_path):
    run(tmp_path)
    original = {path: path.read_bytes() for path in (tmp_path / "run").rglob("*") if path.is_file()}
    with pytest.raises(orchestrator().OrchestratorError, match="exists"):
        run(tmp_path, execute=True)
    assert all(path.read_bytes() == content for path, content in original.items())


def test_non_allowlisted_audit_target_fails_closed_and_never_fetches(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path)
    audit.write_text(json.dumps({"rows": [{"url": "https://dbr.gov.ua:8443/news/unsafe", "status": 200}]}))
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: pytest.fail("unsafe fetch"))
    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", execute=True)
    assert result["state"] == "failed_with_receipt"
    assert not (tmp_path / "run/raw").exists()


def test_zero_budget_blocks_network_and_no_web_has_no_adapter_execution(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path, budget=0)
    monkeypatch.setattr(source_watch_collect, "run", lambda *a, **kw: pytest.fail("budget bypass"))
    manifest = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", execute=True)
    assert manifest["state"] == "completed_bounded"
    assert any(row["reason"] == "source_budget_zero" for row in jsonl(tmp_path / "run/unresolved_queue.jsonl"))
    payload = json.loads(spec.read_text())
    payload["platforms"] = ["telegram"]
    spec.write_text(json.dumps(payload))
    audit.unlink()  # unused inputs must not be read
    manifest = orchestrator().run_incident(spec, tmp_path / "no-web", audit_path=audit,
        git_commit="abcdef1234567", execute=True)
    assert manifest["source_snapshots"] == []


def test_symlink_output_is_rejected_and_cli_defaults_to_dry_run(tmp_path, monkeypatch, capsys):
    spec, audit = inputs(tmp_path)
    target = tmp_path / "external"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(orchestrator().OrchestratorError, match="symlink"):
        orchestrator().run_incident(spec, linked / "run", audit_path=audit, git_commit="abcdef1234567")
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: pytest.fail("CLI fetched"))
    assert orchestrator().main(["--spec", str(spec), "--run-dir", str(tmp_path / "cli-run"),
        "--audit", str(audit), "--git-commit", "abcdef1234567"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "completed_bounded"
    assert not (tmp_path / "cli-run/raw").exists()


def test_public_web_policy_blocks_watch_and_seed_urls_never_expand_targets(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path)
    payload = json.loads(spec.read_text())
    payload["collection_policy"]["access_methods"] = ["public_api"]
    spec.write_text(json.dumps(payload))
    monkeypatch.setattr(source_watch_collect, "run", lambda *a, **kw: pytest.fail("policy bypass"))
    result = orchestrator().run_incident(spec, tmp_path / "blocked", audit_path=tmp_path / "absent-audit",
        git_commit="abcdef1234567", execute=True)
    assert result["source_snapshots"] == []
    assert any(row["reason"] == "public_web_not_authorized"
               for row in jsonl(tmp_path / "blocked/unresolved_queue.jsonl"))
    payload["collection_policy"]["access_methods"] = ["public_web"]
    payload["seed_urls"] = ["https://untrusted.invalid/never-fetch"]
    payload["models"]["text_provider"] = "arbitrary.module:callback"
    spec.write_text(json.dumps(payload))
    result = orchestrator().run_incident(spec, tmp_path / "seed-plan", audit_path=audit,
        git_commit="abcdef1234567")
    coverage = json.loads((tmp_path / "seed-plan/coverage_plan.json").read_text())
    assert coverage["seed_urls_not_dispatched"] == 1
    assert all("untrusted.invalid" not in row["url"] for row in coverage["targets"])
    assert result["model_config"] == {"enabled": False}


def test_mixed_collection_outcomes_remain_partial_with_intact_bodies(tmp_path, monkeypatch):
    replies = iter([response(), response(404)])
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: next(replies))
    result = run(tmp_path, execute=True, pause=0, max_targets=2)
    assert result["state"] == "failed_with_receipt"
    assert result["source_snapshots"][0]["access_status"] == "partial"
    statuses = {row["status"] for row in jsonl(tmp_path / "run/task_receipts.jsonl")
                if row["task_id"].startswith("source_watch:target:")}
    assert statuses == {"complete", "not_found"}
    for output in result["outputs"]:
        import hashlib
        assert hashlib.sha256((tmp_path / "run" / output["path"]).read_bytes()).hexdigest() == output["sha256"]


@pytest.mark.parametrize("bounds", [{"timeout": 0}, {"timeout": float("nan")}, {"pause": 61},
                                  {"max_bytes": 1}, {"max_targets": 101}])
def test_invalid_resource_bounds_are_rejected_before_creating_run(tmp_path, bounds):
    spec, audit = inputs(tmp_path)
    with pytest.raises(orchestrator().OrchestratorError, match="resource bounds"):
        orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
            git_commit="abcdef1234567", **bounds)
    assert not (tmp_path / "run").exists()


def test_missing_audit_failure_is_auditable_and_cli_exits_nonzero(tmp_path, capsys):
    spec, audit = inputs(tmp_path)
    audit.unlink()
    result = orchestrator().main(["--spec", str(spec), "--run-dir", str(tmp_path / "run"),
                                 "--audit", str(audit), "--git-commit", "abcdef1234567", "--execute"])
    assert result == 1
    manifest = json.loads((tmp_path / "run/run_manifest.json").read_text())
    validate_run_manifest(manifest)
    assert manifest["state"] == "failed_with_receipt"
    assert any(row["task_id"] == "source_watch" and row["status"] == "failed"
               for row in jsonl(tmp_path / "run/task_receipts.jsonl"))
    assert not (tmp_path / "run/raw").exists()
    assert orchestrator().main(["--spec", str(spec), "--run-dir", str(tmp_path / "run"),
                               "--audit", str(audit), "--git-commit", "abcdef1234567"]) == 2
    capsys.readouterr()


def test_explicit_local_analysis_runs_typed_posts_without_network_and_hashes_nested_manifest(tmp_path, monkeypatch):
    import hashlib

    spec, audit = inputs(tmp_path)
    posts = tmp_path / "posts.jsonl"
    posts.write_text(json.dumps({
        "post_id": "p1", "platform": "telegram", "channel_id": "c1",
        "publication_time": "2026-11-01T10:15:00Z", "observed_at": "2026-11-01T10:20:00Z",
        "view_count_snapshot": 25,
    }) + "\n")
    typed_manifest = tmp_path / "typed-inputs.json"
    typed_manifest.write_text(json.dumps({"information_posts": str(posts)}))
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: pytest.fail("local mode fetched"))

    manifest = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", run_local_analysis=True, typed_input_manifest_path=typed_manifest)

    validate_run_manifest(manifest)
    assert manifest["state"] == "completed_bounded"
    folder = tmp_path / "run"
    output = json.loads((folder / "analysis/information_dynamics.json").read_text())
    assert output["platform_summary"]["telegram"]["unique_post_count"] == 1
    summary = json.loads((folder / "analysis/case_summary.json").read_text())
    assert summary["analysis_status"] == "partial"
    assert summary["semantic_accuracy"] == "not_evaluated"
    receipts = {row["task_id"]: row for row in jsonl(folder / "task_receipts.jsonl")}
    assert receipts["normalized"]["status"] == "not_run"
    assert receipts["extracting"]["status"] == "not_run"
    assert receipts["verifying"]["status"] != "complete"
    assert receipts["local:information_dynamics"]["status"] == "complete"
    assert receipts["local_analysis"]["typed_input_manifest_sha256"] == hashlib.sha256(typed_manifest.read_bytes()).hexdigest()
    assert receipts["local_analysis"]["unresolved_path"] == "analysis/unresolved_queue.jsonl"
    inner = {row["task_id"]: row for row in jsonl(folder / "analysis/task_receipts.jsonl")}
    assert receipts["local:information_dynamics"]["attempted_at_utc"] == inner["information_dynamics"]["attempted_at_utc"]
    assert receipts["local:information_dynamics"]["executed"] == inner["information_dynamics"]["executed"]
    assert receipts["local:information_dynamics"]["reason"] == inner["information_dynamics"]["reason"]
    nested = folder / "analysis/run_manifest.json"
    assert any(row["path"] == "analysis/run_manifest.json" and
               row["sha256"] == hashlib.sha256(nested.read_bytes()).hexdigest()
               for row in manifest["outputs"])
    assert all(hashlib.sha256((folder / row["path"]).read_bytes()).hexdigest() == row["sha256"]
               for row in manifest["outputs"])
    assert "run_manifest.json" not in {row["path"] for row in manifest["outputs"]}


def test_local_manifest_is_not_opened_without_local_analysis_opt_in(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path)
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: pytest.fail("unexpected fetch"))
    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", typed_input_manifest_path=tmp_path / "missing-inputs.json")
    assert result["state"] == "completed_bounded"
    assert not (tmp_path / "run/analysis").exists()
    receipts = {row["task_id"]: row for row in jsonl(tmp_path / "run/task_receipts.jsonl")}
    assert receipts["local_analysis"]["reason"] == "local_analysis_not_requested"
    assert all(receipts[name]["status"] == "not_run" for name in
               ("normalized", "extracting", "verifying", "reconstructing", "reporting"))


@pytest.mark.parametrize("input_json", [None, "{", "[]", "{}", '{"unknown": "private-path"}',
                                      '{"information_posts": "a", "information_posts": "b"}',
                                      '{"information_posts": 2}', '{"information_posts": "../private"}'])
def test_invalid_or_missing_typed_manifest_leaves_bounded_failure_receipt(tmp_path, input_json):
    spec, audit = inputs(tmp_path)
    path = None
    if input_json is not None:
        path = tmp_path / "typed.json"
        path.write_text(input_json)
    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", run_local_analysis=True, typed_input_manifest_path=path)
    validate_run_manifest(result)
    assert result["state"] == "failed_with_receipt"
    receipts = {row["task_id"]: row for row in jsonl(tmp_path / "run/task_receipts.jsonl")}
    assert receipts["local_analysis"]["status"] == "failed"
    assert receipts["local_analysis"]["executed"] is False
    assert receipts["run_finished"]["reason"] == "local_analysis_failed"
    assert all(receipts[name]["status"] == "not_run" for name in
               ("normalized", "extracting", "verifying", "reconstructing", "reporting"))
    assert not (tmp_path / "run/analysis").exists()
    assert all("private-path" not in p.read_text() for p in (tmp_path / "run").glob("*.json*"))


def test_local_runner_exception_has_failure_receipt_without_exception_message(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path)
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"information_posts": str(tmp_path / "posts.jsonl")}))
    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE-CONTENT-MUST-NOT-BE-LOGGED")
    monkeypatch.setattr(orchestrator(), "run_offline_case", fail)
    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", run_local_analysis=True, typed_input_manifest_path=typed)
    assert result["state"] == "failed_with_receipt"
    receipts = {row["task_id"]: row for row in jsonl(tmp_path / "run/task_receipts.jsonl")}
    assert receipts["local_analysis"]["error_type"] == "RuntimeError"
    assert receipts["run_finished"]["reason"] == "local_analysis_failed"
    assert all("PRIVATE-CONTENT" not in p.read_text() for p in (tmp_path / "run").glob("*.json*"))


def test_inconsistent_nested_receipt_and_summary_cannot_be_reported_as_success(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path)
    posts = tmp_path / "posts.jsonl"
    posts.write_text(json.dumps({"post_id": "p1", "platform": "telegram", "channel_id": "c1",
                                 "publication_time": "2026-11-01T10:15:00Z"}) + "\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"information_posts": str(posts)}))
    module = orchestrator()
    actual_runner = module.run_offline_case
    def corrupt(*args, **kwargs):
        result = actual_runner(*args, **kwargs)
        path = Path(args[1]) / "task_receipts.jsonl"
        rows = jsonl(path)
        next(row for row in rows if row["task_id"] == "information_dynamics")["status"] = "not_run"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return result
    monkeypatch.setattr(module, "run_offline_case", corrupt)
    result = module.run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", run_local_analysis=True, typed_input_manifest_path=typed)
    assert result["state"] == "failed_with_receipt"
    receipts = {row["task_id"]: row for row in jsonl(tmp_path / "run/task_receipts.jsonl")}
    assert receipts["local_analysis"]["status"] == "failed"
    assert receipts["run_finished"]["reason"] == "local_analysis_failed"
    assert (tmp_path / "run/analysis/information_dynamics.json").exists()


@pytest.mark.parametrize(("nested_status", "aggregate"), [("failed", "partial"), ("not_run", "complete")])
def test_nested_stage_failure_or_omission_cannot_be_hidden_by_aggregate(tmp_path, monkeypatch, nested_status, aggregate):
    spec, audit = inputs(tmp_path)
    posts = tmp_path / "posts.jsonl"
    posts.write_text(json.dumps({"post_id": "p1", "platform": "telegram", "channel_id": "c1",
                                 "publication_time": "2026-11-01T10:15:00Z"}) + "\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"information_posts": str(posts)}))
    module = orchestrator()
    actual_runner = module.run_offline_case
    def corrupt(*args, **kwargs):
        result = actual_runner(*args, **kwargs)
        folder = Path(args[1])
        summary_path = folder / "case_summary.json"
        summary = json.loads(summary_path.read_text())
        summary["stage_status"]["information_dynamics"] = nested_status
        summary["analysis_status"] = aggregate
        summary_path.write_text(json.dumps(summary))
        receipts_path = folder / "task_receipts.jsonl"
        rows = jsonl(receipts_path)
        target = next(row for row in rows if row["task_id"] == "information_dynamics")
        target["status"] = nested_status
        target["executed"] = nested_status != "not_run"
        receipts_path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        result["analysis_status"] = aggregate
        return result
    monkeypatch.setattr(module, "run_offline_case", corrupt)
    result = module.run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", run_local_analysis=True, typed_input_manifest_path=typed)
    assert result["state"] == "failed_with_receipt"
    receipts = {row["task_id"]: row for row in jsonl(tmp_path / "run/task_receipts.jsonl")}
    assert receipts["local_analysis"]["status"] == "failed"
    assert receipts["run_finished"]["reason"] == "local_analysis_failed"


def test_local_claim_retrieval_keeps_verification_unverified(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path)
    spec_data = json.loads(spec.read_text())
    spec_data["budgets"]["max_queries_per_claim"] = 1
    spec.write_text(json.dumps(spec_data))
    claims = tmp_path / "claims.jsonl"
    claims.write_text(json.dumps({"claim_id": "c1", "statement": "людей утримували у приміщенні",
                                  "scope": {"event": "demo"}}) + "\n")
    passages = tmp_path / "passages.jsonl"
    passages.write_text(json.dumps({"passage_id": "p1", "source_id": "s1", "text": "Людей утримували у приміщенні.",
                                    "captured_at_utc": "2026-11-01T20:00:00Z"}) + "\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"claims": str(claims), "passages": str(passages)}))
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: pytest.fail("unexpected fetch"))
    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", run_local_analysis=True, typed_input_manifest_path=typed)
    assert result["state"] == "completed_bounded"
    verdict = jsonl(tmp_path / "run/analysis/verification_results.jsonl")[0]
    assert verdict["status"] == "unverified"
    assert verdict["supporting_evidence_ids"] == []
    assert verdict["contradicting_evidence_ids"] == []
    receipts = {row["task_id"]: row for row in jsonl(tmp_path / "run/task_receipts.jsonl")}
    assert receipts["verifying"]["status"] == "partial"
    assert receipts["local:claim_retrieval"]["reason"] == "candidates_only_no_evidence_assessments"


def test_zero_query_budget_skips_claim_search_but_runs_other_local_stages(tmp_path):
    spec, audit = inputs(tmp_path)
    claims = tmp_path / "claims.jsonl"
    claims.write_text(json.dumps({"claim_id": "c1", "statement": "A claim", "scope": {"event": "demo"}}) + "\n")
    passages = tmp_path / "passages.jsonl"
    passages.write_text(json.dumps({"passage_id": "p1", "source_id": "s1", "text": "A claim"}) + "\n")
    posts = tmp_path / "posts.jsonl"
    posts.write_text(json.dumps({"post_id": "p1", "platform": "telegram", "channel_id": "c1",
                                 "publication_time": "2026-11-01T10:15:00Z"}) + "\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"claims": str(claims), "passages": str(passages), "information_posts": str(posts)}))
    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", run_local_analysis=True, typed_input_manifest_path=typed)
    assert result["state"] == "completed_bounded"
    assert (tmp_path / "run/analysis/information_dynamics.json").is_file()
    assert (tmp_path / "run/analysis/evidence_candidates.jsonl").read_text() == ""
    assert (tmp_path / "run/analysis/verification_results.jsonl").read_text() == ""
    receipts = {row["task_id"]: row for row in jsonl(tmp_path / "run/task_receipts.jsonl")}
    assert receipts["local:claim_retrieval"]["status"] == "not_run"
    assert receipts["local:claim_retrieval"]["reason"] == "query_budget_zero"
    assert receipts["verifying"]["status"] == "not_run"
    assert receipts["verifying"]["reason"] == "query_budget_zero"
    nested_inputs = json.loads((tmp_path / "run/analysis/input_manifest.json").read_text())["inputs"]
    assert {"claims", "passages", "information_posts"} <= set(nested_inputs)
    summary = json.loads((tmp_path / "run/analysis/case_summary.json").read_text())
    assert summary["stage_status"]["claim_retrieval"] == "not_run"


@pytest.mark.parametrize("linked", ["manifest", "input"])
def test_local_analysis_rejects_symlink_manifest_or_input(tmp_path, linked):
    spec, audit = inputs(tmp_path)
    posts = tmp_path / "posts.jsonl"
    posts.write_text('{}\n')
    target = posts
    if linked == "input":
        target = tmp_path / "linked-posts"
        target.symlink_to(posts)
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"information_posts": str(target)}))
    if linked == "manifest":
        target = tmp_path / "linked-manifest"
        target.symlink_to(typed)
    else:
        target = typed
    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", run_local_analysis=True, typed_input_manifest_path=target)
    assert result["state"] == "failed_with_receipt"
    receipt = next(row for row in jsonl(tmp_path / "run/task_receipts.jsonl")
                   if row["task_id"] == "local_analysis")
    assert receipt["status"] == "failed" and receipt["executed"] is False
    assert not (tmp_path / "run/analysis").exists()


def test_oversized_typed_manifest_is_rejected_with_receipt(tmp_path):
    spec, audit = inputs(tmp_path)
    typed = tmp_path / "typed.json"
    typed.write_text(" " * 1_000_001 + "{}")
    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", run_local_analysis=True, typed_input_manifest_path=typed)
    assert result["state"] == "failed_with_receipt"
    receipt = next(row for row in jsonl(tmp_path / "run/task_receipts.jsonl")
                   if row["task_id"] == "local_analysis")
    assert receipt["status"] == "failed" and receipt["executed"] is False
    assert not (tmp_path / "run/analysis").exists()


def test_fifo_typed_manifest_is_rejected_without_blocking(tmp_path):
    import os
    import subprocess
    import sys

    spec, audit = inputs(tmp_path)
    typed = tmp_path / "typed-fifo"
    os.mkfifo(typed)
    result = subprocess.run([sys.executable, "-m", "src.analysis.incident_orchestrator",
        "--spec", str(spec), "--run-dir", str(tmp_path / "run"), "--audit", str(audit),
        "--git-commit", "abcdef1234567", "--run-local-analysis", "--typed-input-manifest", str(typed)],
        capture_output=True, text=True, timeout=2)
    assert result.returncode == 1
    receipt = next(row for row in jsonl(tmp_path / "run/task_receipts.jsonl")
                   if row["task_id"] == "local_analysis")
    assert receipt["status"] == "failed" and receipt["executed"] is False


def test_cli_local_flag_runs_pretyped_data_without_enabling_collection(tmp_path, monkeypatch, capsys):
    spec, audit = inputs(tmp_path)
    posts = tmp_path / "posts.jsonl"
    posts.write_text(json.dumps({"post_id": "p1", "platform": "telegram", "channel_id": "c1",
                                 "publication_time": "2026-11-01T10:15:00Z"}) + "\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"information_posts": str(posts)}))
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: pytest.fail("unexpected fetch"))
    rc = orchestrator().main(["--spec", str(spec), "--run-dir", str(tmp_path / "run"),
        "--audit", str(audit), "--git-commit", "abcdef1234567", "--run-local-analysis",
        "--typed-input-manifest", str(typed)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["state"] == "completed_bounded"
    assert (tmp_path / "run/analysis/information_dynamics.json").is_file()
    assert not (tmp_path / "run/raw").exists()


def test_independent_local_analysis_runs_even_if_requested_collection_fails(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path)
    posts = tmp_path / "posts.jsonl"
    posts.write_text(json.dumps({"post_id": "p1", "platform": "telegram", "channel_id": "c1",
                                 "publication_time": "2026-11-01T10:15:00Z"}) + "\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"information_posts": str(posts)}))
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: response(500))
    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", execute=True, pause=0, max_targets=1,
        run_local_analysis=True, typed_input_manifest_path=typed)
    assert result["state"] == "failed_with_receipt"
    receipts = {row["task_id"]: row for row in jsonl(tmp_path / "run/task_receipts.jsonl")}
    assert receipts["run_finished"]["reason"] == "collection_failed"
    assert receipts["local:information_dynamics"]["status"] == "complete"
    assert (tmp_path / "run/analysis/information_dynamics.json").exists()


def test_fresh_capture_is_bridged_merged_and_fed_to_offline_runner(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path, budget=3)
    spec_data = json.loads(spec.read_text())
    spec_data["as_of_utc"] = "2026-11-08T13:00:00Z"
    spec_data["budgets"]["max_queries_per_claim"] = 2
    spec.write_text(json.dumps(spec_data))
    moment = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    set_watch_time(monkeypatch, moment)
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: article_response())

    claims = tmp_path / "claims.jsonl"
    claims.write_text(json.dumps({"claim_id": "claim-one", "statement": "unique candidate phrase public notice",
                                 "scope": {"event": "demo"}}) + "\n")
    caller_passages = tmp_path / "caller-passages.jsonl"
    caller_passages.write_text(json.dumps({"passage_id": "caller-passage", "source_id": "caller-source",
        "text": "An earlier independently typed passage.", "captured_at_utc": "2026-11-01T10:00:00Z"}) + "\n")
    typed_manifest = tmp_path / "typed-inputs.json"
    typed_manifest.write_text(json.dumps({"claims": str(claims), "passages": str(caller_passages)}))
    source_bytes = {path: path.read_bytes() for path in (claims, caller_passages, typed_manifest)}
    module = orchestrator()
    bridge_roots = []
    ingest = module.ingest_source_watch_manifest

    def record_bridge_root(*args, **kwargs):
        bridge_roots.append(Path(kwargs["root"]))
        return ingest(*args, **kwargs)

    monkeypatch.setattr(module, "ingest_source_watch_manifest", record_bridge_root)

    result = module.run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", execute=True, pause=0, max_targets=1,
        run_local_analysis=True, typed_input_manifest_path=typed_manifest)

    folder = tmp_path / "run"
    assert result["state"] == "completed_bounded"
    assert bridge_roots == [folder]
    assert all(path.read_bytes() == contents for path, contents in source_bytes.items())
    receipt = json.loads((folder / "source_ingest_receipt.json").read_text())
    assert receipt["status"] == "complete"
    assert receipt["passage_count"] == 1
    assert receipt["source_count"] == 1 and receipt["artifact_count"] == 1
    assert all(not Path(receipt[key]).is_absolute()
               for key in ("source_manifest_path", "ledger_path", "merged_passages_path"))
    manifest_path = folder / receipt["source_manifest_path"]
    ledger_path = folder / receipt["ledger_path"]
    merged_path = folder / receipt["merged_passages_path"]
    assert receipt["source_manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert receipt["ledger_sha256"] == hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    assert receipt["merged_passages_sha256"] == hashlib.sha256(merged_path.read_bytes()).hexdigest()
    merged = jsonl(merged_path)
    assert len(merged) == 2
    assert {row["passage_id"] for row in merged} == {"caller-passage", merged[1]["passage_id"]}
    captured = next(row for row in merged if row["passage_id"] != "caller-passage")
    assert captured["captured_at_utc"] == "2026-11-08T12:00:00+00:00"
    assert captured["artifact_id"] and captured["artifact_sha256"]
    assert captured["artifact_path"].startswith("raw/source_watch/")
    assert captured["source_uri"] == "https://mvs.gov.ua/news/example"
    assert captured["locator"]["manifest_path"] == receipt["source_manifest_path"]
    assert captured["locator"]["manifest_sha256"] == receipt["source_manifest_sha256"]
    assert captured["artifact_start_offset"] is None
    retrieval = jsonl(folder / "analysis/evidence_candidates.jsonl")[0]
    candidate = next(row for row in retrieval["candidates"] if row["passage_id"] == captured["passage_id"])
    assert candidate["artifact_start_offset"] is None
    assert candidate["artifact_offset_status"] == "unavailable"
    assert candidate["passage_offset_unit"] == "unicode_codepoint"
    assert candidate["locator"] == captured["locator"]
    input_manifest = json.loads((folder / "analysis/input_manifest.json").read_text())
    assert input_manifest["inputs"]["passages"]["file_name"] == "merged_passages.jsonl"
    assert jsonl(folder / "analysis/verification_results.jsonl")[0]["status"] == "unverified"
    receipt_text = (folder / "source_ingest_receipt.json").read_text()
    assert "Unique candidate phrase" not in receipt_text and "https://" not in receipt_text
    import sqlite3
    with sqlite3.connect(ledger_path) as connection:
        ledger_types = {row[0] for row in connection.execute("SELECT DISTINCT record_type FROM records")}
    assert ledger_types == {"Source", "Artifact", "Passage"}
    task_receipts = {row["task_id"]: row for row in jsonl(folder / "task_receipts.jsonl")}
    assert task_receipts["normalized"]["status"] == "not_run"
    assert task_receipts["extracting"]["status"] == "not_run"
    assert task_receipts["verifying"]["status"] == "partial"


def test_artifact_only_fresh_capture_has_partial_bridge_receipt(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path, budget=2)
    spec_data = json.loads(spec.read_text())
    spec_data["as_of_utc"] = "2026-11-08T13:00:00Z"
    spec.write_text(json.dumps(spec_data))
    set_watch_time(monkeypatch, datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(source_watch_collect, "fetch_public",
                        lambda *a, **kw: article_response("tiny"))
    posts = tmp_path / "posts.jsonl"
    posts.write_text(json.dumps({"post_id": "post-one", "platform": "telegram", "channel_id": "channel-one",
                                 "publication_time": "2026-11-01T10:15:00Z"}) + "\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"information_posts": str(posts)}))

    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", execute=True, pause=0, max_targets=1,
        run_local_analysis=True, typed_input_manifest_path=typed)

    folder = tmp_path / "run"
    receipt = json.loads((folder / "source_ingest_receipt.json").read_text())
    assert result["state"] == "completed_bounded"
    assert receipt["status"] == "partial"
    assert receipt["passage_count"] == 0
    assert receipt["artifact_count"] == 1
    assert receipt["merged_passages_count"] == 0
    assert (folder / receipt["merged_passages_path"]).read_text() == ""
    assert (folder / "evidence_ledger.sqlite").is_file()


def test_as_of_before_fresh_capture_fails_with_explicit_time_scope_mismatch(tmp_path, monkeypatch):
    spec, audit = inputs(tmp_path, budget=2)
    spec_data = json.loads(spec.read_text())
    spec_data["as_of_utc"] = "2026-11-08T11:59:00Z"
    spec_data["budgets"]["max_queries_per_claim"] = 1
    spec.write_text(json.dumps(spec_data))
    set_watch_time(monkeypatch, datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: article_response())
    claims = tmp_path / "claims.jsonl"
    claims.write_text(json.dumps({"claim_id": "claim-one", "statement": "public notice",
                                 "scope": {"event": "demo"}}) + "\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"claims": str(claims)}))

    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", execute=True, pause=0, max_targets=1,
        run_local_analysis=True, typed_input_manifest_path=typed)

    folder = tmp_path / "run"
    receipt = json.loads((folder / "source_ingest_receipt.json").read_text())
    task_receipts = {row["task_id"]: row for row in jsonl(folder / "task_receipts.jsonl")}
    assert result["state"] == "failed_with_receipt"
    assert receipt["status"] == "failed"
    assert receipt["reason"] == "time_scope_contract_mismatch"
    assert task_receipts["local:claim_retrieval"]["status"] == "failed"
    assert task_receipts["local:claim_retrieval"]["reason"] == "time_scope_contract_mismatch"


def test_passage_id_conflict_fails_closed_without_mutating_source_input(tmp_path, monkeypatch):
    from src.analysis import source_ingest_bridge
    from src.evidence_ledger import EvidenceLedger

    spec, audit = inputs(tmp_path, budget=2)
    spec_data = json.loads(spec.read_text())
    spec_data["as_of_utc"] = "2026-11-08T13:00:00Z"
    spec.write_text(json.dumps(spec_data))
    set_watch_time(monkeypatch, datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc))
    original_run = source_watch_collect.run
    passages = tmp_path / "caller-passages.jsonl"
    passages.write_text("{}\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"passages": str(passages)}))
    seeded_input = {}

    def collect_then_seed_conflict(*args, **kwargs):
        summary = original_run(*args, **kwargs)
        run_root = Path(kwargs["raw_root"]).parent.parent
        with EvidenceLedger(":memory:") as ledger:
            source_ingest_bridge.ingest_source_watch_manifest(summary["manifest"], ledger, root=run_root)
            bridged = next(record for record in ledger.records() if record["record_type"] == "Passage")
        passages.write_text(json.dumps({"passage_id": bridged["record_id"], "source_id": "conflicting-source",
            "text": "different passage record", "captured_at_utc": "2026-11-08T12:00:00Z"}) + "\n")
        seeded_input["bytes"] = passages.read_bytes()
        return summary

    monkeypatch.setattr(source_watch_collect, "run", collect_then_seed_conflict)
    monkeypatch.setattr(source_watch_collect, "fetch_public", lambda *a, **kw: article_response())

    result = orchestrator().run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", execute=True, pause=0, max_targets=1,
        run_local_analysis=True, typed_input_manifest_path=typed)

    folder = tmp_path / "run"
    receipt = json.loads((folder / "source_ingest_receipt.json").read_text())
    local = next(row for row in jsonl(folder / "task_receipts.jsonl") if row["task_id"] == "local_analysis")
    assert result["state"] == "failed_with_receipt"
    assert receipt["status"] == "failed" and receipt["reason"] == "passage_id_conflict"
    assert local["status"] == "failed" and local["reason"] == "passage_id_conflict"
    assert passages.read_bytes() == seeded_input["bytes"]
    assert not (folder / "merged_passages.jsonl").exists()
    assert not (folder / "analysis").exists()


def test_dry_run_does_not_bridge_or_discover_latest_capture(tmp_path, monkeypatch):
    module = orchestrator()
    spec, audit = inputs(tmp_path, budget=2)
    passages = tmp_path / "passages.jsonl"
    passages.write_text(json.dumps({"passage_id": "typed-passage", "source_id": "typed-source",
        "text": "Existing typed evidence.", "captured_at_utc": "2026-11-01T10:00:00Z"}) + "\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"passages": str(passages)}))
    latest = tmp_path / "reports/source_watch/latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps({"manifest": str(tmp_path / "old-capture/manifest.json")}))
    before = {path: path.read_bytes() for path in (passages, typed, latest)}

    def bridge_must_not_run(*args, **kwargs):
        pytest.fail("dry-run tried to bridge a discovered or stale manifest")

    monkeypatch.setattr(module, "ingest_source_watch_manifest", bridge_must_not_run)
    result = module.run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", execute=False,
        run_local_analysis=True, typed_input_manifest_path=typed)

    folder = tmp_path / "run"
    receipt = json.loads((folder / "source_ingest_receipt.json").read_text())
    assert result["state"] == "completed_bounded"
    assert receipt["status"] == "not_run"
    assert receipt["source_manifest_path"] is None
    assert all(path.read_bytes() == contents for path, contents in before.items())
    assert not (folder / "evidence_ledger.sqlite").exists()
    assert not (folder / "merged_passages.jsonl").exists()
    assert json.loads((folder / "analysis/input_manifest.json").read_text())["inputs"]["passages"]["file_name"] == "passages.jsonl"


def test_missing_manifest_return_does_not_fall_back_to_latest_and_keeps_collection_failed(tmp_path, monkeypatch):
    module = orchestrator()
    spec, audit = inputs(tmp_path, budget=2)
    posts = tmp_path / "posts.jsonl"
    posts.write_text(json.dumps({"post_id": "post-one", "platform": "telegram", "channel_id": "channel-one",
                                 "publication_time": "2026-11-01T10:15:00Z"}) + "\n")
    typed = tmp_path / "typed.json"
    typed.write_text(json.dumps({"information_posts": str(posts)}))
    latest = tmp_path / "reports/source_watch/latest.json"
    latest.parent.mkdir(parents=True)
    latest.write_text(json.dumps({"manifest": str(tmp_path / "old-capture/manifest.json")}))
    before = {path: path.read_bytes() for path in (posts, typed, latest)}
    monkeypatch.setattr(source_watch_collect, "run", lambda *a, **kw: {"snapshot": "old", "rows": []})
    monkeypatch.setattr(module, "ingest_source_watch_manifest",
                        lambda *a, **kw: pytest.fail("missing-manifest result fell back to a discovered capture"))

    result = module.run_incident(spec, tmp_path / "run", audit_path=audit,
        git_commit="abcdef1234567", execute=True, pause=0,
        run_local_analysis=True, typed_input_manifest_path=typed)

    folder = tmp_path / "run"
    bridge_receipt = json.loads((folder / "source_ingest_receipt.json").read_text())
    task_receipts = {row["task_id"]: row for row in jsonl(folder / "task_receipts.jsonl")}
    assert result["state"] == "failed_with_receipt"
    assert bridge_receipt["status"] == "not_run"
    assert bridge_receipt["source_manifest_path"] is None
    assert task_receipts["source_watch"]["status"] == "failed"
    assert task_receipts["local:information_dynamics"]["status"] == "complete"
    assert all(path.read_bytes() == contents for path, contents in before.items())
    assert not (folder / "evidence_ledger.sqlite").exists()
    assert not (folder / "merged_passages.jsonl").exists()
