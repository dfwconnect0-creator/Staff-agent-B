"""Append-only event log for the episodic loop.

One JSON object per line in ``memory/episodic/events.jsonl``. The log is the
authoritative store for the chain briefing -> reply -> state update -> briefing,
because the day files cannot hold more than one briefing per Cairo date and
cannot express "this event has been consumed".

Durability: committed to git by both workflows, so it survives process restart,
machine restart and separate scheduled invocations.

Append-only means a consumed event cannot be edited in place. A reply is written
once with ``processed: false``; consumption is recorded by appending a
``state_update`` event that lists the consumed event ids. ``read_events`` derives
the effective ``processed`` flag from those references.
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import src.memory.episodic as episodic

CAIRO_TZ = timezone(timedelta(hours=3))

EVENTS_FILENAME = "events.jsonl"
PROJECT_ID = "staff-agent"

EVENT_TYPES = (
    "briefing_sent",
    "briefing_delivered",
    "user_reply",
    "state_update",
    # Version 2 additions. Appending to this tuple is backward compatible: existing
    # lines in events.jsonl carry only the first four types and are never rewritten.
    "project_evidence",
    "project_state_update",
    "portfolio_update",
)

BRIEFING_SCOPES = ("staff-agent", "portfolio")

DEFAULT_BRIEFING_SCOPE = "staff-agent"


def events_path() -> Path:
    return episodic.MEMORY_DIR / EVENTS_FILENAME


def now_cairo() -> str:
    return datetime.now(CAIRO_TZ).isoformat(timespec="seconds")


def read_events() -> list[dict]:
    """Read every event, in write order.

    Malformed lines are skipped rather than raising: one truncated final line from
    an interrupted append must not make the whole log unreadable.
    """
    p = events_path()
    if not p.exists():
        return []
    events = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    _apply_processed_flags(events)
    return events


def append_event(event_type: str, **fields) -> dict:
    """Append one event and return it as written.

    ``project_id`` defaults to ``staff-agent`` so every Version 1 call site keeps its
    current behaviour. Version 2 passes a real ``project_id`` for project-scoped
    evidence, which is the only thing that changes here.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event_type '{event_type}'. Valid: {', '.join(EVENT_TYPES)}")
    events = read_events()
    event = {
        "timestamp": now_cairo(),
        "event_id": _next_event_id(events),
        "source": fields.pop("source", "agent"),
        "event_type": event_type,
        "project_id": fields.pop("project_id", PROJECT_ID),
    }
    event.update(fields)
    p = events_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return event


def _next_event_id(events: list[dict]) -> str:
    return f"evt_{len(events) + 1:06d}"


CONSUMING_EVENT_TYPES = ("state_update", "project_state_update", "portfolio_update")


def _apply_processed_flags(events: list[dict]) -> None:
    """Derive the effective `processed` flag from later consumption events.

    Any event that lists an id in its `consumes` marks that id consumed, whatever its
    own type. Version 1 had a single consumer (`state_update`); Version 2 adds
    project- and portfolio-scoped consumers, and a reply routed to a project must stay
    visible to the project layer until the project consumes it.
    """
    consumed: set[str] = set()
    for e in events:
        if e.get("event_type") in CONSUMING_EVENT_TYPES:
            consumed.update(e.get("consumes", []))
    for e in events:
        if e.get("event_type") == "user_reply":
            e["processed"] = e["event_id"] in consumed


def mark_processed(consumes: list[str], transitions: list[dict], state_version: str) -> dict:
    """Record that these events were consumed and produced these transitions."""
    return append_event(
        "state_update",
        source="agent",
        consumes=consumes,
        transitions=transitions,
        state_version=state_version,
    )


def unprocessed_replies() -> list[dict]:
    return [e for e in read_events() if e.get("event_type") == "user_reply" and not e.get("processed")]


def next_briefing_id(day) -> str:
    """briefing_YYYYMMDD_NNN, numbered from the briefings already logged today."""
    prefix = f"briefing_{day.strftime('%Y%m%d')}_"
    used = [
        int(e["briefing_id"][len(prefix):])
        for e in read_events()
        if e.get("event_type") == "briefing_sent" and str(e.get("briefing_id", "")).startswith(prefix)
    ]
    return f"{prefix}{max(used, default=0) + 1:03d}"


def briefings_for_day(day) -> list[dict]:
    prefix = f"briefing_{day.strftime('%Y%m%d')}_"
    return [
        e
        for e in read_events()
        if e.get("event_type") == "briefing_sent" and str(e.get("briefing_id", "")).startswith(prefix)
    ]


def last_delivered_briefing(day, scope: str = "staff-agent") -> dict | None:
    """The most recent briefing for this Cairo date that Telegram actually accepted.

    Scoped, so the staff-agent loop never mistakes a portfolio briefing for its own
    delivered message. Version 1 events carry no scope and count as ``staff-agent``.
    """
    delivered = {e["briefing_id"] for e in read_events() if e.get("event_type") == "briefing_delivered"}
    candidates = [
        e for e in briefings_for_day(day) if e.get("briefing_id") in delivered and briefing_scope(e) == scope
    ]
    return candidates[-1] if candidates else None


def resolve_reply_briefing(reply_to_message_id) -> str | None:
    """Resolve which briefing a reply answered, from direct Telegram provenance only.

    A message links to a briefing only when ``reply_to_message_id`` matches the
    ``telegram_message_id`` of a ``briefing_delivered`` event. Anything else — no
    ``reply_to_message_id`` at all, or a reply-to that was never a delivered briefing —
    resolves to None.

    Such a message is still stored as an event; it simply cannot be treated as evidence
    about a briefing. Falling back to "most recent briefing sent" made an ordinary
    message look like an answer to a briefing it never referenced (evt_000003).
    """
    if reply_to_message_id is None:
        return None
    for e in reversed(read_events()):
        if (
            e.get("event_type") == "briefing_delivered"
            and e.get("telegram_message_id") is not None
            and e["telegram_message_id"] == reply_to_message_id
        ):
            return e.get("briefing_id")
    return None


def has_reply_provenance(event: dict) -> bool:
    """True when this event is a direct Telegram reply to a briefing that was delivered.

    Checked against the event log rather than against the event's own ``briefing_id``,
    so older malformed events that were linked by a fallback are correctly rejected.
    """
    return resolve_reply_briefing(event.get("reply_to_message_id")) is not None


def briefing_scope(event: dict) -> str:
    """Which layer a briefing belongs to.

    Version 1 briefings carry no scope and default to ``staff-agent``, so every existing
    line in the log keeps its original meaning.
    """
    scope = event.get("briefing_scope")
    return scope if scope in BRIEFING_SCOPES else DEFAULT_BRIEFING_SCOPE


def resolve_reply_target(reply_to_message_id) -> tuple[str | None, str]:
    """Resolve a Telegram reply to ``(briefing_id, briefing_scope)``."""
    if reply_to_message_id is None:
        return None, DEFAULT_BRIEFING_SCOPE
    for e in reversed(read_events()):
        if (
            e.get("event_type") == "briefing_delivered"
            and e.get("telegram_message_id") is not None
            and e["telegram_message_id"] == reply_to_message_id
        ):
            scope = briefing_scope(e)
            if "briefing_scope" not in e:
                # The delivered event may predate scoping, or the scope may only have
                # been recorded on the matching send. Falling back to "staff-agent"
                # would silently reclassify a portfolio briefing, so look it up.
                scope = _scope_from_sent(e.get("briefing_id")) or scope
            return e.get("briefing_id"), scope
    return None, DEFAULT_BRIEFING_SCOPE


def _scope_from_sent(briefing_id) -> str | None:
    if not briefing_id:
        return None
    for e in reversed(read_events()):
        if e.get("event_type") == "briefing_sent" and e.get("briefing_id") == briefing_id:
            return e["briefing_scope"] if e.get("briefing_scope") in BRIEFING_SCOPES else None
    return None


# --- Version 2: project-scoped queries -------------------------------------------


def project_events(project_id: str, event_types: tuple[str, ...] | None = None) -> list[dict]:
    """Every event recorded for one project, in write order."""
    return [
        e
        for e in read_events()
        if e.get("project_id") == project_id
        and (event_types is None or e.get("event_type") in event_types)
    ]


def unconsumed_evidence(project_id: str) -> list[dict]:
    """Project evidence that no project_state_update has consumed yet."""
    consumed: set[str] = set()
    for e in read_events():
        if e.get("event_type") == "project_state_update":
            consumed.update(e.get("consumes", []))
    return [
        e
        for e in read_events()
        if e.get("project_id") == project_id
        and e.get("event_type") == "project_evidence"
        and e["event_id"] not in consumed
    ]


def last_project_evidence(project_id: str) -> dict | None:
    evidence = project_events(project_id, ("project_evidence",))
    return evidence[-1] if evidence else None
