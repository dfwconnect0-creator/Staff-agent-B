import json
import logging
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from src.llm.factory import get_provider
from src.telegram_client import send_telegram_message
import src.memory.episodic as episodic
from src.memory.schema import validate_prediction

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

CAIRO_TZ = timezone(timedelta(hours=3))
CONTEXT_DIR = Path(__file__).parent.parent / "context"


def load_context() -> dict:
    def read(name: str) -> str:
        p = CONTEXT_DIR / name
        return p.read_text(encoding="utf-8") if p.exists() else ""

    return {
        "soul": read("soul.md"),
        "user": read("user.md"),
        "heartbeat": read("heartbeat.md"),
    }


def build_prompt(context: dict, yesterday_memory: dict | None, today: date) -> tuple[str, str]:
    day_name = today.strftime("%A")
    date_str = today.isoformat()

    system_prompt = context["soul"]

    yesterday_section = ""
    if yesterday_memory and yesterday_memory.get("prediction"):
        yesterday_date = (today - timedelta(days=1)).isoformat()

        prediction_json = json.dumps(yesterday_memory["prediction"], indent=2, ensure_ascii=False)
        yesterday_section = f"""## Yesterday's context

Yesterday ({yesterday_date}) you predicted:

```json
{prediction_json}
```
"""
        if yesterday_memory.get("user_replies"):
            replies_text = "\n".join(
                f'- {r["timestamp"]} Cairo: "{r["text"]}"'
                for r in yesterday_memory["user_replies"]
            )
            yesterday_section += f"""
The user replied:
{replies_text}

Use this to calibrate today's briefing. If yesterday's prediction was wrong based on the replies, adjust. If it was right but still unresolved, flag it again more firmly.
"""

    user_message = f"""Today is {day_name}, {date_str} (Cairo time).

{yesterday_section}--- USER PROFILE (user.md) ---
{context['user']}

--- DAILY CHECKLIST (heartbeat.md) ---
{context['heartbeat']}

--- YOUR TASK ---

Give me a morning briefing:
🎯 What's stuck? One thing.
✂️ Smallest action to unblock it.
❓ Three sharp questions.
⚡ Energy check (1-5) as the last question.

No filler. No preamble. Just the briefing.

AFTER the briefing text, output a JSON block with this exact structure:

```json
{{
  "schema_version": 1,
  "stuck_item": "one-line name of the stuck item",
  "smallest_action": "the single smallest action to unblock it",
  "confidence": "low|medium|high",
  "flags_raised": ["flag1", "flag2"],
  "questions_asked": ["question 1", "question 2", "question 3"]
}}
```

The JSON must come last. The briefing text (above the JSON) is what I'll read on my phone.
"""
    return system_prompt, user_message


def main() -> int:
    context = load_context()
    today = datetime.now(CAIRO_TZ).date()
    yesterday = today - timedelta(days=1)

    try:
        yesterday_memory = episodic.read_day(yesterday)
    except Exception as e:
        log.warning(f"Failed to read yesterday's memory: {e}, continuing without context")
        yesterday_memory = None

    system_prompt, user_message = build_prompt(context, yesterday_memory, today)

    result = get_provider().complete(system_prompt, user_message)
    log.info(f"LLM response from provider={result['provider']} model={result['model']}")
    briefing_text = result["text"]
    prediction = result["prediction"]

    if not briefing_text.strip():
        send_telegram_message("⚠️ Empty briefing. Check logs.")
        return 1

    send_telegram_message(briefing_text)

    if prediction is None:
        log.info("No valid prediction JSON in response, skipping memory write")
        return 0

    valid, err = validate_prediction(prediction)
    if not valid:
        log.info(f"Prediction failed schema: {err}, skipping memory write")
        return 0

    try:
        episodic.write_day(today, briefing_text, prediction)
    except FileExistsError:
        log.info(f"File for {today} already exists, not overwriting")

    return 0


if __name__ == "__main__":
    sys.exit(main())
