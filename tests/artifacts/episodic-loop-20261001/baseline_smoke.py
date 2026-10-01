"""BASELINE SMOKE TEST — no edits to the repo.

Traces the real src.briefing.main() and src.ingest_replies.main() code paths.
Only the two network boundaries (LLM provider + Telegram) are faked.
Memory dir is redirected to a temp dir so the repo is untouched.
"""
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, "/home/bladina/staff_agent_B")

import src.memory.episodic as episodic  # noqa: E402
import src.briefing as briefing  # noqa: E402
import src.ingest_replies as ingest  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="smoke-"))
episodic.MEMORY_DIR = tmp

captured = []

FAKE_PRED = {
    "schema_version": 1,
    "stuck_item": "Marketing-agent pipeline real-world test run",
    "smallest_action": "Define input and expected output for one test run.",
    "confidence": "high",
    "flags_raised": [],
    "questions_asked": ["What is the input?", "What is the verifiable output?", "Energy 1-5?"],
}

FAKE_RESP = {
    "text": "**Stuck:** Marketing-agent test run.\n\n**Action:** Define one test run.\n\nEnergy: 3",
    "prediction": FAKE_PRED,
    "raw": "text + json",
    "provider": "fake",
    "model": "fake-model",
}


class FakeProvider:
    def complete(self, system_prompt, user_message):
        captured.append(("PROMPT", user_message))
        return FAKE_RESP


def fake_send(text):
    captured.append(("SENT", text))


def main():
    print("=" * 70)
    print("STEP 1: run briefing for day 1 (real briefing.main())")
    print("=" * 70)
    day1 = date(2026, 10, 1)
    import freezegun
    with freezegun.freeze_time("2026-10-01 05:00:00"):
        with patch.object(briefing, "get_provider", return_value=FakeProvider()):
            with patch.object(briefing, "send_telegram_message", side_effect=fake_send):
                rc = briefing.main()
    print(f"  rc={rc}")
    print(f"  files: {sorted(p.name for p in tmp.glob('*.md'))}")

    print()
    print("STEP 2: ingest a reply for day 1 (real ingest.main())")
    print("=" * 70)
    fake_updates = [{
        "update_id": 9001, "chat_id": 12345,
        "timestamp_cairo": "2026-10-01 09:30",
        "text": "I tested it. Storage and retrieval work, but next briefing ignored it.",
    }]
    with patch.object(ingest.telegram, "get_updates", return_value=fake_updates):
        with patch.object(ingest, "read_last_update_id", return_value=0):
            with patch.object(ingest, "write_last_update_id", side_effect=lambda x: print(f"  cursor -> {x}")):
                ingest.main()
    day1_file = tmp / "2026-10-01.md"
    print(f"  day1 file has User replies section: {'## User replies' in day1_file.read_text()}")

    print()
    print("STEP 3: run briefing for day 2 — does the reply reach the prompt?")
    print("=" * 70)
    captured.clear()
    with freezegun.freeze_time("2026-10-02 05:00:00"):
        with patch.object(briefing, "get_provider", return_value=FakeProvider()):
            with patch.object(briefing, "send_telegram_message", side_effect=fake_send):
                rc = briefing.main()
    prompt = [c for c in captured if c[0] == "PROMPT"][0][1]
    print(f"  rc={rc}")
    print(f"  reply text present in prompt: {'Storage and retrieval work' in prompt}")
    print(f"  'Yesterday' section present : {'Yesterday' in prompt}")
    print()
    print("  --- prompt excerpt (yesterday section) ---")
    start = prompt.find("## Yesterday")
    print(prompt[start:start + 700] if start >= 0 else "  (NO YESTERDAY SECTION)")

    print()
    print("STEP 4: idempotency probe — replay the same Telegram update twice")
    print("=" * 70)
    before = day1_file.read_text()
    with patch.object(ingest.telegram, "get_updates", return_value=fake_updates):
        with patch.object(ingest, "read_last_update_id", return_value=0):
            with patch.object(ingest, "write_last_update_id"):
                ingest.main()
    print(f"  day1 file unchanged on replay: {before == day1_file.read_text()}")

    print()
    print("STEP 5: does briefing read current_state.md / any operational state?")
    print("=" * 70)
    print(f"  briefing.CONTEXT_DIR = {briefing.CONTEXT_DIR}")
    print(f"  context files loaded   = {sorted(briefing.load_context().keys())}")
    print(f"  current_state.md exists: {(briefing.CONTEXT_DIR / 'current_state.md').exists()}")
    print(f"  events.jsonl exists    : {(tmp / 'events.jsonl').exists()}")

    print()
    print(f"ARTIFACT DIR: {tmp}")


if __name__ == "__main__":
    try:
        main()
    finally:
        pass
