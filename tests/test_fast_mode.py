"""Fast interactive mode: 10-minute silent reply ingestion with immediate state update.

Covers the contract that makes the 10-minute loop safe:

* a genuinely new, properly linked reply advances state within the same run
* a run with no new reply is a *true* no-op -- no state write, no event, no commit
* a message without briefing provenance is stored but can never move a checkpoint
* a batch of replies is processed once, atomically, with nothing lost
* the workflow files carry the right schedule and one shared concurrency group
* the ingest path never generates a briefing, never calls the LLM, never sends

The workflow tests parse the YAML as text rather than importing a YAML library:
PyYAML is not a project dependency and adding one would churn `uv.lock`, which CI
installs with `--frozen`.
"""

import re
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import src.events as events_log
import src.memory.episodic as episodic
import src.state as state_mod
import src.state_update as state_update_mod
import src.ingest_replies as ingest

REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CONCURRENCY_GROUP = "staff-agent-memory-writes"


@pytest.fixture(autouse=True)
def tmp_memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(episodic, "MEMORY_DIR", tmp_path)
    return tmp_path


def _seed_state(monkeypatch, checkpoints: str) -> Path:
    """Write a minimal current_state.md and point the module at it."""
    path = Path(state_mod.DEFAULT_STATE_PATH)
    path.write_text(
        "# current_state.md\n\n"
        "target_output: Prove the episodic loop.\n"
        "next_action: Verify something.\n"
        "blocker: Nothing yet.\n"
        "accountability_source: telegram\n"
        "last_evidence: none\n"
        "confidence: unknown\n"
        "updated_at: 2026-10-01T00:00:00+03:00\n\n"
        "## Checkpoints\n\n"
        f"{checkpoints}"
        "\n## Transitions\n\n_none_\n",
        encoding="utf-8",
    )
    return path


def _seed_delivered_briefing(briefing_id: str, telegram_message_id: int) -> None:
    """Append the delivery event a real reply's provenance must resolve against."""
    events_log.append_event(
        "briefing_sent",
        source="agent",
        briefing_id=briefing_id,
        state_version="seedversion00000",
        next_action="Verify something.",
        text="seed briefing",
    )
    events_log.append_event(
        "briefing_delivered",
        source="telegram",
        briefing_id=briefing_id,
        telegram_message_id=telegram_message_id,
        delivered=True,
    )


def _run_main(updates, last_id: int = 0) -> None:
    with patch("src.ingest_replies.telegram.get_updates", return_value=updates):
        with patch("src.ingest_replies.read_last_update_id", return_value=last_id):
            ingest.main()


def _update(update_id, message_id, text, reply_to=None, day="2026-10-01", hhmm="10:00"):
    day_path = episodic.path_for(date.fromisoformat(day))
    if not day_path.exists():
        episodic.write_day(
            date.fromisoformat(day), "briefing", {"schema_version": 1, "questions_asked": ["q"]}
        )
    return {
        "update_id": update_id,
        "chat_id": 12345,
        "timestamp_cairo": f"{day} {hhmm}",
        "text": text,
        "message_id": message_id,
        "reply_to_message_id": reply_to,
    }


# --------------------------------------------------------------------------
# TEST A -- a new reply triggers state processing in the same run
# --------------------------------------------------------------------------

def test_A_new_reply_triggers_state_processing(tmp_path, monkeypatch):
    state_path = _seed_state(
        monkeypatch,
        "- live_feedback_loop = not_verified | aliases: live feedback loop, bidirectional loop\n",
    )
    _seed_delivered_briefing("briefing_20261001_001", 100)

    before_version = state_mod.state_version(state_mod.load_state(state_path))

    _run_main(
        [_update(1, 101, "The live feedback loop worked.", reply_to=100)]
    )

    after = state_mod.load_state(state_path)
    assert after["checkpoints"]["live_feedback_loop"] == "verified"
    assert state_mod.state_version(after) != before_version

    events = events_log.read_events()
    reply = [e for e in events if e["event_type"] == "user_reply"]
    state_updates = [e for e in events if e["event_type"] == "state_update"]

    assert len(reply) == 1
    assert len(state_updates) == 1, "state must be processed in the same run"
    # The evidence stays auditable: the transition points at the stored reply.
    assert state_updates[0]["transitions"][0]["evidence_event_id"] == reply[0]["event_id"]
    assert state_updates[0]["consumes"] == [reply[0]["event_id"]]
    assert reply[0]["processed"] is True


# --------------------------------------------------------------------------
# TEST B -- no new reply is a true no-op
# --------------------------------------------------------------------------

def test_B_no_reply_makes_no_state_change_and_no_event(tmp_path, monkeypatch):
    state_path = _seed_state(
        monkeypatch,
        "- live_feedback_loop = not_verified | aliases: live feedback loop\n",
    )
    _seed_delivered_briefing("briefing_20261001_001", 100)

    before_text = state_path.read_text(encoding="utf-8")
    before_events = len(events_log.read_events())

    with patch("src.state_update.update_state_from_events") as spy:
        _run_main([], last_id=555)

    spy.assert_not_called()
    assert state_path.read_text(encoding="utf-8") == before_text
    assert len(events_log.read_events()) == before_events
    assert [e for e in events_log.read_events() if e["event_type"] == "state_update"] == []


def test_B2_cursor_is_not_rewritten_on_empty_run(tmp_path, monkeypatch):
    _seed_state(monkeypatch, "- live_feedback_loop = not_verified | aliases: live feedback loop\n")
    written = []
    with patch("src.ingest_replies.telegram.get_updates", return_value=[]):
        with patch("src.ingest_replies.read_last_update_id", return_value=42):
            with patch("src.ingest_replies.write_last_update_id", side_effect=written.append):
                ingest.main()
    assert written == []


# --------------------------------------------------------------------------
# TEST C -- unlinked message is stored but cannot mutate state
# --------------------------------------------------------------------------

def test_C_unlinked_message_cannot_mutate_state(tmp_path, monkeypatch):
    state_path = _seed_state(
        monkeypatch,
        "- live_feedback_loop = not_verified | aliases: live feedback loop\n",
    )
    _seed_delivered_briefing("briefing_20261001_001", 100)

    # No reply_to_message_id: stored, but provenance cannot be established.
    _run_main([_update(1, 101, "The live feedback loop worked.")])

    reply = [e for e in events_log.read_events() if e["event_type"] == "user_reply"][0]
    assert reply["briefing_id"] is None
    assert reply["reply_to_message_id"] is None
    assert reply["processed"] is True, "consumed, but never allowed to act as evidence"

    state = state_mod.load_state(state_path)
    assert state["checkpoints"]["live_feedback_loop"] == "not_verified"

    update = [e for e in events_log.read_events() if e["event_type"] == "state_update"][0]
    assert update["transitions"] == []
    # No eligible evidence, so confidence is left exactly as it was rather than
    # being downgraded by a message that was never an answer to a briefing.
    assert state["confidence"] == "unknown"


def test_C2_reply_to_never_delivered_message_is_not_evidence(tmp_path, monkeypatch):
    state_path = _seed_state(
        monkeypatch,
        "- live_feedback_loop = not_verified | aliases: live feedback loop\n",
    )
    _seed_delivered_briefing("briefing_20261001_001", 100)

    # reply_to 999 was never a delivered briefing message.
    _run_main([_update(1, 101, "The live feedback loop worked.", reply_to=999)])

    assert state_mod.load_state(state_path)["checkpoints"]["live_feedback_loop"] == "not_verified"


# --------------------------------------------------------------------------
# TEST D -- multiple new replies: one batch, nothing lost
# --------------------------------------------------------------------------

def test_D_batch_of_replies_processed_once(tmp_path, monkeypatch):
    state_path = _seed_state(
        monkeypatch,
        "- live_delivery = not_verified | aliases: live delivery\n"
        "- live_feedback_loop = not_verified | aliases: live feedback loop\n",
    )
    _seed_delivered_briefing("briefing_20261001_001", 100)

    with patch("src.state_update.update_state_from_events", wraps=state_update_mod.update_state_from_events) as spy:
        _run_main([
            _update(1, 101, "Live delivery worked.", reply_to=100, hhmm="10:01"),
            _update(2, 102, "nothing to report", reply_to=100, hhmm="10:02"),
            _update(3, 103, "The live feedback loop worked.", reply_to=100, hhmm="10:03"),
        ])

    spy.assert_called_once()

    events = events_log.read_events()
    replies = [e for e in events if e["event_type"] == "user_reply"]
    state_updates = [e for e in events if e["event_type"] == "state_update"]

    assert len(replies) == 3, "no reply may be dropped"
    assert len(state_updates) == 1, "batch processed as a single state_update"
    assert sorted(state_updates[0]["consumes"]) == sorted(e["event_id"] for e in replies)

    # Every reply is marked consumed.
    for e in events_log.read_events():
        if e["event_type"] == "user_reply":
            assert e["processed"] is True

    state = state_mod.load_state(state_path)
    assert state["checkpoints"]["live_delivery"] == "verified"
    assert state["checkpoints"]["live_feedback_loop"] == "verified"

    # Distinct checkpoints from distinct evidence -- not one checkpoint twice.
    touched = [t["checkpoint"] for t in state_updates[0]["transitions"]]
    assert sorted(touched) == ["live_delivery", "live_feedback_loop"]


def test_D2_cursor_advances_to_max_update_id(tmp_path, monkeypatch):
    _seed_state(monkeypatch, "- live_delivery = not_verified | aliases: live delivery\n")
    _seed_delivered_briefing("briefing_20261001_001", 100)
    (tmp_path / ".last_update_id").write_text("0")
    _run_main([
        _update(7, 101, "Live delivery worked.", reply_to=100, hhmm="10:01"),
        _update(19, 102, "Live delivery worked.", reply_to=100, hhmm="10:02"),
        _update(12, 103, "Live delivery worked.", reply_to=100, hhmm="10:03"),
    ])
    assert (tmp_path / ".last_update_id").read_text() == "19"


def test_D3_identical_replies_are_redundant_but_never_wrong(tmp_path, monkeypatch):
    """Pin the known duplicate-transition behaviour so a future change is deliberate.

    Three copies of one reply log three `not_verified -> verified` lines for the same
    checkpoint. That is noisy but not unsafe, and it predates fast mode: the final
    value is correct, every reply is consumed, each line keeps its own evidence id,
    and `state_version` is derived only from target_output/next_action/blocker/
    checkpoints, so redundancy cannot change it. Batch processing under the 10-minute
    cron makes this *rarer*, since replies arrive in smaller groups. Fixing the
    cosmetic duplication is deferred to a separate cleanup pass.
    """
    state_path = _seed_state(monkeypatch, "- live_delivery = not_verified | aliases: live delivery\n")
    _seed_delivered_briefing("briefing_20261001_001", 100)

    _run_main([
        _update(1, 101, "Live delivery worked.", reply_to=100, hhmm="10:01"),
        _update(2, 102, "Live delivery worked.", reply_to=100, hhmm="10:02"),
        _update(3, 103, "Live delivery worked.", reply_to=100, hhmm="10:03"),
    ])

    update = [e for e in events_log.read_events() if e["event_type"] == "state_update"][0]
    assert len(update["transitions"]) == 3
    assert {t["checkpoint"] for t in update["transitions"]} == {"live_delivery"}
    # Every redundant line still points at its own reply, so the audit trail holds.
    assert len({t["evidence_event_id"] for t in update["transitions"]}) == 3

    state = state_mod.load_state(state_path)
    assert state["checkpoints"]["live_delivery"] == "verified"
    assert state["next_action"] == "No evidence-backed intervention needed."


# --------------------------------------------------------------------------
# TEST E -- workflow schedules
# --------------------------------------------------------------------------

def _crons(path: Path) -> list[str]:
    return re.findall(r'-\s*cron:\s*"([^"]+)"', path.read_text(encoding="utf-8"))


def test_E_ingest_runs_every_ten_minutes():
    assert _crons(WORKFLOWS / "ingest-replies.yml") == ["*/10 * * * *"]


def test_E2_daily_briefing_schedule_unchanged():
    assert _crons(WORKFLOWS / "daily-briefing.yml") == ["0 5 * * *"]


@pytest.mark.parametrize("name", ["ingest-replies.yml", "daily-briefing.yml"])
def test_E3_both_retain_manual_dispatch(name):
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert re.search(r"^on:", text, re.MULTILINE)
    assert re.search(r"^\s+workflow_dispatch:\s*$", text, re.MULTILINE)


def test_E4_ingest_keeps_contents_write():
    text = (WORKFLOWS / "ingest-replies.yml").read_text(encoding="utf-8")
    assert re.search(r"^permissions:\s*\n\s+contents:\s*write\s*$", text, re.MULTILINE)


# --------------------------------------------------------------------------
# TEST F -- one shared concurrency group across both memory writers
# --------------------------------------------------------------------------

def _concurrency_block(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"^concurrency:\s*\n((?:[ \t]+.*\n?)+)", text, re.MULTILINE)
    assert match, f"{path.name} has no top-level concurrency block"
    block = {}
    for key, value in re.findall(r"^\s+([a-z-]+):\s*(.+?)\s*$", match.group(1), re.MULTILINE):
        block[key] = value.strip().strip('"').strip("'")
    return block


@pytest.mark.parametrize("name", ["ingest-replies.yml", "daily-briefing.yml"])
def test_F_both_use_the_same_shared_group(name):
    block = _concurrency_block(WORKFLOWS / name)
    assert block["group"] == CONCURRENCY_GROUP
    assert block["cancel-in-progress"] == "false", (
        "cancelling a memory writer could advance the Telegram cursor without "
        "pushing the commit, silently dropping a reply"
    )


def test_F2_no_workflow_uses_a_private_group():
    """A workflow-specific group would defeat the mutual exclusion entirely."""
    found = set()
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        text = wf.read_text(encoding="utf-8")
        found.update(re.findall(r"^\s+group:\s*(\S+)", text, re.MULTILINE))
    assert found == {CONCURRENCY_GROUP}


# --------------------------------------------------------------------------
# TEST G -- the ingest path stays silent
# --------------------------------------------------------------------------

def test_G_ingest_never_imports_briefing_or_llm():
    source = (REPO_ROOT / "src" / "ingest_replies.py").read_text(encoding="utf-8")
    for forbidden in ("import src.briefing", "from src.briefing",
                      "from src.llm", "import src.llm",
                      "from src.portfolio_briefing", "import src.portfolio_briefing",
                      "send_telegram_message", "get_provider"):
        assert forbidden not in source, f"ingest must not reference {forbidden}"


def test_G2_state_update_path_makes_no_llm_or_telegram_call(tmp_path, monkeypatch):
    """Exercise the real state-update step, with LLM and send both booby-trapped."""
    _seed_state(monkeypatch, "- live_delivery = not_verified | aliases: live delivery\n")
    _seed_delivered_briefing("briefing_20261001_001", 100)

    before = [e["event_type"] for e in events_log.read_events()]

    def boom(*a, **k):
        raise AssertionError("the silent ingest path must not call the LLM or Telegram")

    with patch("src.llm.factory.get_provider", side_effect=boom):
        with patch("src.telegram_client.send_telegram_message", side_effect=boom):
            ingest.apply_state_update()

    after = [e["event_type"] for e in events_log.read_events()]
    # Only the seeded briefing pair exists; state processing appended nothing of its own.
    assert after == before
