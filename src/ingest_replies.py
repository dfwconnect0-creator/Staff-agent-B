"""
Ingest Telegram replies into episodic memory files.

Runs on its own cron (every 2 hours). Reads Telegram replies since last
ingestion, maps each to a day (based on Cairo date of message timestamp),
appends to that day's memory file.

State: .last_update_id is committed to git so the Actions workflow can
read it between runs. Trade-off: adds a small commit every 2 hours when
there are new messages. Acceptable for v1.
"""

import sys
from datetime import date, datetime
from pathlib import Path

import src.telegram_client as telegram
import src.memory.episodic as episodic


def _get_memory_dir() -> Path:
    return episodic.MEMORY_DIR


def read_last_update_id() -> int:
    p = _get_memory_dir() / ".last_update_id"
    if not p.exists():
        return 0
    try:
        return int(p.read_text().strip())
    except ValueError:
        return 0


def write_last_update_id(update_id: int) -> None:
    p = _get_memory_dir() / ".last_update_id"
    p.write_text(str(update_id))


def parse_cairo_date(timestamp_cairo: str) -> date:
    """Parse a Cairo timestamp string like '2026-04-17 09:12' into a date."""
    dt = datetime.strptime(timestamp_cairo, "%Y-%m-%d %H:%M")
    return dt.date()


def append_to_orphans(update: dict) -> None:
    p = _get_memory_dir() / ".orphans.md"
    line = f'- **{update["timestamp_cairo"]} Cairo:** "{update["text"]}"\n'
    existing = p.read_text() if p.exists() else "# Orphaned replies\n\nReplies that arrived for days without a briefing file.\n\n"
    if line not in existing:
        p.write_text(existing + line)


def main() -> int:
    last_id = read_last_update_id()
    updates = telegram.get_updates(since_update_id=last_id)
    for update in updates:
        day = parse_cairo_date(update["timestamp_cairo"])
        try:
            episodic.append_reply(day, update["timestamp_cairo"], update["text"])
        except FileNotFoundError:
            append_to_orphans(update)
    if updates:
        write_last_update_id(max(u["update_id"] for u in updates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
