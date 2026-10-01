import json
import logging
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from src.llm.factory import get_provider
from src.telegram_client import send_telegram_message
import src.memory.episodic as episodic
import src.events as events_log
import src.state as state_mod
import src.state_update as state_update_mod
from src.memory.schema import validate_prediction

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CAIRO_TZ = timezone(timedelta(hours=3))
CONTEXT_DIR = Path(__file__).parent.parent / "context"

BRIEFING_FORMAT = """Current outcome:
...

State:
...

Mismatch or blocker:
...

Smallest next action:
...

Question:
... (omit this block entirely if nothing is genuinely missing)"""


def load_context() -> dict:
    def read(name: str) -> str:
        p = CONTEXT_DIR / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    return {
        "soul": read("soul.md"),
        "user": read("user.md"),
        "heartbeat": read("heartbeat.md"),
        "current_state": read("current_state.md"),
    }


def build_prompt(context: dict, state: dict, briefing_id: str, today: date, recent_events: list[dict],
                 yesterday_memory: dict | None = None) -> tuple[str, str]:
    day_name = today.strftime("%A")
    date_str = today.isoformat()

    system_prompt = context["soul"]

    yesterday_section = ""
    if yesterday_memory and (yesterday_memory.get("prediction") or yesterday_memory.get("user_replies")):
        yesterday_date = yesterday_memory.get("date", (today - timedelta(days=1)).isoformat())
        yesterday_section = f"## Yesterday's context\n\nYesterday ({yesterday_date}):\n"
        if yesterday_memory.get("prediction"):
            prediction_json = json.dumps(yesterday_memory["prediction"], indent=2, ensure_ascii=False)
            yesterday_section += f"""
You predicted:
```json
{prediction_json}
```
"""
        if yesterday_memory.get("user_replies"):
            replies_text = "\n".join(
                f'- {r["timestamp"]} Cairo: "{r["text"]}"' for r in yesterday_memory["user_replies"]
            )
            yesterday_section += f"""
You received:
{replies_text}
"""
        yesterday_section += """
Compare that against the state above. Where current_state.md already recorded the answer,
the state wins.
"""

    events_section = ""
    if recent_events:
        lines = []
        for e in recent_events:
            lines.append(
                f'- [{e["event_id"]}] {e["event_type"]} at {e.get("timestamp", "?")} '
                f'(briefing: {e.get("briefing_id") or "n/a"})'
            )
        events_section = f"""## Recent events (already applied to current_state.md)

{chr(10).join(lines)}

The state above was already updated from these events. Do not re-apply them and do not
ask the user to test anything the state says is already verified.
"""

    checkpoints = state.get("checkpoints", {})
    checkpoint_lines = "\n".join(f"- {k} = {v}" for k, v in checkpoints.items()) or "_none_"

    user_message = f"""Today is {day_name}, {date_str} (Cairo time). Briefing id: {briefing_id}.

{yesterday_section}{events_section}--- CURRENT STATE (current_state.md) — this is the source of truth ---

Target output: {state.get('target_output', 'Unknown')}
Blocker: {state.get('blocker', 'Unknown')}
Accountability source: {state.get('accountability_source', 'unknown')}
Last evidence: {state.get('last_evidence', 'none')}
Confidence: {state.get('confidence', 'unknown')}

Checkpoints:
{checkpoint_lines}

MANDATED NEXT ACTION: {state.get('next_action', 'Unknown')}

That next action is derived from the checkpoint table above. Use it. Do not substitute a
different action, do not re-test anything already marked verified, and do not invent work.

--- USER PROFILE (user.md) ---
{context['user']}

--- INTERVENTION POLICY (heartbeat.md) ---
{context['heartbeat']}

--- YOUR TASK ---

Write one briefing in exactly this format, and nothing else:

{BRIEFING_FORMAT}

Rules for the content:
- "Current outcome" is the target output plus what is actually true now. One project only.
- "State" lists the checkpoints that matter to this target, with their current values.
- "Mismatch or blocker" names the gap between outcome and state, or says there is none.
- "Smallest next action" is the MANDATED NEXT ACTION above, or the genuine next transition
  from current_state.md. Never a step that is already verified.
- "Question" appears only when information is genuinely missing. One question at most.
- If every checkpoint is verified and the target is met, say so and either name the next
  genuine transition from current_state.md or state that no intervention is needed.
- "No intervention needed" is a valid, acceptable answer.

No filler. No preamble. No motivational filler. No "you've got this."

AFTER the briefing text, output a JSON block with this exact structure:

```json
{{
  "schema_version": 1,
  "stuck_item": "one-line name of the stuck item, or 'none'",
  "smallest_action": "the single smallest action, or 'none'",
  "confidence": "low|medium|high",
  "flags_raised": ["flag1"],
  "questions_asked": ["question 1"]
}}
```

The JSON must come last. The briefing text (above the JSON) is what I'll read on my phone.
"""
    return system_prompt, user_message


def main() -> int:
    today = datetime.now(CAIRO_TZ).date()

    report = state_update_mod.update_state_from_events()
    if report["transitions"]:
        for t in report["transitions"]:
            log.info(
                f"state change: {t['checkpoint']} {t['from']} -> {t['to']} "
                f"(evidence {t['evidence_event_id']}, signal '{t['signal']}')"
            )
    state = state_mod.load_state()
    log.info(f"state_version={report['state_version']} next_action={state['next_action']}")

    previous = events_log.last_delivered_briefing(today)
    if previous and previous.get("state_version") == report["state_version"]:
        log.info(
            f"state unchanged since {previous['briefing_id']} and it was already delivered "
            f"today; skipping send"
        )
        return 0

    briefing_id = events_log.next_briefing_id(today)

    context = load_context()
    try:
        yesterday_memory = episodic.read_day(today - timedelta(days=1))
    except Exception as e:
        log.warning(f"Failed to read yesterday's memory: {e}, continuing without context")
        yesterday_memory = None

    recent_events = [
        e for e in events_log.read_events() if e.get("event_type") in ("user_reply", "state_update")
    ][-8:]

    system_prompt, user_message = build_prompt(
        context, state, briefing_id, today, recent_events, yesterday_memory
    )

    result = get_provider().complete(system_prompt, user_message)
    log.info(f"LLM response from provider={result['provider']} model={result['model']}")
    briefing_text = result["text"]
    prediction = result["prediction"]

    if not briefing_text.strip():
        send_telegram_message("⚠️ Empty briefing. Check logs.")
        return 1

    events_log.append_event(
        "briefing_sent",
        source="agent",
        briefing_id=briefing_id,
        state_version=report["state_version"],
        next_action=state["next_action"],
        prediction=prediction,
        text=briefing_text,
    )

    message_id = send_telegram_message(briefing_text)
    if not isinstance(message_id, int):
        message_id = None
    events_log.append_event(
        "briefing_delivered",
        source="telegram",
        briefing_id=briefing_id,
        telegram_message_id=message_id,
        delivered=message_id is not None,
    )

    if prediction is None:
        log.info("No valid prediction JSON in response, skipping day-file write")
        return 0

    valid, err = validate_prediction(prediction)
    if not valid:
        log.info(f"Prediction failed schema: {err}, skipping day-file write")
        return 0

    try:
        episodic.write_day(today, briefing_text, prediction)
    except FileExistsError:
        log.info(f"Day file for {today} already exists, not overwriting (events.jsonl has the full history)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
