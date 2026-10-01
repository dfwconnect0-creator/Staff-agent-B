"""``python -m src.project_update`` — the only sanctioned way to record project evidence.

One stdlib-only CLI with four verbs, chosen so an agent can never accidentally mutate
project state without recording where the change came from:

    python -m src.project_update refresh            # re-read every project's source
    python -m src.project_update refresh <project>  # just one
    python -m src.project_update report <project>   # print one project's state
    python -m src.project_update report --portfolio # print the portfolio decision
    python -m src.project_update portfolio          # rewrite the portfolio state file
    python -m src.project_update route "<reply>"    # show how a reply would route

Every state change appends a ``project_evidence`` event before the state file is
written, so project state is always reconstructible from the log. Nothing here calls
a model, which is what keeps Fast Mode silent.
"""

import argparse
import json
import sys

import src.events as events_log
import src.portfolio as portfolio
import src.project_routing as routing
import src.project_sources as sources
import src.project_state as project_state
import src.projects as registry

MAX_EVIDENCE_LINES = 20
MAX_TRANSITION_LOG = 20


def _log(message: str) -> None:
    print(message, file=sys.stderr)


def record_evidence(project_id: str, report: dict, catalog_entry) -> dict:
    """Append a project_evidence event and fold it into the project's state file.

    The event is written first on purpose: if the state write then fails, the log
    still shows the observation, and a later run can rebuild state from it.

    Re-running ``refresh`` over an unchanged source is a no-op. Without that guard a
    project nobody has touched would gain a duplicate "still the same" event on every
    refresh, and the log would stop being a record of anything.
    """
    previous = events_log.last_project_evidence(project_id)
    identical = bool(
        previous
        and previous.get("fresh") == report["fresh"]
        and previous.get("reason") == report["reason"]
        and previous.get("facts") == report["facts"]
        and previous.get("observed_at") == report.get("newest_evidence_at", "")
    )
    if identical:
        _log(f"{project_id}: unchanged since {previous['event_id']}, nothing recorded")
        return previous

    event = events_log.append_event(
        "project_evidence",
        source="agent",
        project_id=project_id,
        evidence_source=report["source"],
        evidence_path=report["path"],
        observed_at=report.get("newest_evidence_at", ""),
        fresh=report["fresh"],
        reason=report["reason"],
        facts=report["facts"],
    )

    state = project_state.load_state(project_id)
    state["name"] = catalog_entry.name
    state["evidence_source"] = catalog_entry.evidence_source
    state["source_freshness"] = sources.freshness(report.get("newest_evidence_at", "")) if report["fresh"] else "unavailable"
    state["last_evidence"] = f"{event['event_id']} - project_evidence ({event['timestamp']})"
    state["confidence"] = "high" if report["fresh"] else "low"
    if report["fresh"]:
        state["registered_at"] = state.get("registered_at") or catalog_entry.onboarded_at or event["timestamp"]
    state["updated_at"] = sources.now_cairo()

    summary = "; ".join(report["facts"]) if report["facts"] else report["reason"] or "no facts observed"
    line = f"- {event['event_id']} | {event['timestamp']} | {report['source']} | {summary[:300]}"
    state["evidence"] = (state.get("evidence", []) + [line])[-MAX_EVIDENCE_LINES:]

    project_state.write_state(project_id, state)
    return event


def refresh(project_ids: list[str]) -> int:
    catalog = registry.load_registry()
    problems = registry.validate_registry(catalog)
    if problems:
        for problem in problems:
            _log(f"registry problem: {problem}")
    if not catalog:
        _log("no projects registered; nothing to refresh")
        return 1

    targets = project_ids or [pid for pid in catalog if pid != "staff-agent"]
    unknown = [pid for pid in targets if pid not in catalog]
    if unknown:
        _log(f"not in the registry: {', '.join(unknown)}")

    for pid in targets:
        entry = catalog.get(pid)
        if entry is None:
            continue
        report = sources.inspect(pid, entry.evidence_source, entry.evidence_path)
        event = record_evidence(pid, report, entry)
        verdict = "fresh" if report["fresh"] else f"unavailable ({report['reason']})"
        _log(f"{pid}: {verdict} -> {event['event_id']}")

    update_portfolio()
    return 0


def update_portfolio() -> dict:
    states = portfolio.load_states()
    state = portfolio.build_state(states)
    portfolio.write_state(state)
    _log(f"portfolio: {state['required_action']}")
    return state


def _print_project(pid: str) -> None:
    state = project_state.load_state(pid)
    entry = registry.load_registry().get(pid)
    if entry is None:
        _log(f"{pid} is not in the registry")
        return
    print(f"project: {pid}")
    for key in project_state.FIELD_ORDER:
        print(f"  {key}: {state.get(key, 'unknown')}")
    print("  evidence:")
    for line in state.get("evidence", []) or ["_none_"]:
        print(f"    {line}")


def _print_portfolio() -> None:
    state = portfolio.load_state()
    if not state:
        _log("no portfolio state file yet; run `python -m src.project_update portfolio` first")
        return
    for key in portfolio.FIELD_ORDER:
        print(f"{key}: {state.get(key, 'unknown')}")


def _print_route(text: str) -> None:
    catalog = registry.load_registry()
    index = registry.alias_index(catalog)
    decision = routing.resolve_project(text, index)
    print(json.dumps(decision, indent=2, ensure_ascii=False))
    if decision["project_id"]:
        updates = routing.derive_field_updates(text, decision["project_id"], index)
        print(json.dumps(updates, indent=2, ensure_ascii=False))


ONBOARDABLE_FIELDS = (
    "target_output",
    "current_state",
    "required_transition",
    "blocker",
    "phase",
    "work_type",
    "artifact_state",
    "outcome_quality",
    "externalization",
    "accountability_source",
    "scope",
)


def record_routed_reply(project_id: str, reply_event: dict, decision: dict, updates: list[dict]) -> dict:
    """Apply a routed Telegram reply to one project's state, and event it.

    Lives here rather than in ``src/ingest_replies.py`` so there is exactly one writer
    of project state: ingestion decides *whether* a reply routes, this module decides
    what that routing means and records it.

    A field is only written when the reply actually changes it. A reply that changes
    nothing still gets a ``project_evidence`` event, so the routing decision stays
    auditable even when it was a no-op.
    """
    entry = registry.load_registry().get(project_id)
    stamp = sources.now_cairo()
    applied: list[str] = []

    if updates and entry is not None:
        state = project_state.load_state(project_id)
        written = set()
        for routed in updates:
            field, value = routed["field"], routed["value"]
            if field not in ONBOARDABLE_FIELDS or state.get(field) == value:
                continue
            state[field] = value
            written.add(field)
            applied.append(
                f"- {stamp} | {reply_event['event_id']} | {field} -> {value} | "
                f"matched '{routed['matched_alias']}' + signal '{routed['signal']}'"
            )
        if written:
            state["name"] = entry.name
            state["last_evidence"] = f"{reply_event['event_id']} - user_reply ({stamp})"
            state["confidence"] = "high"
            state["updated_at"] = stamp
            state["evidence"] = (state.get("evidence", []) + applied)[-MAX_EVIDENCE_LINES:]
            state["transitions"] = (state.get("transitions", []) + applied)[-MAX_TRANSITION_LOG:]
            project_state.write_state(project_id, state)

    event = events_log.append_event(
        "project_evidence",
        source="telegram",
        project_id=project_id,
        evidence_source="user_reply",
        evidence_path="none",
        observed_at=stamp,
        fresh=True,
        reason=decision["reason"],
        facts=[routed["quote"] for routed in updates] or [reply_event.get("text", "")[:200]],
        reply_event_id=reply_event["event_id"],
        applied=applied,
    )
    for line in applied:
        _log(f"{project_id}: {line}")
    return event


def onboard(project_id: str) -> int:
    """Record a project's judgement fields, read as JSON on stdin.

    Separate from ``refresh`` on purpose. ``refresh`` reports what is on disk and is
    safe to re-run any time; this records what the evidence *means*, which is a
    decision someone made and which therefore has to be an explicit, evented action
    rather than a side effect of reading a directory.

    Only the fields above are accepted, and every one of them must be a non-empty
    string. An unknown key is an error instead of a silent no-op, so a typo cannot
    quietly leave a project's state unwritten.
    """
    catalog = registry.load_registry()
    entry = catalog.get(project_id)
    if entry is None:
        _log(f"{project_id} is not in the registry; add it to context/portfolio/projects.md first")
        return 1

    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        _log(f"expected a JSON object on stdin: {exc}")
        return 1
    if not isinstance(payload, dict):
        _log("expected a JSON object on stdin")
        return 1

    unknown = [k for k in payload if k not in ONBOARDABLE_FIELDS]
    if unknown:
        _log(f"unknown field(s): {', '.join(sorted(unknown))}. Allowed: {', '.join(ONBOARDABLE_FIELDS)}")
        return 1
    blank = [k for k, v in payload.items() if not isinstance(v, str) or not v.strip()]
    if blank:
        _log(f"field(s) must be non-empty strings: {', '.join(sorted(blank))}")
        return 1

    state = project_state.load_state(project_id)
    changes = {}
    for key, value in payload.items():
        value = value.strip()
        if state.get(key) == value:
            continue
        changes[key] = {"from": state.get(key, "unknown"), "to": value}

    if not changes:
        _log(f"{project_id}: no change; state already matches")
        return 0

    stamp = sources.now_cairo()
    event = events_log.append_event(
        "project_evidence",
        source="agent",
        project_id=project_id,
        evidence_source="onboarding",
        evidence_path="none",
        observed_at=stamp,
        fresh=True,
        reason="onboarding judgement recorded",
        facts=[f"{k}: {v['to']}" for k, v in sorted(changes.items())],
        changes=changes,
    )

    state.update({k: v.strip() for k, v in payload.items()})
    state["name"] = entry.name
    state["last_evidence"] = f"{event['event_id']} - project_evidence ({stamp})"
    state["confidence"] = "high"
    state["registered_at"] = state.get("registered_at") or entry.onboarded_at or stamp
    state["updated_at"] = stamp
    lines = [
        f"- {stamp} | {event['event_id']} | onboarding | {k}: {v['from']} -> {v['to']}"
        for k, v in sorted(changes.items())
    ]
    state["evidence"] = (state.get("evidence", []) + lines)[-MAX_EVIDENCE_LINES:]
    state["transitions"] = (state.get("transitions", []) + lines)[-MAX_TRANSITION_LOG:]
    project_state.write_state(project_id, state)

    for key, value in sorted(changes.items()):
        _log(f"{project_id}: {key} {value['from']} -> {value['to']}")
    update_portfolio()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.project_update",
        description="Record evidence about registered projects and derive the portfolio state.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_refresh = sub.add_parser("refresh", help="re-read every registered project's source")
    p_refresh.add_argument("project", nargs="*", help="limit to these project ids")

    p_report = sub.add_parser("report", help="print state without changing anything")
    p_report.add_argument("project", nargs="?", help="project id; omit with --portfolio")
    p_report.add_argument("--portfolio", action="store_true", help="print the portfolio state instead")

    sub.add_parser("portfolio", help="rebuild context/portfolio/current_state.md")

    p_onboard = sub.add_parser(
        "onboard",
        help="record a project's judgement fields (target, phase, blocker) from a JSON object on stdin",
    )
    p_onboard.add_argument("project", help="project id")

    p_route = sub.add_parser("route", help="show how a reply would be routed, without applying it")
    p_route.add_argument("text", help="the reply text")

    args = parser.parse_args(argv)

    if args.command == "refresh":
        return refresh(args.project)
    if args.command == "report":
        if args.portfolio:
            _print_portfolio()
        elif args.project:
            _print_project(args.project)
        else:
            _log("give a project id, or use --portfolio")
            return 1
        return 0
    if args.command == "portfolio":
        update_portfolio()
        return 0
    if args.command == "onboard":
        return onboard(args.project)
    if args.command == "route":
        _print_route(args.text)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
