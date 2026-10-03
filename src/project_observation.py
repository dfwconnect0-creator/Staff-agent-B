"""Authoritative writer for local project observations.

Runs in GitHub Actions (serialized). Takes a bounded batch of observations from the local
watcher, validates **the whole batch**, records evidence, updates Level 2 project state
and rebuilds Level 3 portfolio state. No LLM, no Telegram sends.

Two rules make this the authority rather than a second opinion:

* **Validation is all-or-nothing and happens first.** Every observation is checked
  against the rules in :mod:`src.observation_contract` and against the registry before a
  single event, state file or portfolio file is touched. A batch whose second item is
  malformed leaves nothing behind from its first item — a half-applied batch is a state
  the log cannot explain.

* **Replay protection is historical, not adjacent.** An observation is skipped when its
  identity has ever been applied for that project, not merely when it matches the newest
  evidence event.
"""

import argparse
import json
import os
import sys

import src.events as events_log
import src.portfolio as portfolio
import src.project_sources as sources
import src.project_state as project_state
import src.projects as registry
from src.observation_contract import (
    ALLOWED_SOURCE_TYPES,
    MAX_FACTS,
    MAX_FACT_LENGTH,
    MAX_OBSERVATIONS,
    MAX_PROJECT_ID_LENGTH,
    MAX_REASON_LENGTH,
    MAX_SOURCE_FRESHNESS_LENGTH,
    MAX_SUMMARY_LENGTH,
    OBSERVATION_DIGEST_RE,
    REQUIRED_OBSERVATION_FIELDS,
    SCHEMA_VERSION,
    TOP_LEVEL_FIELDS,
    is_timestamp,
    is_valid_correlation_id,
    is_valid_observation_digest,
    observation_identity,
    optional_timestamp,
)
from src.observation_contract import OBSERVATION_FIELDS

MAX_EVIDENCE_LINES = 20


def _log(msg: str):
    print(msg, file=sys.stderr)


# --- validation ----------------------------------------------------------------


def validate_payload(payload, catalog) -> list[str]:
    """Every rule that must hold before a single byte is written.

    Returns a list of human-readable problems. An empty list is the *only* way a payload
    may be applied. Validation of the entire batch completes first, so a malformed
    second observation can never leave the first one already on disk.
    """
    if not isinstance(payload, dict):
        return [f"payload: expected a JSON object, got {type(payload).__name__}"]

    problems: list[str] = []
    for key in sorted(set(payload) - set(TOP_LEVEL_FIELDS)):
        problems.append(f"payload: unexpected field '{key}'")

    if "schema_version" not in payload:
        problems.append("payload.schema_version: required")
    else:
        version = payload["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            problems.append(f"payload.schema_version: expected the integer {SCHEMA_VERSION}, got {version!r}")
        elif version != SCHEMA_VERSION:
            problems.append(f"payload.schema_version: expected {SCHEMA_VERSION}, got {version}")

    if "correlation_id" not in payload:
        problems.append("payload.correlation_id: required")
    elif not is_valid_correlation_id(payload["correlation_id"]):
        problems.append(
            "payload.correlation_id: expected 12-64 lowercase hex characters, got "
            f"{payload['correlation_id']!r}"
        )

    if "observations" not in payload:
        problems.append("payload.observations: required")
        return problems
    observations = payload["observations"]
    if not isinstance(observations, list):
        problems.append(f"payload.observations: expected a list, got {type(observations).__name__}")
        return problems
    if len(observations) < 1:
        problems.append("payload.observations: at least 1 observation is required")
        return problems
    if len(observations) > MAX_OBSERVATIONS:
        problems.append(
            f"payload.observations: {len(observations)} exceeds the maximum of {MAX_OBSERVATIONS}"
        )
        return problems

    for index, obs in enumerate(observations):
        problems.extend(_validate_observation(index, obs, catalog))
    return problems


def _validate_observation(index: int, obs, catalog) -> list[str]:
    where = f"observations[{index}]"
    if not isinstance(obs, dict):
        return [f"{where}: expected an object, got {type(obs).__name__}"]

    problems: list[str] = []
    for key in sorted(set(obs) - set(OBSERVATION_FIELDS)):
        problems.append(f"{where}: unexpected field '{key}'")
    for field in REQUIRED_OBSERVATION_FIELDS:
        if field not in obs:
            problems.append(f"{where}.{field}: required")

    pid = obs.get("project_id")
    if not isinstance(pid, str) or not pid.strip():
        problems.append(f"{where}.project_id: expected a non-empty string, got {pid!r}")
    elif len(pid) > MAX_PROJECT_ID_LENGTH:
        problems.append(f"{where}.project_id: longer than {MAX_PROJECT_ID_LENGTH} characters")
    else:
        entry = catalog.get(pid)
        if entry is None:
            problems.append(f"{where}.project_id: unknown project '{pid}'")
        elif entry.tracking != "active":
            problems.append(f"{where}.project_id: project '{pid}' is {entry.tracking}, not active")

    source_type = obs.get("source_type")
    allowed = ", ".join(ALLOWED_SOURCE_TYPES)
    if not isinstance(source_type, str) or source_type not in ALLOWED_SOURCE_TYPES:
        problems.append(f"{where}.source_type: expected one of {allowed}, got {source_type!r}")
    elif isinstance(pid, str) and pid in catalog and catalog[pid].evidence_source != source_type:
        problems.append(
            f"{where}.source_type: registry declares "
            f"{catalog[pid].evidence_source!r} for '{pid}', payload claims {source_type!r}"
        )

    fresh = obs.get("fresh")
    if not isinstance(fresh, bool):
        # "false", "true", 0 and 1 are all truthy or falsy in ways that invert or invent
        # the evidence. Only a real JSON boolean states whether the source was readable.
        problems.append(f"{where}.fresh: expected a real boolean, got {fresh!r}")

    facts = obs.get("facts")
    if not isinstance(facts, list):
        problems.append(f"{where}.facts: expected a list of strings, got {type(facts).__name__}")
    elif len(facts) > MAX_FACTS:
        problems.append(f"{where}.facts: {len(facts)} exceeds the maximum of {MAX_FACTS}")
    else:
        for i, fact in enumerate(facts):
            if not isinstance(fact, str):
                problems.append(f"{where}.facts[{i}]: expected a string, got {type(fact).__name__}")
            elif len(fact) > MAX_FACT_LENGTH:
                problems.append(f"{where}.facts[{i}]: longer than {MAX_FACT_LENGTH} characters")

    reason = obs.get("reason")
    if reason is not None and not isinstance(reason, str):
        problems.append(f"{where}.reason: expected a string or null, got {type(reason).__name__}")
    elif isinstance(reason, str) and len(reason) > MAX_REASON_LENGTH:
        problems.append(f"{where}.reason: longer than {MAX_REASON_LENGTH} characters")

    summary = obs.get("evidence_summary")
    if not isinstance(summary, str):
        problems.append(f"{where}.evidence_summary: expected a string, got {type(summary).__name__}")
    elif len(summary) > MAX_SUMMARY_LENGTH:
        problems.append(f"{where}.evidence_summary: longer than {MAX_SUMMARY_LENGTH} characters")

    if "observed_at" in obs and not is_timestamp(obs.get("observed_at")):
        problems.append(
            f"{where}.observed_at: expected a timezone-aware ISO timestamp, got {obs.get('observed_at')!r}"
        )

    if "newest_evidence_at" in obs and not optional_timestamp(obs.get("newest_evidence_at")):
        problems.append(
            f"{where}.newest_evidence_at: expected a timezone-aware ISO timestamp or null, "
            f"got {obs.get('newest_evidence_at')!r}"
        )

    source_freshness = obs.get("source_freshness")
    if source_freshness is not None and not isinstance(source_freshness, str):
        problems.append(
            f"{where}.source_freshness: expected a string or null, got {type(source_freshness).__name__}"
        )
    elif isinstance(source_freshness, str) and len(source_freshness) > MAX_SOURCE_FRESHNESS_LENGTH:
        problems.append(f"{where}.source_freshness: longer than {MAX_SOURCE_FRESHNESS_LENGTH} characters")

    digest = obs.get("observation_digest")
    if not is_valid_observation_digest(digest):
        problems.append(
            f"{where}.observation_digest: expected {OBSERVATION_DIGEST_RE.pattern[2:-2]} lowercase hex "
            f"characters, got {digest!r}"
        )
    elif observation_identity(obs) != digest:
        # The digest is the replay key. If it can disagree with the evidence it describes,
        # replay protection keys on whatever the sender felt like writing.
        problems.append(
            f"{where}.observation_digest: {digest} does not describe this observation "
            f"(evidence hashes to {observation_identity(obs)})"
        )

    return problems


# --- application ---------------------------------------------------------------


def apply_observation(obs: dict, catalog) -> tuple[bool, str]:
    """Record one already-validated observation. Returns (applied, message)."""
    pid = obs["project_id"]
    entry = catalog[pid]
    digest = obs["observation_digest"]

    if events_log.has_applied_observation(pid, digest):
        return False, f"replay of already-applied observation {digest}; nothing written"

    facts = list(obs["facts"])
    reason = obs["reason"] or ""
    evidence_at = obs["newest_evidence_at"] or ""
    report = {
        "source": obs["source_type"],
        "path": entry.evidence_path,
        "fresh": obs["fresh"],
        "reason": reason,
        "newest_evidence_at": evidence_at,
        "facts": facts,
    }

    event = events_log.append_event(
        "project_evidence",
        source="local_watcher",
        project_id=pid,
        evidence_source=report["source"],
        evidence_path=report["path"],
        observed_at=obs["observed_at"],
        evidence_at=evidence_at,
        observation_digest=digest,
        fresh=report["fresh"],
        reason=report["reason"],
        facts=report["facts"],
    )

    state = project_state.load_state(pid)
    state["name"] = entry.name
    state["evidence_source"] = entry.evidence_source
    state["source_freshness"] = sources.freshness(evidence_at) if report["fresh"] else "unavailable"
    state["last_evidence"] = f"{event['event_id']} - project_evidence ({event['timestamp']})"
    state["confidence"] = "high" if report["fresh"] else "low"
    if report["fresh"]:
        state["registered_at"] = state.get("registered_at") or entry.onboarded_at or event["timestamp"]
    state["updated_at"] = sources.now_cairo()

    summary = "; ".join(report["facts"]) if report["facts"] else report["reason"] or "no facts observed"
    line = f"- {event['event_id']} | {event['timestamp']} | {report['source']} | {summary[:300]}"
    state["evidence"] = (state.get("evidence", []) + [line])[-MAX_EVIDENCE_LINES:]
    project_state.write_state(pid, state)
    return True, f"updated -> {event['event_id']} (identity {digest})"


def apply_payload(payload) -> int:
    """Validate the entire batch, then apply it. Returns a process exit code.

    A non-zero return means the workflow step fails, so nothing this module would have
    written is committed. An unknown project is a rejection of the whole batch, not a
    log line followed by a success exit.
    """
    try:
        catalog = registry.load_registry()
        for problem in registry.validate_registry(catalog):
            _log(f"registry problem: {problem}")

        problems = validate_payload(payload, catalog)
        if problems:
            _log(f"payload rejected: {len(problems)} problem(s); nothing was written")
            for problem in problems:
                _log(f"  - {problem}")
            return 1

        applied = 0
        for obs in payload["observations"]:
            ok, message = apply_observation(obs, catalog)
            _log(f"{obs['project_id']}: {message}")
            if ok:
                applied += 1

        if applied:
            portfolio_state = portfolio.build_state(portfolio.load_states())
            portfolio.write_state(portfolio_state)
            _log(f"portfolio: {portfolio_state['required_action']}")
        else:
            _log("no meaningful changes")
        _log(f"correlation_id: {payload['correlation_id']}")
        return 0
    except Exception as exc:
        _log(f"error applying payload: {exc}")
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["apply"])
    args = parser.parse_args()

    if args.command != "apply":
        return 0

    raw = os.environ.get("OBSERVATION_PAYLOAD") or os.environ.get("PAYLOAD")
    if not raw:
        _log("missing payload")
        return 1
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        _log(f"failed to parse payload from env: {exc}")
        return 1
    return apply_payload(payload)


if __name__ == "__main__":
    sys.exit(main())
