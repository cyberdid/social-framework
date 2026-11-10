import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tiktok_public_collect import normalize_comment, walk_pages


def test_pagination_stops_on_platform_error_without_retrying():
    cursors = []

    def fetch(cursor):
        cursors.append(cursor)
        return {"status_code": 5, "status_msg": "restricted"}

    rows, status = walk_pages(fetch)
    assert rows == []
    assert cursors == [0]
    assert status["stop_reason"] == "platform_error"
    assert status["complete_pagination"] is False


def test_pagination_tracks_duplicates_and_requires_explicit_exhaustion():
    pages = {
        0: {"status_code": 0, "comments": [{"cid": "1"}], "cursor": 20, "has_more": 1},
        20: {"status_code": 0, "comments": [{"cid": "1"}, {"cid": "2"}], "has_more": 0},
    }
    rows, status = walk_pages(pages.__getitem__)
    assert len(rows) == 3
    assert status["distinct_ids"] == 2
    assert status["complete_pagination"] is True
    assert status["stop_reason"] == "has_more_false"


def test_missing_cursor_cannot_be_reported_as_complete():
    _, status = walk_pages(lambda _: {"status_code": 0, "comments": [], "has_more": 1})
    assert status["complete_pagination"] is False
    assert status["stop_reason"] == "cursor_missing_or_repeated"


def test_normalization_excludes_author_profile_and_preserves_parent():
    source = {"cid": "r1", "text": "public text", "create_time": 1793830654,
              "digg_count": 7, "reply_comment_total": 0, "author_pin": False,
              "user": {"nickname": "DO NOT EXPORT", "unique_id": "private_context"}}
    row = normalize_comment(source, "v1", "https://example.test/video/v1", "2026-11-05T00:00:00Z", "p1")
    assert row["parent_id"] == "p1"
    assert row["created_at_utc"] == "2026-11-04T22:17:34+00:00"
    assert row["likes"] == 7
    assert "user" not in row and "author" not in row
    assert "DO NOT EXPORT" not in str(row)
