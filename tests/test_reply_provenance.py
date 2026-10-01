"""Regression: an ordinary Telegram message must not act as briefing evidence.

Reproduces the live provenance bug found on 2026-10-01. Two messages arrived carrying the
same text, which would both verify `live_delivery`:

    evt_000003  telegram_message_id=158  reply_to_message_id=null
                briefing_id was wrongly set to briefing_20261001_001
    evt_000004  telegram_message_id=159  reply_to_message_id=157
                the genuine direct reply to briefing_20261001_001

Only the direct reply may drive a state transition. The ordinary message is still stored
as an event, and is still consumed so it cannot be reconsidered later.
"""

import pytest

import src.events as events_log
import src.memory.episodic as episodic
import src.state as state_mod
import src.state_update as state_update_mod
import src.ingest_replies as ingest

BRIEFING_ID = "briefing_20261001_001"
DELIVERED_MESSAGE_ID = 157

# "Live delivery worked" matches the live_delivery alias 'live delivery' plus the
# positive signal 'worked', so this text WOULD verify the checkpoint if it were trusted.
ORDINARY_TEXT = "Live delivery worked. I received this briefing successfully in Telegram."


@pytest.fixture
def live(tmp_path, monkeypatch):
    monkeypatch.setattr(episodic, "MEMORY_DIR", tmp_path / "memory")
    (tmp_path / "memory").mkdir()
    state_file = tmp_path / "current_state.md"
    state_mod.write_state(
        {
            **state_mod.empty_state(),
            "target_output": "Prove one real bidirectional cycle.",
            "next_action": "Verify live_delivery.",
            "checkpoints": {"live_delivery": "not_verified"},
            "aliases": {"live_delivery": ["live delivery", "telegram delivery"]},
        },
        state_file,
    )
    monkeypatch.setattr(state_mod, "DEFAULT_STATE_PATH", state_file)

    events_log.append_event("briefing_sent", briefing_id=BRIEFING_ID, state_version="v1")
    events_log.append_event(
        "briefing_delivered",
        briefing_id=BRIEFING_ID,
        telegram_message_id=DELIVERED_MESSAGE_ID,
        delivered=True,
    )
    return state_file


def send(update_id, message_id, text, reply_to_message_id):
    return ingest.ingest_update(
        {
            "update_id": update_id,
            "chat_id": 12345,
            "text": text,
            "message_id": message_id,
            "reply_to_message_id": reply_to_message_id,
            "timestamp_cairo": "2026-10-01 08:00",
        }
    )


# --- A. ordinary message: reply_to_message_id is null ---------------------------


def test_ordinary_message_is_not_linked_to_a_briefing(live):
    event = send(1, 158, ORDINARY_TEXT, reply_to_message_id=None)

    assert event["event_type"] == "user_reply"
    assert event["reply_to_message_id"] is None
    assert event["briefing_id"] is None


def test_ordinary_message_is_still_stored(live):
    send(1, 158, ORDINARY_TEXT, reply_to_message_id=None)

    stored = [e for e in events_log.read_events() if e["event_type"] == "user_reply"]
    assert len(stored) == 1
    assert stored[0]["telegram_message_id"] == 158
    assert stored[0]["text"] == ORDINARY_TEXT


def test_ordinary_message_cannot_mutate_operational_state(live):
    send(1, 158, ORDINARY_TEXT, reply_to_message_id=None)
    before = live.read_text(encoding="utf-8")

    report = state_update_mod.update_state_from_events()

    assert report["transitions"] == []
    assert report["evidence_event_ids"] == []
    assert report["processed_event_ids"] != []  # consumed, so it is never reconsidered
    assert state_mod.load_state()["checkpoints"]["live_delivery"] == "not_verified"
    assert state_mod.load_state()["transitions"] == []
    # next_action and updated_at may move, but no checkpoint or transition may appear
    assert "live_delivery = verified" not in live.read_text(encoding="utf-8")
    assert before.count("live_delivery = not_verified") == live.read_text(
        encoding="utf-8").count("live_delivery = not_verified")


def test_ordinary_message_does_not_become_last_evidence(live):
    send(1, 158, ORDINARY_TEXT, reply_to_message_id=None)
    state_update_mod.update_state_from_events()

    assert state_mod.load_state()["last_evidence"] == "none"


# --- B. direct reply: reply_to_message_id matches the delivered briefing --------


def test_direct_reply_resolves_to_the_briefing(live):
    event = send(2, 159, ORDINARY_TEXT, reply_to_message_id=DELIVERED_MESSAGE_ID)

    assert event["briefing_id"] == BRIEFING_ID
    assert event["reply_to_message_id"] == DELIVERED_MESSAGE_ID
    assert events_log.has_reply_provenance(event) is True


def test_direct_reply_is_eligible_to_update_state(live):
    send(2, 159, ORDINARY_TEXT, reply_to_message_id=DELIVERED_MESSAGE_ID)
    report = state_update_mod.update_state_from_events()

    assert report["evidence_event_ids"] == ["evt_000003"]
    assert [(t["checkpoint"], t["to"]) for t in report["transitions"]] == [
        ("live_delivery", "verified")]
    assert report["transitions"][0]["evidence_event_id"] == "evt_000003"
    assert state_mod.load_state()["checkpoints"]["live_delivery"] == "verified"


# --- C. both messages present: evidence must trace to the direct reply only ------


def test_with_both_messages_only_the_direct_reply_provides_evidence(live):
    ordinary = send(1, 158, ORDINARY_TEXT, reply_to_message_id=None)
    direct = send(2, 159, ORDINARY_TEXT, reply_to_message_id=DELIVERED_MESSAGE_ID)

    report = state_update_mod.update_state_from_events()

    assert ordinary["event_id"] == "evt_000003"
    assert direct["event_id"] == "evt_000004"
    # both consumed, only the direct reply counted as evidence
    assert report["processed_event_ids"] == ["evt_000003", "evt_000004"]
    assert report["evidence_event_ids"] == ["evt_000004"]
    assert [t["evidence_event_id"] for t in report["transitions"]] == ["evt_000004"]
    state = state_mod.load_state()
    assert state["checkpoints"]["live_delivery"] == "verified"
    assert "evt_000004" in state["last_evidence"]
    assert "evt_000003" not in state["last_evidence"]
    # the recorded transition cites evt_000004, never evt_000003
    assert "evt_000004" in state["transitions"][-1]
    assert "evt_000003" not in state["transitions"][-1]


def test_pre_existing_malformed_event_is_not_trusted(live):
    """A historical event already carries a briefing_id but no reply provenance.

    Mirrors evt_000003 on the live log: briefing_id set by the old fallback, while
    reply_to_message_id is null. State processing must reject it on the merits.
    """
    events_log.append_event(
        "user_reply",
        source="telegram",
        processed=False,
        text=ORDINARY_TEXT,
        briefing_id=BRIEFING_ID,          # wrong, written before the fix
        telegram_update_id=1,
        telegram_message_id=158,
        reply_to_message_id=None,
    )

    assert events_log.has_reply_provenance(events_log.unprocessed_replies()[0]) is False

    report = state_update_mod.update_state_from_events()

    assert report["evidence_event_ids"] == []
    assert report["transitions"] == []
    assert state_mod.load_state()["checkpoints"]["live_delivery"] == "not_verified"