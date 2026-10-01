"""current_state.md: parse, render, checkpoint table, next action derivation."""
import pytest

import src.state as state_mod


SEED = """# current_state.md

target_output: Prove the episodic loop.
next_action: Verify storage.
blocker: Nothing has been tested.
accountability_source: telegram
last_evidence: evt_000002 - user_reply (2026-10-01T09:00:00+03:00)
confidence: low
updated_at: 2026-10-01T08:00:00+03:00

## Checkpoints

- reply_ingestion = not_verified | aliases: reply ingestion
- storage = not_verified | aliases: stored, reply was stored
- retrieval = verified

## Transitions

- 2026-10-01T09:00:00+03:00 | evt_000002 | storage: unknown -> not_verified | matched 'stored' + signal 'stored'
"""


@pytest.fixture
def seeded(tmp_path):
    p = tmp_path / "current_state.md"
    p.write_text(SEED, encoding="utf-8")
    return p


def test_missing_file_yields_unknown_everywhere(tmp_path):
    s = state_mod.load_state(tmp_path / "nope.md")
    assert s["target_output"] == "Unknown"
    assert s["checkpoints"] == {}
    assert s["next_action"] == "Unknown"


def test_all_operational_fields_are_parsed(seeded):
    s = state_mod.load_state(seeded)
    assert s["target_output"] == "Prove the episodic loop."
    assert s["blocker"] == "Nothing has been tested."
    assert s["accountability_source"] == "telegram"
    assert s["last_evidence"] == "evt_000002 - user_reply (2026-10-01T09:00:00+03:00)"
    assert s["confidence"] == "low"


def test_checkpoints_and_aliases_are_parsed(seeded):
    s = state_mod.load_state(seeded)
    assert s["checkpoints"] == {"reply_ingestion": "not_verified", "storage": "not_verified", "retrieval": "verified"}
    assert s["aliases"]["storage"] == ["stored", "reply was stored"]
    assert s["aliases"]["retrieval"] == []


def test_transition_log_is_parsed(seeded):
    s = state_mod.load_state(seeded)
    assert len(s["transitions"]) == 1
    assert "storage: unknown -> not_verified" in s["transitions"][0]


def test_render_then_load_round_trips(seeded):
    before = state_mod.load_state(seeded)
    p = state_mod.write_state(before, seeded)
    after = state_mod.load_state(p)
    for key in ("target_output", "blocker", "accountability_source", "confidence"):
        assert after[key] == before[key]
    assert after["checkpoints"] == before["checkpoints"]
    assert after["aliases"] == before["aliases"]


def test_render_keeps_field_order_and_preserves_unknowns(seeded):
    text = state_mod.render_state(state_mod.load_state(seeded))
    assert text.index("target_output:") < text.index("next_action:") < text.index("blocker:")
    assert "## Checkpoints" in text


def test_next_action_targets_the_first_unverified_checkpoint():
    s = {
        "checkpoints": {
            "reply_ingestion": "verified",
            "storage": "verified",
            "retrieval": "not_verified",
            "use_in_decision": "not_verified",
        }
    }
    assert state_mod.next_action_from_state(s) == "Verify retrieval."


def test_next_action_skips_verified_checkpoints():
    s = {
        "checkpoints": {
            "reply_ingestion": "verified",
            "storage": "verified",
            "retrieval": "verified",
            "use_in_decision": "not_verified",
        }
    }
    assert state_mod.next_action_from_state(s) == "Verify use_in_decision."


def test_next_action_is_silence_when_everything_is_verified():
    s = {"checkpoints": {"a": "verified", "b": "verified"}}
    assert state_mod.next_action_from_state(s) == "No evidence-backed intervention needed."


def test_next_action_is_unknown_when_there_are_no_checkpoints():
    assert state_mod.next_action_from_state({"checkpoints": {}}) == "Unknown"


def test_state_version_changes_when_a_checkpoint_changes():
    a = {"target_output": "t", "next_action": "Verify x.", "blocker": "b",
         "checkpoints": {"x": "not_verified"}}
    b = {"target_output": "t", "next_action": "Verify y.", "blocker": "b",
         "checkpoints": {"x": "verified"}}
    assert state_mod.state_version(a) != state_mod.state_version(b)


def test_state_version_is_stable_for_identical_state():
    a = {"target_output": "t", "next_action": "n", "blocker": "b", "checkpoints": {"x": "verified"}}
    b = {"target_output": "t", "next_action": "n", "blocker": "b", "checkpoints": {"x": "verified"}}
    assert state_mod.state_version(a) == state_mod.state_version(b)


def test_state_version_ignores_non_decision_fields():
    a = {"target_output": "t", "next_action": "n", "blocker": "b", "checkpoints": {}, "confidence": "low"}
    b = {"target_output": "t", "next_action": "n", "blocker": "b", "checkpoints": {}, "confidence": "high"}
    assert state_mod.state_version(a) == state_mod.state_version(b)
