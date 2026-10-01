"""Collect evidence about a project from its declared source, on this machine.

This is the honest part of the system. It reads what actually exists — git log, file
listings, a human's `PROJECT_STATUS.md` — and turns it into a plain summary that
records **what was seen and when**. It never decides what the summary means: no phase
is inferred from a file count, no completion is inferred from a commit message.

Two consequences worth stating plainly:

* Every function here returns ``fresh: False`` with a reason when the source is
  missing or unreadable. Absence of evidence is recorded, not smoothed over.
* ``refreshable_in_actions`` is about GitHub Actions, not about this module. Running
  this on the user's machine reads local files fine; running it on a CI runner cannot,
  which is why the registry records that flag per project.
"""

import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

CAIRO_TZ = timezone(timedelta(hours=3))
STALE_AFTER_DAYS = 30

_EMPHASIS_RE = re.compile(r"\*{1,2}")


def now_cairo() -> str:
    return datetime.now(CAIRO_TZ).isoformat(timespec="seconds")


def _age_days(iso_timestamp: str) -> float | None:
    try:
        when = datetime.fromisoformat(iso_timestamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=CAIRO_TZ)
    return (datetime.now(CAIRO_TZ) - when).total_seconds() / 86400.0


def freshness(iso_timestamp: str) -> str:
    """`current` / `stale` / `very_stale` / `unknown`.

    Freshness describes how old the newest evidence is. It never implies the project is
    healthy — a very fresh failure is still a failure.
    """
    age = _age_days(iso_timestamp)
    if age is None:
        return "unknown"
    if age <= STALE_AFTER_DAYS:
        return "current"
    if age <= STALE_AFTER_DAYS * 3:
        return "stale"
    return "very_stale"


def _run_git(args: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if result.returncode != 0:
        return False, ""
    return True, result.stdout.strip()


def inspect_repo(path_str: str) -> dict:
    """Read git state for a local repository. Never raises."""
    report = {
        "source": "repo",
        "path": path_str,
        "fresh": False,
        "reason": "",
        "newest_evidence_at": "",
        "facts": [],
    }
    path = Path(path_str).expanduser()
    if not path.exists():
        report["reason"] = f"path does not exist: {path}"
        return report
    if not path.is_dir():
        report["reason"] = f"not a directory: {path}"
        return report

    is_repo, inside = _run_git(["rev-parse", "--is-inside-work-tree"], path)
    if not is_repo or inside != "true":
        report["reason"] = f"not a git repository: {path}"
        return report

    report["fresh"] = True

    _, head = _run_git(["log", "-1", "--format=%H%x09%ad%x09%s", "--date=iso"], path)
    ok, commits = _run_git(["rev-list", "--count", "HEAD"], path)
    if not head or not ok:
        # Both `git log` and `git rev-list HEAD` fail on a repository with no commits.
        # Reading that as "fresh, no facts" would claim a project has history when it
        # has none, so it is reported as exactly what it is.
        report["fresh"] = False
        report["reason"] = "repository has no commits yet; no project history to read"
        report["facts"].append("no commits yet")
        return report

    parts = head.split("\t", 2)
    report["facts"].append(f"last commit {parts[0][:7]}: {parts[2] if len(parts) > 2 else ''}".strip())
    report["newest_evidence_at"] = parts[1] if len(parts) > 1 else ""
    if commits:
        report["facts"].append(f"{commits} commits")

    _, status = _run_git(["status", "--porcelain"], path)
    dirty = [line for line in status.splitlines() if line.strip()]
    if dirty:
        report["facts"].append(f"{len(dirty)} uncommitted file(s)")

    _, branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    if branch:
        report["facts"].append(f"branch {branch}")

    _, remotes = _run_git(["remote", "-v"], path)
    if not remotes:
        report["facts"].append("no git remote configured; not readable from CI")
    return report


def inspect_local_file(path_str: str) -> dict:
    """Read a human-written status file. Returns its first lines verbatim, truncated."""
    report = {
        "source": "local_file",
        "path": path_str,
        "fresh": False,
        "reason": "",
        "newest_evidence_at": datetime.fromtimestamp(Path(path_str).stat().st_mtime, CAIRO_TZ).isoformat(
            timespec="seconds"
        )
        if Path(path_str).exists()
        else "",
        "facts": [],
    }
    path = Path(path_str).expanduser()
    if not path.exists():
        report["reason"] = f"file does not exist: {path}"
        return report
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        report["reason"] = f"could not read {path}: {exc}"
        return report

    report["fresh"] = True
    status_line = None
    for raw in text.splitlines():
        # "**Status:** x" is the shape the user's own status files use, so the emphasis
        # markers are removed before the label is matched. Both `**` and single `*` are
        # dropped, otherwise the closing pair lands inside the quoted status text.
        stripped = _EMPHASIS_RE.sub("", raw).strip()
        if stripped.lower().startswith("status:"):
            status_line = stripped
            break
    if status_line:
        report["facts"].append(status_line[:200])
    else:
        report["facts"].append(text.strip().splitlines()[0][:200] if text.strip() else "file is empty")
    return report


def inspect_directory(path_str: str) -> dict:
    """Summarise a code directory: newest file, entry count, test presence."""
    report = {
        "source": "local_file",
        "path": path_str,
        "fresh": False,
        "reason": "",
        "newest_evidence_at": "",
        "facts": [],
    }
    path = Path(path_str).expanduser()
    if not path.is_dir():
        report["reason"] = f"directory does not exist: {path}"
        return report

    report["fresh"] = True
    ignored = {".git", "node_modules", "__pycache__", ".venv", "venv", ".uv-cache", "dist", "out"}
    newest = None
    newest_path = None
    count = 0
    for item in path.rglob("*"):
        if any(part in ignored for part in item.parts):
            continue
        if not item.is_file():
            continue
        count += 1
        mtime = datetime.fromtimestamp(item.stat().st_mtime, CAIRO_TZ)
        if newest is None or mtime > newest:
            newest = mtime
            newest_path = item
    report["newest_evidence_at"] = newest.isoformat(timespec="seconds") if newest else ""
    report["facts"].append(f"{count} files (excluding build and dependency directories)")

    has_tests = any(p.name.startswith("test") or p.name == "tests" for p in path.iterdir())
    report["facts"].append("has a tests directory" if has_tests else "no tests directory at top level")

    output_dir = path / "output"
    if output_dir.is_dir():
        outputs = [p for p in output_dir.iterdir() if p.is_file()]
        report["facts"].append(f"output/ holds {len(outputs)} file(s)")
    return report


def inspect(project_id: str, evidence_source: str, evidence_path: str) -> dict:
    """Dispatch to the right inspector for a registered project.

    ``event_only`` projects have no source to read, so the result says exactly that
    rather than returning an empty success.
    """
    if evidence_source == "event_only" or evidence_path in ("", "none"):
        return {
            "source": "event_only",
            "path": "none",
            "fresh": False,
            "reason": "no automatic source; this project changes only when reported",
            "newest_evidence_at": "",
            "facts": [],
        }
    if evidence_source == "repo":
        return inspect_repo(evidence_path)
    return inspect_local_file(evidence_path) if evidence_path.endswith(".md") else inspect_directory(evidence_path)
