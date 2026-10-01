"""Build the short portfolio briefing from project state. No model call.

The portfolio briefing is the one place where a model would be *tempting* and
*unnecessary*. The decision has already been made deterministically in
``src/portfolio.py``; all that is left is rendering it into something readable on a
phone. Rendering decided facts is exactly the job that should not go through a model,
because a model can only add ways for the briefing to disagree with the state it
claims to report.

So this module is pure formatting. The staff-agent's own Daily Briefing in
``src/briefing.py`` still uses the model, unchanged; this is a separate, short,
portfolio-wide message.
"""

import logging
import sys
from datetime import datetime, timezone, timedelta

import src.events as events_log
import src.portfolio as portfolio
import src.projects as registry
from src.telegram_client import send_telegram_message

log = logging.getLogger(__name__)

CAIRO_TZ = timezone(timedelta(hours=3))

MAX_PROJECTS_IN_BRIEFING = 5
MAX_TEXT_LEN = 200


def _clip(text: str, limit: int = MAX_TEXT_LEN) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def project_lines(states: dict[str, dict], catalog: dict[str, registry.Project]) -> list[str]:
    """One line per project: name, phase, and its next required transition."""
    lines = []
    for pid, state in states.items():
        entry = catalog.get(pid)
        name = state.get("name") or (entry.name if entry else pid)
        phase = state.get("phase", "unknown")
        transition = _clip(state.get("required_transition", "unknown"), 110)
        lines.append(f"- {name} [{phase}]: {transition}")
    return lines


def render(states: dict[str, dict], catalog: dict[str, registry.Project]) -> str:
    """The full portfolio briefing text.

    Structure is fixed and short: what the portfolio needs, one line per project, and
    an explicit note when the correct answer is silence. There is no question block,
    because a question per project would turn a five-project briefing into a form.
    """
    decision = portfolio.decide(states)
    state = portfolio.build_state(states)
    lines = [
        "Portfolio check-in",
        "",
        f"Needs attention: {state['required_action']}",
        "",
    ]
    lines += project_lines(states, catalog)[:MAX_PROJECTS_IN_BRIEFING]
    if len(states) > MAX_PROJECTS_IN_BRIEFING:
        lines.append(f"- (+{len(states) - MAX_PROJECTS_IN_BRIEFING} more tracked)")
    lines += [
        "",
        (
            "No intervention needed today."
            if decision["rule"] == "none"
            else 'Reply naming a project to update it, e.g. "topic finder now works".'
        ),
    ]
    return "\n".join(lines)


def send(states: dict[str, dict], catalog: dict[str, registry.Project], today) -> int:
    """Send the portfolio briefing once per changed portfolio, recording both events.

    Idempotent the same way the staff-agent briefing is: if a portfolio briefing with
    this exact ``portfolio_version`` was already delivered today, nothing is sent. That
    is what keeps a re-run of the daily workflow from messaging the human twice.
    """
    state = portfolio.build_state(states)
    version = state["portfolio_version"]

    previous = events_log.last_delivered_briefing(today, scope="portfolio")
    if previous and previous.get("state_version") == version:
        log.info(f"portfolio unchanged since {previous['briefing_id']}; skipping send")
        return 0

    text = render(states, catalog)
    briefing_id = events_log.next_briefing_id(today)
    events_log.append_event(
        "briefing_sent",
        source="agent",
        briefing_id=briefing_id,
        briefing_scope="portfolio",
        state_version=version,
        next_action=state["required_action"],
        text=text,
    )
    message_id = send_telegram_message(text)
    if not isinstance(message_id, int):
        message_id = None
    events_log.append_event(
        "briefing_delivered",
        source="telegram",
        briefing_id=briefing_id,
        briefing_scope="portfolio",
        telegram_message_id=message_id,
        delivered=message_id is not None,
    )
    log.info(f"portfolio briefing {briefing_id} delivered={message_id is not None}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    argv = sys.argv[1:] if argv is None else argv
    catalog = registry.load_registry()
    states = portfolio.load_states()
    if not states:
        log.warning("No project state files yet. Run: python -m src.project_update refresh")
        return 1

    if "--send" in argv:
        today = datetime.now(CAIRO_TZ).date()
        return send(states, catalog, today)

    print(render(states, catalog))
    return 0


if __name__ == "__main__":
    sys.exit(main())
