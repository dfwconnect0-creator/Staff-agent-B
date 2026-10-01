"""Append-only event log: shape, durability, provenance, briefing ids."""
import json
from datetime import date

import pytest

import src.memory.episodic as episodic
import src.events as events_log


@pytest.fixture(autouse=True)
def tmp_memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(episodic, "MEMORY_DIR", tmp_path)
    return tmp_path


def test_log_is_created_on_first_append(tmp_path):
    assert not (tmp_path / "events.jsonl").exists()
    events_log.append_event("state_update", consumes=[], transitions=[])
    assert (tmp_path / "events.jsonl").exists()


def test_appended_event_has_the_required_fields(tmp_path):
    e = events_log.append_event(
        "user_reply",
        source="telegram",
        processed=False,
        text="retrieval worked",
        briefing_id="briefing_20261001_001",
    )
    for key in ("timestamp", "event_id", "source", "event_type", "project_id", "briefing_id", "text", "processed"):
        assert key in e, f"missing {key}"
    assert e["event_type"] == "user_reply"
    assert e["processed"] is False
    assert e["project_id"] == "staff-agent"


def test_event_ids_increment_and_are_unique(tmp_path):
    ids = [events_log.append_event("state_update")["event_id"] for _ in range(3)]
    assert ids == ["evt_000001", "evt_000002", "evt_000003"]


def test_log_is_append_only_one_json_object_per_line(tmp_path):
    events_log.append_event("state_update", consumes=["a"])
    events_log.append_event("state_update", consumes=["b"])
    lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    for line in lines:
        assert isinstance(json.loads(line), dict)


def test_malformed_line_does_not_break_reading(tmp_path):
    events_log.append_event("state_update", consumes=[])
    with (tmp_path / "events.jsonl").open("a") as fh:
        fh.write("{truncated json\n")
    events_log.append_event("state_update", consumes=[])
    events = events_log.read_events()
    assert len(events) == 2


def test_unknown_event_type_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        events_log.append_event("not_a_real_type")


def test_replies_start_unprocessed_and_become_processed_after_marking(tmp_path):
    reply = events_log.append_event("user_reply", processed=False, text="hi")
    assert events_log.unprocessed_replies() == [reply]
    events_log.mark_processed(consumes=[reply["event_id"]], transitions=[], state_version="v1")
    assert events_log.unprocessed_replies() == []
    assert [e["processed"] for e in events_log.read_events() if e["event_type"] == "user_reply"] == [True]


def test_marking_processed_is_idempotent(tmp_path):
    reply = events_log.append_event("user_reply", processed=False, text="hi")
    events_log.mark_processed(consumes=[reply["event_id"]], transitions=[], state_version="v1")
    events_log.mark_processed(consumes=[reply["event_id"]], transitions=[], state_version="v1")
    consumed = [
        e for e in events_log.read_events() if e["event_type"] == "state_update"
    ]
    assert len(consumed) == 2
    assert events_log.unprocessed_replies() == []


def test_next_briefing_id_starts_at_001_and_increments_per_day(tmp_path):
    day = date(2026, 10, 1)
    assert events_log.next_briefing_id(day) == "briefing_20261001_001"
    events_log.append_event("briefing_sent", briefing_id=events_log.next_briefing_id(day))
    assert events_log.next_briefing_id(day) == "briefing_20261001_002"
    events_log.append_event("briefing_sent", briefing_id=events_log.next_briefing_id(day))
    assert events_log.next_briefing_id(day) == "briefing_20261001_003"


def test_next_briefing_id_resets_on_a_new_day(tmp_path):
    events_log.append_event("briefing_sent", briefing_id="briefing_20261001_001")
    assert events_log.next_briefing_id(date(2026, 10, 2)) == "briefing_20261002_001"


def test_reply_to_message_id_links_the_reply_to_its_briefing(tmp_path):
    events_log.append_event("briefing_sent", briefing_id="briefing_20261001_001")
    events_log.append_event("briefing_delivered", briefing_id="briefing_20261001_001",
                            telegram_message_id=5001)
    assert events_log.resolve_reply_briefing(5001) == "briefing_20261001_001"


def test_reply_to_unknown_message_falls_back_to_latest_briefing(tmp_path):
    events_log.append_event("briefing_sent", briefing_id="briefing_20261001_001")
    events_log.append_event("briefing_delivered", briefing_id="briefing_20261001_001",
                            telegram_message_id=5001)
    events_log.append_event("briefing_sent", briefing_id="briefing_20261001_002")
    events_log.append_event("briefing_delivered", briefing_id="briefing_20261001_002",
                            telegram_message_id=5002)
    assert events_log.resolve_reply_briefing(None) == "briefing_20261001_002"
    assert events_log.resolve_reply_briefing(5001) == "briefing_20261001_001"


def test_last_delivered_briefing_ignores_briefings_that_were_never_delivered(tmp_path):
    day = date(2026, 10, 1)
    events_log.append_event("briefing_sent", briefing_id="briefing_20261001_001", state_version="v1")
    events_log.append_event("briefing_delivered", briefing_id="briefing_20261001_001", telegram_message_id=1)
    events_log.append_event("briefing_sent", briefing_id="briefing_20261001_002", state_version="v2")
    assert events_log.last_delivered_briefing(day)["briefing_id"] == "briefing_20261001_001"


def test_survives_a_fresh_process_view_of_the_same_directory(tmp_path):
    events_log.append_event("user_reply", processed=False, text="durable")
    reloaded = events_log.read_events()
    assert reloaded[0]["text"] == "durable"
