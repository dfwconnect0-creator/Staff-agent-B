"""Read and write ``context/current_state.md`` — the agent's operational state.

This file is the only place changing project state lives. ``user.md`` holds stable
facts and preferences; ``heartbeat.md`` holds intervention policy; ``current_state.md``
holds what is true right now and what has to happen next.

The format is deliberately line-oriented and regex-parsed, like the day files in
``src/memory/episodic.py``. A human can edit it by hand and the state-update step
will not revert those edits: it only applies transitions that new evidence supports.

Layout:

    target_output: Prove the episodic-memory feedback loop.
    next_action: Verify reply ingestion end to end.
    blocker: Reply ingestion has never been run against a live Telegram chat.
    accountability_source: telegram
    last_evidence: evt_000004 — user_reply (2026-10-01T09:30:00+03:00)
    confidence: medium
    updated_at: 2026-10-01T08:00:00+03:00

    ## Checkpoints

    - reply_ingestion = not_verified | aliases: reply ingestion
    - storage = not_verified | aliases: stored
"""

import hashlib
import re
from pathlib import Path

CAIRO_TZ_OFFSET = "+03:00"

FIELD_ORDER = [
    "target_output",
    "next_action",
    "blocker",
    "accountability_source",
    "last_evidence",
    "confidence",
    "updated_at",
]

CHECKPOINT_VALUES = ("verified", "not_verified", "unknown")

STATE_FILENAME = "current_state.md"
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_STATE_PATH = PROJECT_ROOT / "context" / STATE_FILENAME

_FIELD_RE = re.compile(r"^([a-z_]+):[ \t]*(.*)$", re.MULTILINE)
_CHECKPOINT_RE = re.compile(r"^-[ \t]*([a-z0-9_]+)[ \t]*=[ \t]*([a-z_]+)[ \t]*(?:\|[ \t]*aliases:[ \t]*(.*))?$", re.MULTILINE)


def empty_state() -> dict:
    return {
        "target_output": "Unknown",
        "next_action": "Unknown",
        "blocker": "Unknown",
        "accountability_source": "unknown",
        "last_evidence": "none",
        "confidence": "unknown",
        "updated_at": "",
        "checkpoints": {},
        "aliases": {},
        "transitions": [],
    }


def load_state(path: Path | None = None) -> dict:
    p = path or DEFAULT_STATE_PATH
    if not p.exists():
        return empty_state()
    content = p.read_text(encoding="utf-8")
    state = empty_state()
    for key, value in _FIELD_RE.findall(content):
        if key in FIELD_ORDER:
            state[key] = value.strip()
    for name, value, aliases in _CHECKPOINT_RE.findall(content):
        state["checkpoints"][name] = value
        state["aliases"][name] = [a.strip() for a in aliases.split(",") if a.strip()] if aliases else []
    transitions = re.search(r"## Transitions\n\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if transitions:
        state["transitions"] = [
            line.strip()
            for line in transitions.group(1).splitlines()
            if line.strip() and line.strip() != "_none_"
        ]
    return state


def render_state(state: dict) -> str:
    checkpoints = state.get("checkpoints", {})
    aliases = state.get("aliases", {})
    lines = [
        "# current_state.md",
        "",
        "Operational state for the staff-agent. Changing state lives here, not in `user.md`.",
        "Rewritten by the state-update step in `src/briefing.py`; safe to edit by hand.",
        "",
    ]
    for key in FIELD_ORDER:
        lines.append(f"{key}: {state.get(key, 'Unknown')}")
    lines += ["", "## Checkpoints", ""]
    if checkpoints:
        for name, value in checkpoints.items():
            alias_text = f" | aliases: {', '.join(aliases.get(name, []))}" if aliases.get(name) else ""
            lines.append(f"- {name} = {value}{alias_text}")
    else:
        lines.append("_none_")
    lines += ["", "## Transitions", ""]
    transitions = state.get("transitions", [])
    lines += transitions if transitions else ["_none_"]
    lines.append("")
    return "\n".join(lines)


def write_state(state: dict, path: Path | None = None) -> Path:
    p = path or DEFAULT_STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_state(state), encoding="utf-8")
    return p


def state_version(state: dict) -> str:
    """Stable digest of the decision-relevant fields.

    Two states with the same version produce the same briefing recommendation, which
    is what makes a no-op re-run detectable and therefore idempotent.
    """
    material = {
        "target_output": state.get("target_output"),
        "next_action": state.get("next_action"),
        "blocker": state.get("blocker"),
        "checkpoints": state.get("checkpoints", {}),
    }
    blob = "\n".join(f"{k}={v}" for k, v in sorted(material.items()) if k != "checkpoints")
    blob += "\n" + json_like(material["checkpoints"])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def json_like(value) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(f"{k}:{v}" for k, v in value.items()) + "}"
    return str(value)


def next_action_from_state(state: dict) -> str:
    """The single next action, derived from the first unverified checkpoint.

    Returns an explicit "no intervention needed" marker when every checkpoint is
    verified. Silence is a valid outcome, so this never invents work.
    """
    unverified = [name for name, value in state.get("checkpoints", {}).items() if value != "verified"]
    if not state.get("checkpoints"):
        return "Unknown"
    if not unverified:
        return "No evidence-backed intervention needed."
    return f"Verify {unverified[0]}."
