"""Read the project registry: ``context/portfolio/projects.md``.

The registry is the only place a project is declared. It is flat Markdown, parsed
with regex, because the rest of the context files are Markdown too and a human needs
to be able to add a project by hand without learning a schema tool.

The one non-obvious rule here: each project declares whether its evidence source is
readable by GitHub Actions. Most of the user's projects are local-only, and the agent
says so rather than pretending it refreshed them from a runner that has never seen
that disk.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PORTFOLIO_DIR = REPO_ROOT / "context" / "portfolio"
REGISTRY_FILENAME = "projects.md"
DEFAULT_REGISTRY_PATH = PORTFOLIO_DIR / REGISTRY_FILENAME

TRACKING_VALUES = ("active", "paused", "candidate")
EVIDENCE_SOURCES = ("repo", "local_file", "event_only")

_FIELD_RE = re.compile(r"^- ([a-z_]+):[ \t]*(.*)$", re.MULTILINE)
_RECORD_RE = re.compile(r"^### ([a-z0-9-]+)[ \t]*$", re.MULTILINE)


@dataclass
class Project:
    id: str
    name: str = "unknown"
    aliases: list[str] = field(default_factory=list)
    tracking: str = "candidate"
    evidence_source: str = "event_only"
    evidence_path: str = "none"
    refreshable_in_actions: str = "no"
    onboarded_at: str = ""
    description: str = ""

    @property
    def is_active(self) -> bool:
        return self.tracking == "active"


def parse_registry(content: str) -> dict[str, Project]:
    projects: dict[str, Project] = {}
    positions = [(m.start(), m.group(1)) for m in _RECORD_RE.finditer(content)]
    for index, (start, pid) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(content)
        block = content[start:end]
        fields: dict[str, str] = {}
        for key, value in _FIELD_RE.findall(block):
            fields.setdefault(key, value.strip())
        aliases_raw = fields.get("aliases", "")
        projects[pid] = Project(
            id=pid,
            name=fields.get("name", pid),
            aliases=[a.strip() for a in aliases_raw.split(",") if a.strip()],
            tracking=fields.get("tracking", "candidate"),
            evidence_source=fields.get("evidence_source", "event_only"),
            evidence_path=fields.get("evidence_path", "none"),
            refreshable_in_actions=fields.get("refreshable_in_actions", "no"),
            onboarded_at=fields.get("onboarded_at", ""),
            description=fields.get("description", ""),
        )
    return projects


def load_registry(path: Path | None = None) -> dict[str, Project]:
    p = path or DEFAULT_REGISTRY_PATH
    if not p.exists():
        return {}
    return parse_registry(p.read_text(encoding="utf-8"))


def alias_index(projects: dict[str, Project]) -> dict[str, list[str]]:
    """Map every alias (lowercased) to the project ids that claim it.

    An alias claimed by two projects maps to both, which is how the router detects an
    ambiguous reply instead of picking one.
    """
    index: dict[str, list[str]] = {}
    for pid, project in projects.items():
        for alias in {pid, project.name, *project.aliases}:
            key = alias.strip().lower()
            if not key:
                continue
            index.setdefault(key, []).append(pid)
    return {k: sorted(set(v)) for k, v in index.items()}


def validate_registry(projects: dict[str, Project]) -> list[str]:
    """Return human-readable problems. Empty list means the registry is coherent."""
    problems: list[str] = []
    for pid, project in projects.items():
        if project.tracking not in TRACKING_VALUES:
            problems.append(f"{pid}: tracking '{project.tracking}' not in {TRACKING_VALUES}")
        if project.evidence_source not in EVIDENCE_SOURCES:
            problems.append(f"{pid}: evidence_source '{project.evidence_source}' not in {EVIDENCE_SOURCES}")
        if not project.aliases:
            problems.append(f"{pid}: no aliases, a human could never route a reply to it")
    # An alias that duplicates a project's own name is one claim, not two, so dedupe
    # owners before declaring a collision. Otherwise every "name == alias" project
    # would falsely report as ambiguous against itself.
    claims: dict[str, set[str]] = {}
    for pid, project in projects.items():
        for alias in {pid, project.name, *project.aliases}:
            claims.setdefault(alias.strip().lower(), set()).add(pid)
    for alias, owners in claims.items():
        if len(owners) > 1:
            problems.append(f"alias '{alias}' claimed by {sorted(owners)}; routing will treat it as ambiguous")
    return problems
