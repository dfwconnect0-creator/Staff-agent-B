"""Reply -> explicit state transition. Deterministic and inspectable."""
import pytest

import src.memory.episodic as episodic
import src.state as state_mod
import src.state_update as su
import src.events as events_log

CYCLE_REPLIES = {
    1: "Cycle 1 update: I completed reply ingestion. The reply was stored successfully. "
       "Retrieval has not been tested yet.",
    2: "Cycle 2 update: Retrieval worked. The system found the previous reply correctly, "
       "but the retrieved information was not used to change the next recommendation.",
    3: "Cycle 3 update: The next briefing correctly used the retrieved information and "
       "changed its recommendation. The full feedback loop worked.",
}


@pytest.fixture(autouse=True)
def tmp_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(episodic, "MEMORY_DIR", tmp_path / "memory")
    (tmp_path / "memory").mkdir()
    state_file = tmp_path / "current_state.md"
    state_file.write_text(
        state_mod.render_state(
            {
                **state_mod.empty_state(),
                "target_output": "Prove episodic-memory feedback loop.",
                "checkpoints": {
                    "reply_ingestion": "not_verified",
                    "storage": "not_verified",
                    "retrieval": "not_verified",
                    "use_in_decision": "not_verified",
                    "feedback_loop": "not_verified",
                },
                "aliases": {
                    "reply_ingestion": ["reply ingestion"],
                    "storage": ["stored", "reply was stored"],
                    "retrieval": ["retrieval"],
                    "use_in_decision": ["retrieved information"],
                    "feedback_loop": ["feedback loop"],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(state_mod, "DEFAULT_STATE_PATH", state_file)
    return state_file


# --- sentence handling -------------------------------------------------------

def test_sentences_split_on_punctuation():
    assert su.split_sentences("A worked. B did not work! C?") == ["A worked", "B did not work", "C"]


# --- per-checkpoint extraction ----------------------------------------------

def test_positive_signal_marks_verified():
    st = {"checkpoints": {"storage": "not_verified"}, "aliases": {"storage": ["stored"]}}
    t = su.extract_transitions("The reply was stored successfully.", st)
    assert len(t) == 1
    assert t[0]["to"] == "verified"
    assert t[0]["matched_alias"] == "stored"
    assert t[0]["quote"] == "The reply was stored successfully"


def test_negative_signal_marks_not_verified():
    st = {"checkpoints": {"retrieval": "verified"}, "aliases": {"retrieval": ["retrieval"]}}
    t = su.extract_transitions("Retrieval has not been tested yet.", st)
    assert t[0]["to"] == "not_verified"
    assert t[0]["signal"] in su.NEGATIVE_SIGNALS


def test_negative_signal_beats_positive_signal_in_the_same_sentence():
    st = {"checkpoints": {"use_in_decision": "verified"},
          "aliases": {"use_in_decision": ["retrieved information"]}}
    t = su.extract_transitions(
        "The system found the previous reply correctly, but the retrieved information "
        "was not used to change the next recommendation.", st)
    assert t[0]["to"] == "not_verified"
    assert t[0]["signal"] == "not used"


def test_a_negative_signal_does_not_produce_a_change_when_already_not_verified():
    st = {"checkpoints": {"use_in_decision": "not_verified"},
          "aliases": {"use_in_decision": ["retrieved information"]}}
    t = su.extract_transitions(
        "The retrieved information was not used to change the next recommendation.", st)
    assert t == []


def test_no_alias_mention_leaves_the_checkpoint_alone():
    st = {"checkpoints": {"storage": "not_verified"}, "aliases": {"storage": ["stored"]}}
    assert su.extract_transitions("Something entirely unrelated.", st) == []


def test_alias_without_any_signal_produces_no_transition():
    st = {"checkpoints": {"storage": "not_verified"}, "aliases": {"storage": ["stored"]}}
    assert su.extract_transitions("The storage design is still unclear.", st) == []


def test_value_already_held_produces_no_transition():
    st = {"checkpoints": {"storage": "verified"}, "aliases": {"storage": ["stored"]}}
    assert su.extract_transitions("The reply was stored successfully.", st) == []


def test_last_mention_in_a_reply_wins():
    st = {"checkpoints": {"retrieval": "not_verified"}, "aliases": {"retrieval": ["retrieval"]}}
    t = su.extract_transitions("Retrieval has not been tested. Then retrieval worked.", st)
    assert t[0]["to"] == "verified"
    assert t[0]["quote"] == "Then retrieval worked"


def test_checkpoint_name_itself_is_accepted_without_aliases():
    st = {"checkpoints": {"feedback_loop": "not_verified"}, "aliases": {"feedback_loop": []}}
    t = su.extract_transitions("The feedback loop worked.", st)
    assert t[0]["to"] == "verified"


# --- full cycle behaviour ----------------------------------------------------

def test_no_events_leaves_state_untouched(tmp_path):
    before = state_mod.load_state()
    report = su.update_state_from_events()
    assert report["transitions"] == []
    assert state_mod.load_state()["checkpoints"] == before["checkpoints"]


def test_cycle_1_reply_moves_exactly_two_checkpoints(tmp_path):
    events_log.append_event("user_reply", processed=False, text=CYCLE_REPLIES[1])
    report = su.update_state_from_events()
    cps = state_mod.load_state()["checkpoints"]
    assert cps["reply_ingestion"] == "verified"
    assert cps["storage"] == "verified"
    assert cps["retrieval"] == "not_verified"
    assert cps["use_in_decision"] == "not_verified"
    assert [t["checkpoint"] for t in report["transitions"]] == ["reply_ingestion", "storage"]


def test_cycle_1_sets_next_action_to_retrieval(tmp_path):
    events_log.append_event("user_reply", processed=False, text=CYCLE_REPLIES[1])
    su.update_state_from_events()
    assert state_mod.load_state()["next_action"] == "Verify retrieval."


def test_cycle_2_reply_moves_retrieval_and_use_in_decision(tmp_path):
    events_log.append_event("user_reply", processed=False, text=CYCLE_REPLIES[1])
    su.update_state_from_events()
    events_log.append_event("user_reply", processed=False, text=CYCLE_REPLIES[2])
    su.update_state_from_events()
    cps = state_mod.load_state()["checkpoints"]
    assert cps == {
        "reply_ingestion": "verified",
        "storage": "verified",
        "retrieval": "verified",
        "use_in_decision": "not_verified",
        "feedback_loop": "not_verified",
    }
    assert state_mod.load_state()["next_action"] == "Verify use_in_decision."


def test_cycle_3_reply_verifies_everything(tmp_path):
    for i in (1, 2, 3):
        events_log.append_event("user_reply", processed=False, text=CYCLE_REPLIES[i])
        su.update_state_from_events()
    cps = state_mod.load_state()["checkpoints"]
    assert all(v == "verified" for v in cps.values())
    assert state_mod.load_state()["next_action"] == "No evidence-backed intervention needed."


def test_state_transition_is_recorded_inside_the_state_file(tmp_path):
    events_log.append_event("user_reply", processed=False, text=CYCLE_REPLIES[1])
    su.update_state_from_events()
    raw = tmp_path.joinpath("current_state.md").read_text(encoding="utf-8")
    assert "reply_ingestion: not_verified -> verified" in raw
    assert "storage: not_verified -> verified" in raw
    assert "evt_000001" in raw


def test_events_are_marked_processed_only_after_the_state_is_written(tmp_path):
    events_log.append_event("user_reply", processed=False, text=CYCLE_REPLIES[1])
    su.update_state_from_events()
    assert events_log.unprocessed_replies() == []
    update = [e for e in events_log.read_events() if e["event_type"] == "state_update"][0]
    assert update["consumes"] == ["evt_000001"]
    assert update["state_version"]


def test_running_the_state_update_twice_is_a_no_op(tmp_path):
    events_log.append_event("user_reply", processed=False, text=CYCLE_REPLIES[1])
    first = su.update_state_from_events()
    second = su.update_state_from_events()
    assert first["transitions"]
    assert second["transitions"] == []
    assert first["state_version"] == second["state_version"]


def test_reply_that_supports_no_change_leaves_confidence_low(tmp_path):
    events_log.append_event("user_reply", processed=False, text="Unrelated small talk.")
    report = su.update_state_from_events()
    assert report["transitions"] == []
    assert state_mod.load_state()["confidence"] == "low"


def test_last_evidence_points_at_the_consumed_event(tmp_path):
    events_log.append_event("user_reply", processed=False, text=CYCLE_REPLIES[1])
    su.update_state_from_events()
    assert state_mod.load_state()["last_evidence"].startswith("evt_000001 - user_reply")
