"""Read and write per-project state: ``context/projects/<project_id>/current_state.md``.

This is Level 2 of the three-level model, and it is deliberately **not** a
one-file-fits-all summary. Each project gets its own Markdown state file so a
project's outcome, phase and next required transition are readable without
running anything.

The three levels are kept apart on purpose:

* ``context/current_state.md`` — is the loop working? (Staff Agent's own work)
* ``context/projects/<id>/current_state.md`` — what is true about *this* project
* ``context/portfolio/current_state.md`` — where should attention go across projects

Same format discipline as ``src/state.py``: line-oriented ``key: value`` fields,
regex-parsed, hand-editable, no dependencies. Fields the project layer adds on top
of the Version 1 set are namespaced so both readers can share one file safely.
"""

import hashlib
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

CAIRO_TZ = timezone(timedelta(hours=3))

PROJECTS_DIRNAME = "projects"
STATE_FILENAME = "current_state.md"
REPO_ROOT = Path(__file__).parent.parent
PROJECTS_DIR = REPO_ROOT / "context" / PROJECTS_DIRNAME

# The fields the project state must always carry. Order is the render order.
FIELD_ORDER = [
    "name",
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
    "last_evidence",
    "confidence",
    "evidence_source",
    "source_freshness",
    "registered_at",
    "updated_at",
]

# `unknown` is a first-class value, not a failure. The registry and the updater both
# rely on it to record "no evidence found" without inventing a state.
UNKNOWN = "unknown"

_FIELD_RE = re.compile(r"^([a-z_]+):[ \t]*(.*)$", re.MULTILINE)


def projects_dir() -> Path:
    return PROJECTS_DIR


def project_dir(project_id: str) -> Path:
    return PROJECTS_DIR / project_id


def state_path(project_id: str) -> Path:
    return project_dir(project_id) / STATE_FILENAME


def now_cairo() -> str:
    return datetime.now(CAIRO_TZ).isoformat(timespec="seconds")


def empty_state() -> dict:
    state = {key: UNKNOWN for key in FIELD_ORDER}
    state["name"] = UNKNOWN
    state["target_output"] = UNKNOWN
    state["current_state"] = UNKNOWN
    state["required_transition"] = UNKNOWN
    state["blocker"] = UNKNOWN
    state["last_evidence"] = "none"
    state["source_freshness"] = "never_refreshed"
    state["registered_at"] = ""
    state["updated_at"] = ""
    state["evidence"] = []
    state["transitions"] = []
    return state


def load_state(project_id: str) -> dict:
    p = state_path(project_id)
    state = empty_state()
    if not p.exists():
        return state
    content = p.read_text(encoding="utf-8")
    found = _FIELD_RE.findall(content)
    for key, value in found:
        if key in FIELD_ORDER:
            state[key] = value.strip()

    evidence = re.search(r"## Evidence\n\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if evidence:
        state["evidence"] = [
            line.strip() for line in evidence.group(1).splitlines() if line.strip() and line.strip() != "_none_"
        ]

    transitions = re.search(r"## Transitions\n\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if transitions:
        state["transitions"] = [
            line.strip()
            for line in transitions.group(1).splitlines()
            if line.strip() and line.strip() != "_none_"
        ]
    return state


def render_state(state: dict) -> str:
    lines = [
        f"# {state.get('name', UNKNOWN)}",
        "",
        "Per-project state. Written by `python -m src.project_update`; safe to edit by hand.",
        "Level 2 of 3: this is the project, not the Staff Agent loop, and not the portfolio.",
        "",
    ]
    for key in FIELD_ORDER:
        lines.append(f"{key}: {state.get(key, UNKNOWN)}")
    lines += ["", "## Evidence", ""]
    evidence = state.get("evidence", [])
    lines += evidence if evidence else ["_none_"]
    lines += ["", "## Transitions", ""]
    transitions = state.get("transitions", [])
    lines += transitions if transitions else ["_none_"]
    lines.append("")
    return "\n".join(lines)


def write_state(project_id: str, state: dict) -> Path:
    p = state_path(project_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_state(state), encoding="utf-8")
    return p


def state_version(state: dict) -> str:
    """Digest of the fields that should change a decision.

    Includes the required transition and the artifact state, so a project that moved
    without its summary changing still produces a new version — that is what makes a
    portfolio briefing idempotent instead of repetitive.
    """
    material = {
        "target_output": state.get("target_output"),
        "current_state": state.get("current_state"),
        "required_transition": state.get("required_transition"),
        "blocker": state.get("blocker"),
        "phase": state.get("phase"),
        "artifact_state": state.get("artifact_state"),
        "outcome_quality": state.get("outcome_quality"),
        "externalization": state.get("externalization"),
        "scope": state.get("scope"),
    }
    blob = "\n".join(f"{k}={v}" for k, v in sorted(material.items()))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def portfolio_version(states: dict[str, dict]) -> str:
    """Digest across all project states, so the portfolio changes only when a project does."""
    parts = [f"{pid}={state_version(state)}" for pid, state in sorted(states.items())]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]
