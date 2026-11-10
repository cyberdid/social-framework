"""Focused tests for bridging immutable source captures into EvidenceLedger."""

import gzip
import hashlib
import json
from pathlib import Path

from src.analysis.source_ingest_bridge import (
    ingest_archive_index,
    ingest_source_watch_manifest,
)
from src.evidence_ledger import EvidenceLedger


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_watch_capture(root: Path, snapshot: str, *, url: str, final_url: str,
                        captured_at: str, body: bytes, text: str,
                        mode: str = "article_text") -> Path:
    folder = root / "watch" / snapshot
    folder.mkdir(parents=True)
    (folder / "capture.html").write_bytes(body)
    manifest = {
        "schema_version": 1,
        "snapshot": snapshot,
        "rows": [{
            "url": url,
            "final_url": final_url,
            "requested_at": captured_at,
            "status": 200,
            "content_type": "text/html; charset=utf-8",
            "body_file": "capture.html",
            "bytes": len(body),
            "sha256": sha(body),
            "truncated": False,
            "extracted": {
                "mode": mode,
                "extractor_version": 2,
                "filtered_text": text,
            },
        }],
    }
    path = folder / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


def make_ledger(tmp_path: Path) -> EvidenceLedger:
    ledger = EvidenceLedger(tmp_path / "ledger.sqlite")
    return ledger


def test_watch_ingest_keeps_urls_hash_timestamp_and_locator_without_echoing_text(tmp_path):
    root = tmp_path
    body = b"<html><article>captured bytes</article></html>"
    passage_text = "Exact collector supplied extraction, not a new claim."
    source_url = "https://publisher.example/requested"
    capture_url = "https://publisher.example/final"
    manifest = write_watch_capture(
        root, "snapshot-a", url=source_url, final_url=capture_url,
        captured_at="2026-11-07T12:30:00+02:00", body=body, text=passage_text,
    )
    ledger = make_ledger(tmp_path)
    try:
        report = ingest_source_watch_manifest(manifest, ledger, root=root)
        records = ledger.records()
    finally:
        ledger.close()

    source, artifact, passage = records
    assert source["uri"] == source_url
    assert artifact["sha256"] == sha(body)
    assert artifact["captured_at"] == "2026-11-07T10:30:00+00:00"
    assert artifact["path"] == "watch/snapshot-a/capture.html"
    assert passage["text"] == passage_text
    assert passage["locator"] == {
        "kind": "source_watch_filtered_article_text",
        "manifest_path": "watch/snapshot-a/manifest.json",
        "manifest_sha256": sha(manifest.read_bytes()),
        "row_index": 0,
        "body_path": "watch/snapshot-a/capture.html",
        "body_sha256": sha(body),
        "source_url": source_url,
        "capture_url": capture_url,
        "capture_timestamp": "2026-11-07T12:30:00+02:00",
        "extractor_version": 2,
        "extraction_mode": "article_text",
        "text_sha256": sha(passage_text.encode("utf-8")),
    }
    assert report["items"] == [{"index": 0, "status": "ingested", "records_added": 3}]
    public_report = json.dumps(report, ensure_ascii=False)
    assert passage_text not in public_report
    assert source_url not in public_report
    assert capture_url not in public_report
    assert str(manifest) not in public_report


def test_watch_ingest_rejects_hash_or_declared_byte_count_mismatch(tmp_path):
    body = b"captured bytes"
    for mismatch in ("hash", "bytes"):
        root = tmp_path / mismatch
        manifest = write_watch_capture(
            root, "snapshot", url="https://publisher.example/a",
            final_url="https://publisher.example/a", captured_at="2026-11-07T10:00:00Z",
            body=body, text="A passage.",
        )
        data = json.loads(manifest.read_text())
        if mismatch == "hash":
            data["rows"][0]["sha256"] = "0" * 64
        else:
            data["rows"][0]["bytes"] += 1
        manifest.write_text(json.dumps(data), encoding="utf-8")
        ledger = make_ledger(root)
        try:
            report = ingest_source_watch_manifest(manifest, ledger, root=root)
            assert report["items"][0]["status"] == "integrity_mismatch"
            assert ledger.records() == []
        finally:
            ledger.close()


def test_repeated_captures_are_idempotent_per_receipt_and_distinct_over_time(tmp_path):
    root = tmp_path
    url = "https://publisher.example/repeated"
    body = b"same exact body"
    first = write_watch_capture(
        root, "snapshot-one", url=url, final_url=url,
        captured_at="2026-11-07T10:00:00Z", body=body, text="Same extraction.",
    )
    second = write_watch_capture(
        root, "snapshot-two", url=url, final_url=url,
        captured_at="2026-11-07T11:00:00Z", body=body, text="Same extraction.",
    )
    ledger = make_ledger(tmp_path)
    try:
        first_report = ingest_source_watch_manifest(first, ledger, root=root)
        replay_report = ingest_source_watch_manifest(first, ledger, root=root)
        second_report = ingest_source_watch_manifest(second, ledger, root=root)
        records = ledger.records()
    finally:
        ledger.close()

    assert first_report["items"][0]["status"] == "ingested"
    assert replay_report["items"][0]["status"] == "already_present"
    assert replay_report["items"][0]["records_added"] == 0
    assert second_report["items"][0]["status"] == "ingested"
    sources = [record for record in records if record["record_type"] == "Source"]
    artifacts = [record for record in records if record["record_type"] == "Artifact"]
    passages = [record for record in records if record["record_type"] == "Passage"]
    assert len(sources) == 1
    assert len(artifacts) == 2
    assert len(passages) == 2
    assert len({artifact["record_id"] for artifact in artifacts}) == 2
    assert len({artifact["captured_at"] for artifact in artifacts}) == 2


def test_missing_url_or_body_has_explicit_state_and_adds_no_records(tmp_path):
    root = tmp_path
    folder = root / "watch" / "snapshot"
    folder.mkdir(parents=True)
    (folder / "orphan.html").write_bytes(b"orphan")
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps({"rows": [
        {"url": "https://publisher.example/no-body", "final_url": "https://publisher.example/no-body"},
        {"final_url": "https://publisher.example/no-url", "body_file": "orphan.html"},
    ]}), encoding="utf-8")
    ledger = make_ledger(tmp_path)
    try:
        report = ingest_source_watch_manifest(manifest, ledger, root=root)
        assert [item["status"] for item in report["items"]] == ["missing_body", "missing_url"]
        assert ledger.records() == []
    finally:
        ledger.close()


def test_archive_gzip_hashes_uncompressed_bytes_and_reports_no_passage(tmp_path):
    root = tmp_path
    archive = root / "raw" / "web"
    archive.mkdir(parents=True)
    body = b"<html><body>Archived bytes only.</body></html>"
    (archive / "capture.html.gz").write_bytes(gzip.compress(body))
    index = archive / "index.jsonl"
    index.write_text(json.dumps({
        "url": "https://publisher.example/archive",
        "final_url": "https://publisher.example/archive",
        "file": "capture.html.gz",
        "sha256": sha(body),
        "bytes": len(body),
        "fetched_at": "2026-11-07T12:00:00+02:00",
        "status": 200,
    }) + "\n", encoding="utf-8")
    ledger = make_ledger(tmp_path)
    try:
        report = ingest_archive_index(index, ledger, root=root)
        records = ledger.records()
    finally:
        ledger.close()

    assert report["items"][0]["status"] == "unsupported_no_passage"
    assert len(records) == 2
    artifact = next(record for record in records if record["record_type"] == "Artifact")
    assert artifact["sha256"] == sha(body)
    assert artifact["path"] == "raw/web/capture.html.gz"
    assert artifact["captured_at"] == "2026-11-07T10:00:00+00:00"
    assert "decompressed" in " ".join(artifact["limitations"])


def test_archive_redirect_without_passage_is_explicitly_unrepresentable(tmp_path):
    root = tmp_path
    archive = root / "raw" / "web"
    archive.mkdir(parents=True)
    body = b"<html>body</html>"
    (archive / "capture.html.gz").write_bytes(gzip.compress(body))
    index = archive / "index.jsonl"
    index.write_text(json.dumps({
        "url": "https://publisher.example/requested",
        "final_url": "https://publisher.example/canonical",
        "file": "capture.html.gz",
        "sha256": sha(body),
        "bytes": len(body),
        "fetched_at": "2026-11-07T12:00:00Z",
        "status": 200,
    }) + "\n", encoding="utf-8")
    ledger = make_ledger(tmp_path)
    try:
        report = ingest_archive_index(index, ledger, root=root)
        assert report["items"][0]["status"] == "unsupported_capture_url"
        assert ledger.records() == []
    finally:
        ledger.close()


def test_watch_redirect_without_supported_passage_is_not_silently_collapsed(tmp_path):
    root = tmp_path
    body = b"<html><section>listing response</section></html>"
    manifest = write_watch_capture(
        root, "snapshot", url="https://publisher.example/requested",
        final_url="https://publisher.example/final", captured_at="2026-11-07T10:00:00Z",
        body=body, text="listing links are not a passage", mode="news_links",
    )
    ledger = make_ledger(tmp_path)
    try:
        report = ingest_source_watch_manifest(manifest, ledger, root=root)
        assert report["items"][0]["status"] == "unsupported_capture_url"
        assert ledger.records() == []
    finally:
        ledger.close()
