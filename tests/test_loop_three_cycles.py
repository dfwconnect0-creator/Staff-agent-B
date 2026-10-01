"""End-to-end episodic loop: briefing -> reply -> state change -> changed briefing.

Each test drives the real `src.briefing.main()` and the real
`src.ingest_replies.ingest_update()`. Only the LLM and the Telegram HTTP API are
substituted, at the seams the existing suite already uses.

These are the acceptance criteria for the three-cycle experiment, asserted
mechanically rather than eyeballed.
"""

import json
from unittest.mock import patch

import freezegun
import pytest

import src.memory.episodic as episodic
import src.events as events_log
import src.state as state_mod
import src.state_update as state_update_mod
import src.briefing as briefing
import src.ingest_replies as ingest

from tests.loop_support import (
    CHECKPOINTS,
    TEST_REPLIES,
    FakeTelegram,
    ScriptedProvider,
    checkpoints_from_prompt,
    reset_state_to_seed,
)


@pytest.fixture
def loop(tmp_path, monkeypatch):
    monkeypatch.setattr(episodic, "MEMORY_DIR", tmp_path / "memory")
    (tmp_path / "memory").mkdir()
    state_file = reset_state_to_seed(tmp_path / "current_state.md")
    monkeypatch.setattr(state_mod, "DEFAULT_STATE_PATH", state_file)

    provider = ScriptedProvider()
    telegram = FakeTelegram()
    harness = LoopHarness(provider, telegram)
    return harness


class LoopHarness:
    def __init__(self, provider, telegram):
        self.provider = provider
        self.telegram = telegram
        self.cycle = 0

    def briefing(self, clock=None):
        self.cycle += 1
        clock = clock or f"2026-10-01 0{self.cycle}:00:00"
        before = len(self.telegram.sent)
        with freezegun.freeze_time(clock):
            with patch.object(briefing, "get_provider", return_value=self.provider):
                with patch.object(briefing, "send_telegram_message", side_effect=self.telegram.send):
                    rc = briefing.main()
        return rc, (self.telegram.sent[before] if len(self.telegram.sent) > before else None)

    def reply(self, cycle, delivered, minute=5):
        update = self.telegram.reply_update(cycle, reply_to_message_id=delivered["message_id"], minute=minute)
        with freezegun.freeze_time("2026-10-01 01:00:00"):
            return ingest.ingest_update(update), update

    def cycle_once(self, cycle):
        rc, delivered = self.briefing()
        assert rc == 0 and delivered is not None, "expected a briefing to be delivered"
        event, update = self.reply(cycle, delivered)
        report = state_update_mod.update_state_from_events()
        return {"briefing": delivered, "reply_event": event, "reply_update": update, "report": report}

    def state(self):
        return state_mod.load_state()

    def last_prompt(self):
        return self.provider.prompts[-1]

    def prompts(self):
        return self.provider.prompts


def run_cycles(loop, n=3):
    """Run `n` complete cycles. Each cycle: briefing, reply, state update."""
    return [loop.cycle_once(c) for c in range(1, n + 1)]


# --- 1. every briefing has a unique id --------------------------------------

def test_criterion_1_every_briefing_has_a_unique_id(loop):
    run_cycles(loop)
    ids = [e["briefing_id"] for e in events_log.read_events() if e["event_type"] == "briefing_sent"]
    assert ids == ["briefing_20261001_001", "briefing_20261001_002", "briefing_20261001_003"]
    assert len(set(ids)) == 3


def test_briefing_ids_also_appear_in_the_delivered_text_placeholder(loop):
    cycles = run_cycles(loop)
    for i, c in enumerate(cycles, start=1):
        assert f"briefing_20261001_00{i}" in loop.prompts()[i - 1]


# --- 2 + 3. replies stored with provenance, linked to their briefing ---------

def test_criterion_2_replies_are_stored_with_provenance(loop):
    cycles = run_cycles(loop)
    for c in cycles:
        e = c["reply_event"]
        assert e["event_type"] == "user_reply"
        assert e["source"] == "telegram"
        assert e["project_id"] == "staff-agent"
        assert e["text"] == TEST_REPLIES[int(e["briefing_id"][-3:])]
        assert isinstance(e["telegram_update_id"], int)
        assert isinstance(e["telegram_message_id"], int)
        assert isinstance(e["reply_to_message_id"], int)


def test_criterion_3_reply_is_linked_to_the_briefing_it_answered(loop):
    cycles = run_cycles(loop)
    for i, c in enumerate(cycles, start=1):
        assert c["reply_event"]["briefing_id"] == f"briefing_20261001_00{i}"
        assert c["reply_event"]["reply_to_message_id"] == c["briefing"]["message_id"]


def test_reply_is_also_appended_to_the_day_file(loop):
    from datetime import date
    run_cycles(loop, n=1)
    day_file = episodic.path_for(date(2026, 10, 1))
    assert day_file.exists()
    assert "## User replies" in day_file.read_text(encoding="utf-8")


# --- 4 + 5. replies update inspectable operational state ---------------------

def test_criterion_4_reply_updates_operational_state(loop):
    cycles = run_cycles(loop)
    assert state_mod.load_state()["checkpoints"] == {c: "verified" for c in CHECKPOINTS}


def test_criterion_5_state_changes_are_inspectable_in_the_file(loop):
    run_cycles(loop)
    raw = state_mod.load_state()  # parses cleanly
    assert raw["transitions"]
    joined = "\n".join(raw["transitions"])
    for name in ("reply_ingestion", "storage", "retrieval", "use_in_decision", "feedback_loop"):
        assert f"{name}: " in joined
    assert "not_verified -> verified" in joined
    assert all("signal '" in line for line in raw["transitions"])


# --- 6 + 7 + 8. briefing N+1 reflects cycle N, no re-testing ------------------

def test_criterion_6_briefing_2_reflects_cycle_1(loop):
    run_cycles(loop, n=2)
    cps = checkpoints_from_prompt(loop.prompts()[1])
    assert cps["reply_ingestion"] == "verified"
    assert cps["storage"] == "verified"
    assert cps["retrieval"] == "not_verified"
    assert "reply_ingestion = verified" in loop.prompts()[1]


def test_criterion_6_briefing_3_reflects_cycle_2(loop):
    run_cycles(loop, n=3)
    cps = checkpoints_from_prompt(loop.prompts()[2])
    assert cps["retrieval"] == "verified"
    assert cps["use_in_decision"] == "not_verified"


def test_criterion_7_briefing_2_does_not_recommend_re_testing_verified_steps(loop):
    run_cycles(loop, n=2)
    action = loop.prompts()[1].split("MANDATED NEXT ACTION:")[1].split("\n")[0].strip()
    assert action == "Verify retrieval."
    assert "Verify storage" not in loop.prompts()[1].split("Smallest next action")[1]
    text = loop.telegram.sent[1]["text"]
    assert "Verify retrieval." in text
    assert "Verify storage" not in text
    assert "Verify reply_ingestion" not in text


def test_criterion_7_briefing_3_focuses_on_use_in_decision(loop):
    run_cycles(loop, n=3)
    action = loop.prompts()[2].split("MANDATED NEXT ACTION:")[1].split("\n")[0].strip()
    assert action == "Verify use_in_decision."
    text = loop.telegram.sent[2]["text"]
    assert "Verify use_in_decision." in text
    for stale in ("Verify storage", "Verify retrieval", "Verify reply_ingestion"):
        assert stale not in text


def test_criterion_8_the_recommendation_changes_with_the_evidence(loop):
    run_cycles(loop, n=3)
    actions = [
        p.split("MANDATED NEXT ACTION:")[1].split("\n")[0].strip() for p in loop.prompts()
    ]
    assert actions == ["Verify reply_ingestion.", "Verify retrieval.", "Verify use_in_decision."]


def test_prompt_carries_the_state_not_the_raw_conversation(loop):
    run_cycles(loop, n=2)
    prompt = loop.prompts()[1]
    assert "CURRENT STATE (current_state.md)" in prompt
    assert "MANDATED NEXT ACTION" in prompt
    assert "Next action" not in prompt.split("--- USER PROFILE")[0].split("MANDATED")[0][-200:]


# --- 9. by cycle 3 the target is recognised as achieved ----------------------

def test_criterion_9_briefing_4_recognises_the_target_was_achieved(loop):
    run_cycles(loop, n=3)
    rc, delivered = loop.briefing("2026-10-01 04:00:00")
    assert rc == 0 and delivered is not None
    text = delivered["text"]
    assert "No mismatch. Target output met by:" in text
    assert "No evidence-backed intervention needed." in text
    for name in CHECKPOINTS:
        assert name in text
    assert "still" not in text and "has not been tested" not in text
    assert "Question:" not in text


def test_criterion_9_briefing_4_stops_treating_the_loop_as_the_blocker(loop):
    run_cycles(loop, n=3)
    _, delivered = loop.briefing("2026-10-01 04:00:00")
    assert "retrieval = not_verified" not in delivered["text"]
    assert "use_in_decision = not_verified" not in delivered["text"]
    assert "reply_ingestion = not_verified" not in delivered["text"]


# --- 10. no manual editing of current_state.md between cycles -----------------

def test_criterion_10_no_manual_state_edits_are_needed(loop):
    path = state_mod.DEFAULT_STATE_PATH
    states_written_by_pipeline = []
    for cycle in (1, 2, 3):
        run_one = loop.cycle_once(cycle)
        assert run_one["report"]["transitions"], f"cycle {cycle} produced no transition"
        states_written_by_pipeline.append(path.read_text(encoding="utf-8"))
    assert len(set(states_written_by_pipeline)) == 3
    assert loop.state()["checkpoints"] == {c: "verified" for c in CHECKPOINTS}


def test_criterion_10_replies_alone_drive_every_transition(loop):
    replies = [
        c["reply_event"]["event_id"]
        for c in (loop.cycle_once(i) for i in (1, 2, 3))
    ]
    for c in events_log.read_events():
        if c["event_type"] == "state_update":
            assert c["consumes"] and set(c["consumes"]) <= set(replies)
            assert all(c["consumes"])


# --- 11. the same core logic serves the scheduled run -------------------------

def test_criterion_11_briefing_main_is_the_only_entrypoint(loop):
    import inspect
    source = inspect.getsource(briefing.main)
    assert "state_update_mod.update_state_from_events()" in source
    assert "events_log.append_event" in source
    assert "send_telegram_message" in source


def test_scheduled_entrypoints_remain_unchanged_in_shape():
    assert callable(ingest.main) and callable(briefing.main)


# --- idempotency -------------------------------------------------------------

def test_back_to_back_rerun_with_no_new_evidence_sends_nothing(loop):
    loop.briefing()
    before = len(loop.telegram.sent)
    loop.briefing()
    assert len(loop.telegram.sent) == before


def test_a_briefing_is_still_sent_when_new_evidence_changed_the_state(loop):
    loop.cycle_once(1)
    before = len(loop.telegram.sent)
    _, delivered = loop.briefing()
    assert delivered is not None
    assert len(loop.telegram.sent) == before + 1


def test_state_update_step_is_idempotent(loop):
    run_cycles(loop, n=1)
    first = state_mod.load_state()["checkpoints"]
    again = state_update_mod.update_state_from_events()
    assert again["transitions"] == []
    assert state_mod.load_state()["checkpoints"] == first


def test_events_survive_a_reload_from_disk(loop):
    run_cycles(loop, n=2)
    reloaded = json.loads(json.dumps(events_log.read_events()))
    replies = [e for e in reloaded if e["event_type"] == "user_reply"]
    assert len(replies) == 2
    assert all(r["processed"] for r in replies)


def test_briefing_records_state_version_and_next_action(loop):
    run_cycles(loop, n=1)
    sent = [e for e in events_log.read_events() if e["event_type"] == "briefing_sent"][-1]
    assert sent["state_version"]
    assert sent["next_action"]
