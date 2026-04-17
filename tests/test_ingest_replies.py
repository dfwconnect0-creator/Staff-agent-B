import json
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

import src.memory.episodic as episodic

VALID_PREDICTION = {
    "schema_version": 1,
    "stuck_item": "Topic finder GitHub Actions step",
    "smallest_action": "Open last workflow run in GitHub",
    "confidence": "medium",
    "flags_raised": [],
    "questions_asked": ["Did you check the workflow run?"],
}

CAIRO_TZ = timezone(timedelta(hours=3))


@pytest.fixture(autouse=True)
def tmp_memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(episodic, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    return tmp_path


def test_parse_cairo_date_maps_utc_to_cairo():
    from src.ingest_replies import parse_cairo_date
    # 2026-04-17 23:30 UTC = 2026-04-18 02:30 Cairo
    result = parse_cairo_date("2026-04-18 02:30")
    assert result == date(2026, 4, 18)


def test_get_updates_filters_other_chat_ids(monkeypatch):
    import httpx
    from src.telegram_client import get_updates
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")

    mock_response = MagicMock()
    mock_response.json.return_value = {
        "ok": True,
        "result": [
            {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 12345}, "text": "hi", "date": 1713308400}},
            {"update_id": 2, "message": {"message_id": 2, "chat": {"id": 99999}, "text": "other", "date": 1713308401}},
            {"update_id": 3, "message": {"message_id": 3, "chat": {"id": 12345}, "text": "bye", "date": 1713308402}},
        ]
    }
    mock_response.raise_for_status = MagicMock()

    with patch("src.telegram_client.httpx.get", return_value=mock_response):
        updates = get_updates(since_update_id=0)

    assert len(updates) == 2
    assert all(u["chat_id"] == 12345 for u in updates)


def test_appends_reply_to_correct_day(tmp_path):
    from src.ingest_replies import main as ingest_main

    d = date(2026, 4, 17)
    episodic.write_day(d, "briefing", VALID_PREDICTION)

    # Unix timestamp for 2026-04-17 09:12 Cairo = 2026-04-17 06:12 UTC
    ts = int(datetime(2026, 4, 17, 6, 12, tzinfo=timezone.utc).timestamp())

    mock_updates = [
        {"update_id": 101, "chat_id": 12345, "timestamp_cairo": "2026-04-17 09:12", "text": "test reply"},
    ]

    with patch("src.ingest_replies.telegram.get_updates", return_value=mock_updates):
        with patch("src.ingest_replies.read_last_update_id", return_value=0):
            with patch("src.ingest_replies.write_last_update_id") as mock_write:
                ingest_main()

    content = (tmp_path / "2026-04-17.md").read_text()
    assert '- **2026-04-17 09:12 Cairo:** "test reply"' in content


def test_orphan_replies_go_to_orphans_file(tmp_path):
    from src.ingest_replies import main as ingest_main

    mock_updates = [
        {"update_id": 101, "chat_id": 12345, "timestamp_cairo": "2026-04-17 09:12", "text": "orphaned reply"},
    ]

    with patch("src.ingest_replies.telegram.get_updates", return_value=mock_updates):
        with patch("src.ingest_replies.read_last_update_id", return_value=0):
            with patch("src.ingest_replies.write_last_update_id"):
                ingest_main()

    orphans = (tmp_path / ".orphans.md").read_text()
    assert "orphaned reply" in orphans


def test_updates_last_update_id(tmp_path):
    from src.ingest_replies import main as ingest_main

    d = date(2026, 4, 17)
    episodic.write_day(d, "briefing", VALID_PREDICTION)

    mock_updates = [
        {"update_id": 100, "chat_id": 12345, "timestamp_cairo": "2026-04-17 09:00", "text": "msg1"},
        {"update_id": 200, "chat_id": 12345, "timestamp_cairo": "2026-04-17 09:05", "text": "msg2"},
        {"update_id": 250, "chat_id": 12345, "timestamp_cairo": "2026-04-17 09:10", "text": "msg3"},
    ]

    written_id = []

    with patch("src.ingest_replies.telegram.get_updates", return_value=mock_updates):
        with patch("src.ingest_replies.read_last_update_id", return_value=0):
            with patch("src.ingest_replies.write_last_update_id", side_effect=lambda x: written_id.append(x)):
                ingest_main()

    assert written_id[-1] == 250


def test_no_updates_no_changes(tmp_path):
    from src.ingest_replies import main as ingest_main

    last_id_file = tmp_path / ".last_update_id"
    last_id_file.write_text("100")

    written_id = []

    with patch("src.ingest_replies.telegram.get_updates", return_value=[]):
        with patch("src.ingest_replies.read_last_update_id", return_value=100):
            with patch("src.ingest_replies.write_last_update_id", side_effect=lambda x: written_id.append(x)):
                ingest_main()

    assert written_id == []
