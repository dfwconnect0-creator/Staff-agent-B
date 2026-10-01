"""
Ingest Telegram replies into episodic memory files and the event log.

Runs on its own cron (every 10 minutes). Reads Telegram replies since last
ingestion, maps each to a day (based on Cairo date of message timestamp),
appends to that day's memory file, and appends a `user_reply` event to
`memory/episodic/events.jsonl` carrying the briefing it answered.

Fast interactive mode: when new replies were actually stored, this run also calls
the existing deterministic state-update step, so `context/current_state.md` is
refreshed within ~10 minutes instead of waiting for the next daily briefing.
The transition logic is not reimplemented here -- `src.state_update` is the only
place that decides a checkpoint changed.

This is a SILENT loop. It never calls the LLM and never sends a Telegram message;
user-facing briefings stay in `src/briefing.py` / `daily-briefing.yml`. A run with
no new replies touches nothing at all, so it produces no commit.

State: .last_update_id is committed to git so the Actions workflow can
read it between runs. Trade-off: adds a small commit per run *that found new
messages*, and nothing at all otherwise.
"""

import logging
import sys
from datetime import date, datetime
from pathlib import Path

import src.telegram_client as telegram
import src.memory.episodic as episodic
import src.events as events_log
import src.portfolio as portfolio
import src.project_routing as routing
import src.project_update as project_update
import src.projects as registry
import src.state_update as state_update_mod

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


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
    p.parent.mkdir(parents=True, exist_ok=True)
    line = f'- **{update["timestamp_cairo"]} Cairo:** "{update["text"]}"\n'
    existing = p.read_text() if p.exists() else "# Orphaned replies\n\nReplies that arrived for days without a briefing file.\n\n"
    if line not in existing:
        p.write_text(existing + line)


def ingest_update(update: dict) -> dict:
    """Ingest one Telegram update.

    Accepts exactly the dict shape `telegram.get_updates` returns, so the manual
    test trigger and the scheduled cron go through identical code.

    Writes the human-readable markdown AND the machine-readable event, in that
    order, so a reply is never in the log without being on disk in the day file.
    Returns the appended `user_reply` event.
    """
    day = parse_cairo_date(update["timestamp_cairo"])
    try:
        episodic.append_reply(day, update["timestamp_cairo"], update["text"])
    except FileNotFoundError:
        append_to_orphans(update)

    briefing_id = events_log.resolve_reply_briefing(update.get("reply_to_message_id"))
    event = events_log.append_event(
        "user_reply",
        source="telegram",
        processed=False,
        text=update["text"],
        briefing_id=briefing_id,
        telegram_update_id=update.get("update_id"),
        telegram_message_id=update.get("message_id"),
        reply_to_message_id=update.get("reply_to_message_id"),
        timestamp_cairo=update["timestamp_cairo"],
        cairo_day=day.isoformat(),
    )
    log.info(f"ingested {event['event_id']} -> {event.get('briefing_id')}")
    return event


def route_reply(update: dict, event: dict) -> dict | None:
    """Decide whether a reply is about exactly one project, and record that decision.

    Conservative by design. A reply that names no project, or more than one, is left
    as a user_reply only: it is still stored and still consumed, but it cannot change
    a project's state. Applying the routing is left to ``src.project_update``, which is
    the single writer of project state.

    Returns the appended project_evidence event when the reply routed, else None.
    """
    catalog = registry.load_registry()
    if not catalog:
        return None
    index = registry.alias_index(catalog)
    decision = routing.resolve_project(update.get("text", ""), index)
    if decision["project_id"] is None:
        log.info(f"reply not routed to a project: {decision['reason']}")
        return None

    project_id = decision["project_id"]
    updates = routing.derive_field_updates(update.get("text", ""), project_id, index)
    routed_event = project_update.record_routed_reply(project_id, event, decision, updates)
    if not routed_event.get("applied"):
        log.info(f"routed {event['event_id']} -> {project_id}, no field changed")
    return routed_event


def apply_project_routing(events: list[dict]) -> int:
    """Route already-ingested replies to projects, oldest first.

    Kept separate from ``ingest_update`` so the write order stays readable: the reply
    is stored as a user_reply first, then routing is derived from that stored event.
    """
    routed = 0
    for event in events:
        routing_event = route_reply({"text": event.get("text", "")}, event)
        if routing_event is None:
            continue
        routed += 1
        log.info(f"routed {event['event_id']} -> {routing_event['project_id']}")
    return routed


def apply_state_update() -> dict:
    """Run the deterministic state-update step and log what it changed.

    Deliberately delegates to ``src.state_update``: the alias matching, signal
    tables and provenance rules live there and are shared with the daily briefing.
    No LLM call, no Telegram send -- a state transition here is silent.
    """
    report = state_update_mod.update_state_from_events()
    for t in report["transitions"]:
        log.info(
            f"state change: {t['checkpoint']} {t['from']} -> {t['to']} "
            f"(evidence {t['evidence_event_id']}, signal '{t['signal']}')"
        )
    log.info(
        f"state_version={report['state_version']} "
        f"consumed={report['processed_event_ids']} "
        f"next_action={report['next_action']}"
    )
    return report


def main() -> int:
    last_id = read_last_update_id()
    updates = telegram.get_updates(since_update_id=last_id)
    if not updates:
        log.info("no new updates; nothing to ingest, no state change")
        return 0

    ingested = [ingest_update(update) for update in updates]
    write_last_update_id(max(u["update_id"] for u in updates))

    # Project routing runs after the replies are stored, so it can point at the event
    # id of the reply it acted on. A reply naming one project updates that project's
    # state; anything ambiguous is left alone.
    routed = apply_project_routing(ingested)

    # Strict gate: state is only re-derived when a reply was actually stored.
    # With zero new replies this returns above, so a silent check can never
    # rewrite current_state.md, append a state_update event, or force a commit.
    apply_state_update()

    if routed:
        states = portfolio.load_states()
        derived = portfolio.write_state(portfolio.build_state(states))
        log.info(f"portfolio rebuilt: {derived}")

    log.info(f"ingested {len(ingested)} new reply/replies, {routed} routed to a project")
    return 0


if __name__ == "__main__":
    sys.exit(main())
