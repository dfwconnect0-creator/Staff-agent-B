"""Shared stubs for the episodic-loop test.

Both `tests/test_loop_three_cycles.py` and `tests/run_episodic_loop.py` use these,
so the automated gate and the manual trigger exercise identical stand-ins.

The stubs replace exactly two network boundaries and nothing else:

  * the LLM, at the `src.briefing.get_provider` seam
  * the Telegram HTTP API, at the `src.briefing.send_telegram_message` seam

Simulated Telegram replies still enter through `src.ingest_replies.ingest_update`,
the same function the 2-hourly cron calls per update.
"""

import json

TEST_REPLIES = {
    1: "Cycle 1 update: I completed reply ingestion. The reply was stored successfully. "
       "Retrieval has not been tested yet.",
    2: "Cycle 2 update: Retrieval worked. The system found the previous reply correctly, "
       "but the retrieved information was not used to change the next recommendation.",
    3: "Cycle 3 update: The next briefing correctly used the retrieved information and "
       "changed its recommendation. The full feedback loop worked.",
}

CHECKPOINTS = ["reply_ingestion", "storage", "retrieval", "use_in_decision", "feedback_loop"]

# The starting condition for the loop experiment, defined here so the manual trigger
# and the test suite cannot disagree about it. This is *setup*, applied once before
# cycle 1 — no step inside a cycle writes it. Everything after it is pipeline-written.
LOOP_SEED = """# current_state.md

Operational state for the staff-agent. Changing state lives here, not in `user.md`.
Rewritten by the state-update step in `src/briefing.py`; safe to edit by hand.

target_output: Prove episodic-memory feedback loop: 3 consecutive briefing -> reply -> state change -> changed briefing cycles.
next_action: Verify reply_ingestion.
blocker: Reply ingestion has never been run against a live Telegram chat in this project.
accountability_source: telegram
last_evidence: none
confidence: low
updated_at: 2026-10-01T00:00:00+03:00

## Checkpoints

- reply_ingestion = not_verified | aliases: reply ingestion
- storage = not_verified | aliases: stored, reply was stored
- retrieval = not_verified | aliases: retrieval
- use_in_decision = not_verified | aliases: retrieved information
- feedback_loop = not_verified | aliases: feedback loop

## Transitions

_none_
"""


def reset_state_to_seed(path):
    """Put the state file back at the experiment's starting condition."""
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(LOOP_SEED, encoding="utf-8")
    return p


def _between(text, start, end):
    i = text.find(start)
    if i < 0:
        return "Unknown"
    i += len(start)
    j = text.find(end, i)
    return (text[i:j] if j > 0 else text[i:]).strip()


def checkpoints_from_prompt(user_message):
    block = _between(user_message, "Checkpoints:\n", "\n\nMANDATED NEXT ACTION:")
    out = {}
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("- ") and " = " in line:
            name, value = line[2:].split(" = ", 1)
            out[name.strip()] = value.strip()
    return out


def next_action_from_prompt(user_message):
    return _between(user_message, "MANDATED NEXT ACTION: ", "\n")


class ScriptedProvider:
    """Stands in for the LLM.

    It renders the briefing using only what the real pipeline put into the prompt:
    the target output, the checkpoint table, and the mandated next action. It has no
    access to `current_state.md` and no knowledge of which cycle it is on, so if
    briefing N+1 does not reflect cycle N, its output shows that.
    """

    name = "scripted"
    model = "scripted-state-driven"

    def __init__(self):
        self.prompts = []

    def complete(self, system_prompt, user_message):
        self.prompts.append(user_message)
        target = _between(user_message, "Target output: ", "\nBlocker:")
        action = next_action_from_prompt(user_message)
        checkpoints = checkpoints_from_prompt(user_message)
        verified = [k for k, v in checkpoints.items() if v == "verified"]
        pending = [k for k, v in checkpoints.items() if v != "verified"]

        outcome = f"{target} Verified: {', '.join(verified) if verified else 'nothing yet'}."
        state_block = " | ".join(f"{k} = {v}" for k, v in checkpoints.items()) or "unknown"
        if pending:
            mismatch = f"{pending[0]} is {checkpoints[pending[0]]}, which blocks the target output."
            question = "Question:\nDoes the state above match what you actually observed?"
        else:
            mismatch = f"No mismatch. Target output met by: {', '.join(verified)}."
            question = ""

        text = (
            f"Current outcome:\n{outcome}\n\n"
            f"State:\n{state_block}\n\n"
            f"Mismatch or blocker:\n{mismatch}\n\n"
            f"Smallest next action:\n{action}\n"
        )
        if question:
            text += f"\n{question}\n"

        prediction = {
            "schema_version": 1,
            "stuck_item": (f"{pending[0]} is {checkpoints[pending[0]]}" if pending else "none")[:300],
            "smallest_action": action[:300],
            "confidence": "medium" if pending else "high",
            "flags_raised": [],
            "questions_asked": ["Does the state above match what you observed?"] if pending else ["none"],
        }
        return {
            "text": text,
            "prediction": prediction,
            "raw": text + "\n```json\n" + json.dumps(prediction, indent=2) + "\n```\n",
            "provider": self.name,
            "model": self.model,
        }


class FakeTelegram:
    """Stands in for the Telegram HTTP API at the `send_telegram_message` seam."""

    def __init__(self):
        self.sent = []
        self._next_message_id = 5000
        self._next_update_id = 900000
        self._next_reply_id = 6000
        self.day = "2026-10-01"

    def send(self, text):
        self._next_message_id += 1
        self.sent.append({"message_id": self._next_message_id, "text": text})
        return self._next_message_id

    def reply_update(self, cycle, reply_to_message_id, hour=None, minute=5):
        self._next_update_id += 1
        self._next_reply_id += 1
        hour = cycle + 1 if hour is None else hour
        return {
            "update_id": self._next_update_id,
            "chat_id": 12345,
            "text": TEST_REPLIES[cycle],
            "message_id": self._next_reply_id,
            "reply_to_message_id": reply_to_message_id,
            "timestamp_cairo": f"{self.day} {hour:02d}:{minute:02d}",
        }
