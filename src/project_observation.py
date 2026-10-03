"""Authoritative writer for local project observations.

Runs in GitHub Actions (serialized). Takes a bounded batch of observations
from the local watcher, validates them against the registry, records evidence,
updates Level 2 project state, rebuilds Level 3 portfolio state. No LLM, no
Telegram sends.
"""

import argparse
import json
import sys

import src.events as events_log
import src.portfolio as portfolio
import src.project_sources as sources
import src.project_state as project_state
import src.projects as registry

MAX_EVIDENCE_LINES = 20


def _log(msg: str):
    print(msg, file=sys.stderr)


def apply_observation(obs: dict, catalog) -> tuple[bool, str]:
    pid = obs.get("project_id")
    if not pid:
        return False, "missing project_id"
    entry = catalog.get(pid)
    if entry is None:
        return False, f"unknown project_id: {pid}"
    if entry.tracking != "active":
        return False, f"project not active: {pid}"

    # Re-construct report from observation for comparison
    facts = obs.get("facts", [])
    report = {
        "source": obs.get("source_type", entry.evidence_source),
        "path": entry.evidence_path,
        "fresh": obs.get("fresh", False),
        "reason": obs.get("reason", ""),
        "newest_evidence_at": obs.get("observed_at", ""),
        "facts": facts,
    }

    previous = events_log.last_project_evidence(pid)
    identical = bool(
        previous
        and previous.get("fresh") == report["fresh"]
        and previous.get("reason") == report["reason"]
        and previous.get("facts") == report["facts"]
    )
    if identical:
        return True, "unchanged (idempotent)"

    event = events_log.append_event(
        "project_evidence",
        source="local_watcher",
        project_id=pid,
        evidence_source=report["source"],
        evidence_path=report["path"],
        observed_at=report.get("newest_evidence_at", ""),
        fresh=report["fresh"],
        reason=report["reason"],
        facts=report["facts"],
    )

    state = project_state.load_state(pid)
    state["name"] = entry.name
    state["evidence_source"] = entry.evidence_source
    state["source_freshness"] = sources.freshness(report.get("newest_evidence_at", "")) if report["fresh"] else "unavailable"
    state["last_evidence"] = f"{event['event_id']} - project_evidence ({event['timestamp']})"
    state["confidence"] = "high" if report["fresh"] else "low"
    if report["fresh"]:
        state["registered_at"] = state.get("registered_at") or entry.onboarded_at or event["timestamp"]
    state["updated_at"] = sources.now_cairo()

    summary = "; ".join(report["facts"]) if report["facts"] else report["reason"] or "no facts observed"
    line = f"- {event['event_id']} | {event['timestamp']} | {report['source']} | {summary[:300]}"
    state["evidence"] = (state.get("evidence", []) + [line])[-MAX_EVIDENCE_LINES:]
    project_state.write_state(pid, state)
    return True, f"updated -> {event['event_id']}"


def apply_payload(payload: dict) -> int:
    try:
        catalog = registry.load_registry()
        problems = registry.validate_registry(catalog)
        if problems:
            for p in problems:
                _log(f"registry problem: {p}")
        observations = payload.get("observations", []) if isinstance(payload, dict) else []
        if not observations:
            _log("no observations in payload")
            return 0
        changed_any = False
        for obs in observations:
            ok, msg = apply_observation(obs, catalog)
            pid = obs.get("project_id", "?")
            _log(f"{pid}: {msg}")
            if ok and "unchanged" not in msg:
                changed_any = True
        if changed_any:
            portfolio.load_states()
            s = portfolio.build_state(portfolio.load_states())
            portfolio.write_state(s)
            _log(f"portfolio: {s['required_action']}")
        else:
            _log("no meaningful changes")
        return 0
    except Exception as e:
        _log(f"error applying payload: {e}")
        return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["apply"])
    parser.add_argument("--payload", required=False)
    parser.add_argument("--payload-file", required=False)
    args = parser.parse_args()

    if args.command == "apply":
        payload = None
        if args.payload:
            try:
                payload = json.loads(args.payload)
            except Exception as e:
                _log(f"failed to parse payload: {e}")
                return 1
        elif args.payload_file:
            try:
                payload = json.loads(Path(args.payload_file).read_text())
            except Exception as e:
                _log(f"failed to parse payload file: {e}")
                return 1
        else:
            _log("missing --payload or --payload-file")
            return 1
        return apply_payload(payload)
    return 0


if __name__ == "__main__":
    from pathlib import Path
    sys.exit(main())
