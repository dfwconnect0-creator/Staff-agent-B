import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from claude_client import ask_claude
from telegram_client import send_telegram_message

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


def build_prompt(context: dict, today: "date") -> tuple[str, str]:
    from datetime import date
    day_name = today.strftime("%A")
    date_str = today.strftime("%Y-%m-%d")

    system_prompt = context["soul"]

    user_message = f"""Today is {day_name}, {date_str} (Cairo time).

--- USER PROFILE (user.md) ---
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
"""
    return system_prompt, user_message


def main() -> int:
    context = load_context()
    today = datetime.now(CAIRO_TZ).date()

    system_prompt, user_message = build_prompt(context, today)
    briefing_text = ask_claude(system_prompt, user_message)

    if not briefing_text.strip():
        send_telegram_message("⚠️ Empty briefing. Check logs.")
        return 1

    send_telegram_message(briefing_text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
