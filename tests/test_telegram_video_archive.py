import asyncio
import hashlib
import json
from types import SimpleNamespace

import src.telegram_video_archive as telegram_archive
from src.telegram_video_archive import (
    build_summary,
    collect,
    download_atomic,
    file_matches,
    group_manifest_by_file_id,
    link_verified_asset,
    normalize_manifest_row,
    quarantine_orphan_part_files,
    validate_public_broadcast_entity,
)


def test_file_matches_requires_exact_size_and_optional_hash(tmp_path):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video bytes")
    digest = hashlib.sha256(b"video bytes").hexdigest()

    assert file_matches(path, 11, digest)
    assert not file_matches(path, 10, digest)
    assert not file_matches(path, 11, "0" * 64)
    assert not file_matches(tmp_path / "missing.mp4", 11)


def test_download_atomic_keeps_only_verified_complete_file(tmp_path):
    destination = tmp_path / "media.mp4"

    async def writer(path):
        path.write_bytes(b"complete-media")
        return str(path)

    result = asyncio.run(download_atomic(writer, destination, expected_size=14))
    assert result["status"] == "downloaded"
    assert destination.read_bytes() == b"complete-media"
    assert result["size_bytes"] == 14
    assert not list(tmp_path.glob("*.part"))


def test_download_atomic_rejects_truncated_media_and_preserves_destination(tmp_path):
    destination = tmp_path / "media.mp4"
    destination.write_bytes(b"previous")

    async def writer(path):
        path.write_bytes(b"short")
        return str(path)

    try:
        asyncio.run(download_atomic(writer, destination, expected_size=10))
    except ValueError as exc:
        assert "size" in str(exc).lower()
    else:
        raise AssertionError("truncated download should fail integrity check")

    assert destination.read_bytes() == b"previous"
    assert not list(tmp_path.glob("*.part"))


def test_download_atomic_reuses_existing_verified_file(tmp_path):
    destination = tmp_path / "media.mp4"
    destination.write_bytes(b"already complete")

    async def should_not_run(path):
        raise AssertionError("verified asset should not be downloaded again")

    result = asyncio.run(download_atomic(should_not_run, destination, expected_size=16))
    assert result["status"] == "existing_verified"


def test_group_manifest_by_file_id_preserves_rows_and_skips_missing_ids():
    rows = [
        {"telegram_file_id": "10", "message_id": 1},
        {"telegram_file_id": "10", "message_id": 2},
        {"message_id": 3},
    ]
    grouped = group_manifest_by_file_id(rows)
    assert list(grouped) == ["10"]
    assert grouped["10"] == rows[:2]


def test_cached_channel_must_be_exact_public_broadcast_channel():
    valid = SimpleNamespace(id=42, username="ExampleChannel", broadcast=True, megagroup=False)
    assert validate_public_broadcast_entity(valid, "@examplechannel", 42) is valid
    assert validate_public_broadcast_entity(valid, "examplechannel", -1000000000042) is valid

    invalid_entities = [
        SimpleNamespace(id=42, username="someone_else", broadcast=True, megagroup=False),
        SimpleNamespace(id=42, username=None, broadcast=True, megagroup=False),
        SimpleNamespace(id=42, username="examplechannel", broadcast=False, megagroup=True),
        SimpleNamespace(id=41, username="examplechannel", broadcast=True, megagroup=False),
    ]
    for entity in invalid_entities:
        try:
            validate_public_broadcast_entity(entity, "examplechannel", 42)
        except ValueError:
            continue
        raise AssertionError("nonmatching or nonpublic entities must be rejected")


def test_legacy_asset_status_normalizes_without_overwriting_source_error():
    row = {
        "channel": "examplechannel", "message_id": 9, "telegram_file_id": "101",
        "status": "downloaded_verified", "source_status": "message_unavailable",
        "source_error_type": "MessageIdInvalid",
    }

    normalized = normalize_manifest_row(row)

    assert normalized["status"] == "message_unavailable"
    assert normalized["source_status"] == "message_unavailable"
    assert normalized["source_error_type"] == "MessageIdInvalid"
    assert normalized["asset_status"] == "downloaded_verified"


def test_verified_asset_links_to_all_duplicate_observations_without_changing_source_status():
    rows = [
        {"telegram_file_id": "101", "status": "metadata_ok", "source_status": "metadata_ok"},
        {"telegram_file_id": "101", "status": "message_unavailable",
         "source_status": "message_unavailable", "source_error_type": "MessageIdInvalid"},
        {"telegram_file_id": "102", "status": "metadata_ok", "source_status": "metadata_ok"},
    ]

    link_verified_asset(rows, "101", {
        "status": "downloaded", "path": "/archive/tg_101.mp4", "size_bytes": 5,
        "sha256": "abc123",
    })

    assert rows[0]["asset_status"] == rows[1]["asset_status"] == "downloaded_verified"
    assert rows[0]["local_file"] == rows[1]["local_file"] == "/archive/tg_101.mp4"
    assert rows[0]["status"] == "metadata_ok"
    assert rows[1]["status"] == "message_unavailable"
    assert rows[1]["source_error_type"] == "MessageIdInvalid"
    assert "asset_status" not in rows[2]


def test_startup_quarantines_only_collector_part_files_and_avoids_overwrite(tmp_path):
    media = tmp_path / "media"
    quarantine = tmp_path / "partial_downloads"
    media.mkdir()
    (media / ".tg_101.mp4.random.part").write_bytes(b"orphan")
    (media / "unrelated.part").write_bytes(b"leave me")
    quarantine.mkdir()
    (quarantine / ".tg_101.mp4.random.part").write_bytes(b"prior")

    moved = quarantine_orphan_part_files(media, quarantine)

    assert moved == 1
    assert not (media / ".tg_101.mp4.random.part").exists()
    assert (media / "unrelated.part").read_bytes() == b"leave me"
    quarantined = sorted(p.read_bytes() for p in quarantine.iterdir())
    assert quarantined == [b"orphan", b"prior"]


def test_summary_reports_source_posts_and_unique_verified_assets_separately(tmp_path):
    media = tmp_path / "media" / "tg_101.mp4"
    media.parent.mkdir()
    media.write_bytes(b"video")
    rows = [
        {"telegram_file_id": "101", "local_file": str(media), "file_size_bytes": 5,
         "sha256": hashlib.sha256(b"video").hexdigest(), "status": "metadata_ok",
         "asset_status": "downloaded_verified"},
        {"telegram_file_id": "101", "status": "message_unavailable",
         "asset_status": "downloaded_verified"},
    ]

    summary = build_summary(rows, tmp_path / "manifest.jsonl", media.parent)

    assert summary["source_posts"] == 2
    assert summary["unique_telegram_file_ids"] == 1
    assert summary["verified_unique_media"] == 1
    assert summary["source_status_counts_by_post"] == {
        "message_unavailable": 1, "metadata_ok": 1,
    }


def test_collect_rejects_unexpected_entity_before_fetching_messages(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps({
        "channel": "examplechannel", "message_id": 7, "status": "pending",
    }) + "\n", encoding="utf-8")

    class FakeClient:
        message_fetches = 0

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_entity(self, reference):
            return SimpleNamespace(id=77, username="differentchannel", broadcast=True,
                                   megagroup=False)

        async def get_input_entity(self, entity):
            return entity

        async def get_messages(self, entity, ids):
            self.message_fetches += 1
            raise AssertionError("messages must not be fetched for a mismatched entity")

        async def disconnect(self):
            pass

    client = FakeClient()
    result = asyncio.run(collect(
        manifest, tmp_path / "media", None,
        client_factory=lambda _: client, env_loader=lambda: {},
    ))

    row = json.loads(manifest.read_text(encoding="utf-8"))
    receipt = json.loads((tmp_path / "collection_receipts" /
                          f"{result['run_id']}.json").read_text(encoding="utf-8"))
    assert client.message_fetches == 0
    assert row["source_status"] == "channel_or_metadata_error"
    assert receipt["posts"][0]["source_error_type"] == "ValueError"
    assert receipt["posts"][0]["attempts"][-1] == {
        "stage": "channel_resolution", "status": "error", "error_type": "ValueError",
    }
    assert "differentchannel" not in json.dumps(receipt)


def test_collect_retries_unresolved_duplicate_source_and_keeps_verified_asset(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    binary = media_dir / "tg_101.mp4"
    binary.write_bytes(b"video")
    orphan_part = media_dir / ".tg_999.mp4.crashleft.part"
    orphan_part.write_bytes(b"partial")
    digest = hashlib.sha256(b"video").hexdigest()
    rows = [
        {"channel": "examplechannel", "message_id": 1, "telegram_file_id": "101",
         "status": "downloaded_verified", "local_file": str(binary), "file_size_bytes": 5,
         "sha256": digest},
        {"channel": "examplechannel", "message_id": 2, "telegram_file_id": "101",
         "status": "channel_or_metadata_error", "error_type": "MessageIdInvalid"},
    ]
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    snapshot = tmp_path / "channels.jsonl"
    snapshot.write_text(json.dumps({"username": "examplechannel", "chat_id": 42}) + "\n",
                        encoding="utf-8")

    class FakeClient:
        requested_ids = []

        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_entity(self, reference):
            return SimpleNamespace(id=42, username="ExampleChannel", broadcast=True,
                                   megagroup=False)

        async def get_input_entity(self, entity):
            return entity

        async def get_messages(self, entity, ids):
            self.requested_ids.extend(ids)
            return []

        async def download_media(self, message, file):
            raise AssertionError("a verified duplicate asset must not be downloaded again")

        async def disconnect(self):
            pass

    client = FakeClient()
    result = asyncio.run(collect(
        manifest, media_dir, snapshot,
        client_factory=lambda _: client, env_loader=lambda: {},
    ))

    final_rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    receipt = json.loads((tmp_path / "collection_receipts" /
                          f"{result['run_id']}.json").read_text(encoding="utf-8"))
    assert client.requested_ids == [2]
    assert final_rows[1]["source_status"] == "message_unavailable"
    assert final_rows[1]["source_error_type"] == "MessageUnavailable"
    assert final_rows[1]["asset_status"] == "downloaded_verified"
    assert final_rows[1]["local_file"] == str(binary)
    assert final_rows[0]["source_status"] == "metadata_ok"
    assert result["source_posts"] == 2
    assert result["unique_telegram_file_ids"] == 1
    assert receipt["run_id"] == result["run_id"]
    assert receipt["collector"]["version"] == "1.1.0"
    assert receipt["parameters"]["concurrency"] == 3
    assert receipt["started_at_utc"] and receipt["completed_at_utc"]
    assert receipt["channel_snapshot"]["source"] == "local_jsonl_snapshot"
    assert receipt["channel_snapshot"]["sha256"] == hashlib.sha256(snapshot.read_bytes()).hexdigest()
    assert receipt["orphan_part_files_quarantined"] == 1
    assert (tmp_path / "partial_downloads" / orphan_part.name).read_bytes() == b"partial"
    assert receipt["posts"][1]["attempts"][-1]["status"] == "unavailable"


def test_receipt_persists_exception_type_without_raw_message(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps({
        "channel": "examplechannel", "message_id": 7, "status": "pending",
    }) + "\n", encoding="utf-8")

    class FakeClient:
        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_entity(self, reference):
            raise RuntimeError("session cookie secret-value-123")

        async def disconnect(self):
            pass

    result = asyncio.run(collect(
        manifest, tmp_path / "media", None,
        client_factory=lambda _: FakeClient(), env_loader=lambda: {},
    ))
    receipt_text = (tmp_path / "collection_receipts" /
                    f"{result['run_id']}.json").read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)

    assert receipt["posts"][0]["source_error_type"] == "RuntimeError"
    assert "secret-value-123" not in receipt_text


def test_asset_failure_does_not_replace_duplicate_source_failure(tmp_path, monkeypatch):
    manifest = tmp_path / "manifest.jsonl"
    rows = [
        {"channel": "examplechannel", "message_id": 1, "telegram_file_id": "101",
         "status": "metadata_ok"},
        {"channel": "examplechannel", "message_id": 2, "telegram_file_id": "101",
         "status": "message_unavailable", "error_type": "MessageIdInvalid"},
    ]
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    document = SimpleNamespace(id=101, size=5, mime_type="video/mp4")
    monkeypatch.setattr(telegram_archive, "_video_document", lambda message: (document, None))

    class FakeClient:
        async def connect(self):
            pass

        async def is_user_authorized(self):
            return True

        async def get_entity(self, reference):
            return SimpleNamespace(id=42, username="examplechannel", broadcast=True,
                                   megagroup=False)

        async def get_input_entity(self, entity):
            return entity

        async def get_messages(self, entity, ids):
            return [SimpleNamespace(id=1)]

        async def download_media(self, message, file):
            raise RuntimeError("session token should never be recorded")

        async def disconnect(self):
            pass

    result = asyncio.run(collect(
        manifest, tmp_path / "media", None,
        client_factory=lambda _: FakeClient(), env_loader=lambda: {},
    ))
    final_rows = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
    receipt_text = (tmp_path / "collection_receipts" /
                    f"{result['run_id']}.json").read_text(encoding="utf-8")
    receipt = json.loads(receipt_text)

    assert final_rows[0]["source_status"] == "metadata_ok"
    assert final_rows[0]["asset_status"] == "download_error"
    assert final_rows[1]["source_status"] == "message_unavailable"
    assert final_rows[1]["source_error_type"] == "MessageUnavailable"
    assert final_rows[1]["asset_status"] == "download_error"
    assert receipt["posts"][1]["asset_error_type"] == "RuntimeError"
    assert "session token" not in receipt_text
