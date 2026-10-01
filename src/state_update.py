"""Turn user replies into explicit, inspectable state transitions.

This is the step the project was missing. A reply used to reach the next briefing
as prose and the model was asked to "adjust". Nothing recorded what actually
changed, so an item that had already been tested could be re-recommended forever.

The transition rule here is deliberately deterministic and auditable rather than
model-generated:

* ``current_state.md`` lists *checkpoints* and, for each, the phrases a human would
  actually use to talk about it (``aliases``).
* A reply is split into sentences. For each checkpoint, the **last** sentence that
  mentions one of its aliases decides the new value.
* Within that sentence, a negative signal wins over a positive one. "I tested it
  and it worked, but the next briefing ignored it" must not read as verified.
* Every applied transition records the evidence event id and the exact matched
  signal, so the change is readable in the state file rather than inferred.

Only transitions supported by a quoted signal are applied. Nothing is inferred
from silence: a checkpoint the reply does not mention keeps its current value.
"""

import re
from datetime import datetime, timezone, timedelta

import src.events as events_log
import src.state as state_mod

CAIRO_TZ = timezone(timedelta(hours=3))

NEGATIVE_SIGNALS = (
    "not tested",
    "not verified",
    "not used",
    "not working",
    "not retrieved",
    "not confirmed",
    "not yet",
    "still not",
    "did not",
    "does not",
    "doesn't",
    "do not",
    "don't",
    "was not",
    "wasn't",
    "were not",
    "weren't",
    "has not",
    "have not",
    "had not",
    "no evidence",
    "unverified",
    "never",
    "failed",
    "fails",
    "failure",
    "broke",
    "broken",
    "ignored",
    "wrong",
    "regressed",
)

POSITIVE_SIGNALS = (
    "worked",
    "works",
    "verified",
    "passed",
    "passes",
    "succeed",
    "success",
    "completed",
    "confirmed",
    "correctly",
    "properly",
    "stored",
    "resolved",
    "fixed",
    "shipped",
    "done",
    "fine",
    "green",
)

MAX_TRANSITION_LOG = 20

_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def _find_signal(sentence: str) -> str | None:
    lowered = sentence.lower()
    negative = [s for s in NEGATIVE_SIGNALS if s in lowered]
    if negative:
        return negative[0]
    positive = [s for s in POSITIVE_SIGNALS if s in lowered]
    return positive[0] if positive else None


def _value_for(signal: str | None) -> str | None:
    if signal is None:
        return None
    if signal in NEGATIVE_SIGNALS:
        return "not_verified"
    return "verified"


def extract_transitions(text: str, state: dict) -> list[dict]:
    """Candidate checkpoint transitions implied by one reply.

    Returns transitions with the value that *would* be applied, whether or not it
    differs from the current value, plus the sentence and signal that produced it.
    """
    sentences = split_sentences(text)
    aliases = state.get("aliases", {})
    current = state.get("checkpoints", {})
    out = []
    for name, phrases in aliases.items():
        needles = [p for p in [name.replace("_", " "), *phrases] if p]
        hit = None
        for sentence in sentences:
            lowered = sentence.lower()
            for needle in needles:
                if needle.lower() in lowered:
                    hit = (sentence, needle)
                    break
        if not hit:
            continue
        sentence, needle = hit
        signal = _find_signal(sentence)
        new_value = _value_for(signal)
        if new_value is None:
            continue
        if current.get(name) == new_value:
            continue
        out.append(
            {
                "checkpoint": name,
                "from": current.get(name, "unknown"),
                "to": new_value,
                "matched_alias": needle,
                "signal": signal,
                "quote": sentence,
            }
        )
    return out


def _describe(event: dict) -> str:
    return f"{event['event_id']} - {event['event_type']} ({event.get('timestamp', 'unknown time')})"


def update_state_from_events(state_path=None) -> dict:
    """The state-update step. Run before every briefing generation.

    1. read new unprocessed events
    2. derive the transitions they support
    3. write ``current_state.md`` only with those transitions applied
    4. mark the events processed

    Returns a report dict; ``transitions`` is empty when nothing changed.
    """
    state = state_mod.load_state(state_path)
    pending = events_log.unprocessed_replies()

    transitions = []
    for event in pending:
        for t in extract_transitions(event.get("text", ""), state):
            transitions.append({**t, "evidence_event_id": event["event_id"]})

    for t in transitions:
        state["checkpoints"][t["checkpoint"]] = t["to"]

    if transitions:
        stamp = datetime.now(CAIRO_TZ).isoformat(timespec="seconds")
        lines = [
            f"- {stamp} | {t['evidence_event_id']} | {t['checkpoint']}: "
            f"{t['from']} -> {t['to']} | matched '{t['matched_alias']}' + signal '{t['signal']}'"
            for t in transitions
        ]
        state["transitions"] = (state.get("transitions", []) + lines)[-MAX_TRANSITION_LOG:]

    state["next_action"] = state_mod.next_action_from_state(state)
    state["updated_at"] = datetime.now(CAIRO_TZ).isoformat(timespec="seconds")
    if pending:
        state["last_evidence"] = _describe(pending[-1])
        state["confidence"] = "high" if transitions else "low"

    state_mod.write_state(state, state_path)

    version = state_mod.state_version(state)
    if pending:
        events_log.mark_processed(
            consumes=[e["event_id"] for e in pending],
            transitions=transitions,
            state_version=version,
        )

    return {
        "processed_event_ids": [e["event_id"] for e in pending],
        "transitions": transitions,
        "state_version": version,
        "next_action": state["next_action"],
    }
