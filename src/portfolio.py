"""Level 3: derive the portfolio state and decide whether to intervene at all.

Reads every registered project's state file and answers one question: **where should
the human's attention go today, and is saying so justified?**

The rules are deterministic tables, not model calls, and they are ordered by how much
they should override each other. A blocked project outranks a stale one; a project with
no evidence at all is never escalated, because "I have not looked" is not a finding
about the user's work.

Silence is a first-class outcome. If nothing matches a rule, ``required_action`` is an
explicit "no intervention needed" marker and the portfolio briefing says so.
"""

import re
from datetime import datetime, timezone, timedelta
from pathlib import Path

import src.project_state as project_state
import src.projects as registry

CAIRO_TZ = timezone(timedelta(hours=3))
PORTFOLIO_DIR = project_state.REPO_ROOT / "context" / "portfolio"
PORTFOLIO_STATE_PATH = PORTFOLIO_DIR / "current_state.md"

NO_INTERVENTION = "No evidence-backed intervention needed."

FIELD_ORDER = [
    "headline",
    "required_action",
    "attention_project",
    "attention_reason",
    "silence_is_correct",
    "project_count",
    "blocked_count",
    "stale_count",
    "portfolio_version",
    "updated_at",
]

_FIELD_RE = re.compile(r"^([a-z_]+):[ \t]*(.*)$", re.MULTILINE)


def now_cairo() -> str:
    return datetime.now(CAIRO_TZ).isoformat(timespec="seconds")


def load_states(project_ids: list[str] | None = None) -> dict[str, dict]:
    """Load every project's state file, skipping ids that are not registered."""
    catalog = registry.load_registry()
    ids = project_ids if project_ids is not None else list(catalog)
    states = {}
    for pid in ids:
        if pid not in catalog:
            continue
        if project_state.state_path(pid).exists():
            states[pid] = project_state.load_state(pid)
    return states


# --- intervention rules ---------------------------------------------------------
# Each rule is (name, applies_to, rationale). The first match wins, so ordering is
# the priority: a real blocker beats stale evidence, and stale evidence beats nothing
# at all.


def _is_blocked(state: dict) -> bool:
    """A hard stop, taken from the project's own declared phase or scope.

    Deliberately *not* inferred from the `blocker` field being non-empty. Every real
    project has some constraint recorded; treating any constraint as a block made all
    four onboarded projects read as blocked and flattened the portfolio to a single
    undifferentiated "everything is blocked", which tells the human nothing.
    """
    return state.get("scope") == "blocked" or state.get("phase") == "blocked"


def _is_stale(state: dict) -> bool:
    return state.get("source_freshness") in ("stale", "very_stale", "never_refreshed")


def _has_artifact(state: dict) -> bool:
    return state.get("artifact_state", "unknown").lower() not in ("unknown", "none", "")


def _rule_blocked(state: dict) -> dict | None:
    """A hard stop: the project's own phase or scope says it cannot move."""
    if state.get("scope") != "blocked" and state.get("phase") != "blocked":
        return None
    return {
        "rule": "blocked_project_has_a_named_required_transition",
        "required_action": f"Unblock {state.get('name', 'the project')}: {state.get('required_transition', 'unknown')}",
        "reason": f"blocked with: {state.get('blocker', 'unknown')}",
    }


def _rule_unexternalized(state: dict) -> dict | None:
    if state.get("externalization", "unknown").lower() != "internal_only":
        return None
    if not _has_artifact(state):
        return None
    return {
        "rule": "finished_artifact_never_left_the_machine",
        "required_action": f"Decide where {state.get('name', 'the artifact')} goes next",
        "reason": "artifact exists but has not been externalized",
    }


def _rule_unvalidated(state: dict) -> dict | None:
    if state.get("outcome_quality", "unknown").lower() != "unvalidated":
        return None
    if not _has_artifact(state):
        return None
    return {
        "rule": "artifact_exists_with_no_outcome_quality",
        "required_action": f"Validate the {state.get('name', 'artifact')} against its target",
        "reason": "artifact produced but outcome quality unrecorded",
    }


def _rule_stale(state: dict) -> dict | None:
    if not _is_stale(state):
        return None
    if state.get("scope") != "active":
        return None
    return {
        "rule": "active_project_evidence_is_stale",
        "required_action": f"Refresh evidence for {state.get('name', 'the project')}",
        "reason": f"newest evidence is {state.get('source_freshness', 'unknown')}",
    }


def _rule_undefined_target(state: dict) -> dict | None:
    if state.get("target_output", "unknown").lower() not in ("unknown", "none", ""):
        return None
    if state.get("scope") != "active":
        # A paused or candidate project is not supposed to be pressing for a decision.
        # Escalating it for having no target would be nagging about something the user
        # already set aside.
        return None
    return {
        "rule": "project_has_no_stated_target",
        "required_action": f"State the target output for {state.get('name', 'the project')}",
        "reason": "no target output recorded, so no transition can be derived",
    }


def _staleness_rank(state: dict) -> int:
    """Higher is staler, so sorting ascending surfaces the stalest first."""
    return {"very_stale": 4, "stale": 3, "never_refreshed": 2, "current": 0, "unavailable": 1}.get(
        state.get("source_freshness", "unknown"), 0
    )


RULES = (
    _rule_blocked,
    _rule_unexternalized,
    _rule_unvalidated,
    _rule_stale,
    _rule_undefined_target,
)


def decide(states: dict[str, dict]) -> dict:
    """The single portfolio-level decision, from all project states.

    Blockers win first, then the unexternalized/unvalidated artifact problems, then
    staleness. Ties break on the oldest evidence, because a project nobody has looked
    at in longer is the one most likely to be quietly wrong.
    """
    candidates = []
    for pid, state in states.items():
        for rule in RULES:
            hit = rule(state)
            if hit:
                candidates.append({"project_id": pid, **hit})
                break

    if not candidates:
        return {
            "required_action": NO_INTERVENTION,
            "attention_project": "none",
            "attention_reason": "no project matched an intervention rule",
            "rule": "none",
            "candidates": [],
        }

    priority = {
        "blocked_project_has_a_named_required_transition": 0,
        "finished_artifact_never_left_the_machine": 1,
        "artifact_exists_with_no_outcome_quality": 2,
        "project_has_no_stated_target": 3,
        "active_project_evidence_is_stale": 4,
    }
    # Ties break on stalest-first, then id, so two equally-urgent projects resolve the
    # same way every run instead of depending on directory ordering.
    states_by_id = states
    candidates.sort(
        key=lambda c: (
            priority.get(c["rule"], 9),
            _staleness_rank(states_by_id.get(c["project_id"], {})),
            c["project_id"],
        )
    )
    winner = candidates[0]
    return {
        "required_action": winner["required_action"],
        "attention_project": winner["project_id"],
        "attention_reason": winner["reason"],
        "rule": winner["rule"],
        "candidates": candidates,
    }


def build_state(states: dict[str, dict]) -> dict:
    decision = decide(states)
    active = [pid for pid, s in states.items() if s.get("scope") == "active"]
    return {
        "headline": f"{len(states)} project(s) tracked, {len(active)} active",
        "required_action": decision["required_action"],
        "attention_project": decision["attention_project"],
        "attention_reason": decision["attention_reason"],
        "silence_is_correct": "yes" if decision["rule"] == "none" else "no",
        "project_count": str(len(states)),
        "blocked_count": str(sum(1 for s in states.values() if _is_blocked(s))),
        "stale_count": str(sum(1 for s in states.values() if _is_stale(s))),
        "portfolio_version": project_state.portfolio_version(states),
        "updated_at": now_cairo(),
    }


def render_state(state: dict) -> str:
    lines = [
        "# portfolio current_state.md",
        "",
        "Level 3 of 3: which project needs attention across all projects.",
        "Derived by `python -m src.project_update`; do not hand-edit the derived fields.",
        "",
    ]
    for key in FIELD_ORDER:
        lines.append(f"{key}: {state.get(key, 'unknown')}")
    lines.append("")
    return "\n".join(lines)


def write_state(state: dict, path: Path | None = None) -> Path:
    """Write the portfolio state, but only when something actually changed.

    ``updated_at`` changes every run by definition, so comparing rendered text would
    report a difference every single time and force a commit on every workflow run.
    It is excluded, which makes an unchanged portfolio a true no-op — the same
    guarantee the ingest loop gives.
    """
    p = path or PORTFOLIO_STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and _comparable(p.read_text(encoding="utf-8")) == _comparable(render_state(state)):
        return p
    p.write_text(render_state(state), encoding="utf-8")
    return p


def _comparable(rendered: str) -> str:
    return "\n".join(
        line for line in rendered.splitlines() if not line.startswith("updated_at:")
    )


def load_state(path: Path | None = None) -> dict:
    p = path or PORTFOLIO_STATE_PATH
    if not p.exists():
        return {}
    content = p.read_text(encoding="utf-8")
    return {key: value.strip() for key, value in _FIELD_RE.findall(content) if key in FIELD_ORDER}
