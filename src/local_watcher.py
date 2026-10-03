#!/usr/bin/env python3
"""Autonomous local project watcher."""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

CAIRO_TZ = timezone(timedelta(hours=3))

REPO_ROOT = Path(__file__).parent.parent
XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR")
if XDG_RUNTIME_DIR:
    LOCK_DIR = Path(XDG_RUNTIME_DIR) / "staff-agent"
else:
    LOCK_DIR = Path.home() / ".local" / "state" / "staff-agent"
LOCK_PATH = LOCK_DIR / "watcher.lock"

PENDING_OBS_PATH = Path.home() / ".local" / "state" / "staff-agent" / "pending-observations.json"
UNCERTAIN_OBS_PATH = Path.home() / ".local" / "state" / "staff-agent" / "uncertain-observations.json"


def now_cairo() -> str:
    return datetime.now(CAIRO_TZ).isoformat(timespec="seconds")


def ensure_dirs():
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_OBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNCERTAIN_OBS_PATH.parent.mkdir(parents=True, exist_ok=True)


_lock_fd = None


def acquire_lock() -> bool:
    ensure_dirs()
    global _lock_fd
    try:
        _lock_fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except Exception:
        if _lock_fd is not None:
            try:
                os.close(_lock_fd)
            except Exception:
                pass
            _lock_fd = None
        return False


def release_lock():
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except Exception:
            pass
        _lock_fd = None


def load_json(path: Path) -> list[dict]:
    ensure_dirs()
    try:
        if path.exists():
            data = path.read_text(encoding="utf-8")
            return json.loads(data) if data.strip() else []
    except Exception:
        # Fail closed - don't silently corrupt
        raise
    return []


def save_json(path: Path, data: list[dict]):
    ensure_dirs()
    tmp = path.with_suffix(path.suffix + '.tmp')
    content = json.dumps(data, indent=2, ensure_ascii=False)
    try:
        tmp.write_text(content, encoding="utf-8")
        os.fsync(tmp.fileno())
        os.replace(str(tmp), str(path))
        with open(str(path), 'rb') as f:
            os.fsync(f.fileno())
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def clear_json(path: Path):
    ensure_dirs()
    try:
        if path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


load_pending = lambda: load_json(PENDING_OBS_PATH)
save_pending = lambda obs: save_json(PENDING_OBS_PATH, obs)
clear_pending = lambda: clear_json(PENDING_OBS_PATH)
load_uncertain = lambda: load_json(UNCERTAIN_OBS_PATH)
save_uncertain = lambda obs: save_json(UNCERTAIN_OBS_PATH, obs)
clear_uncertain = lambda: clear_json(UNCERTAIN_OBS_PATH)


def _run_git(args: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return False, ""
    if result.returncode != 0:
        return False, ""
    return True, result.stdout.strip()


def refresh_local_repo_safely() -> dict:
    result = {"ok": False, "dirty": False, "was_behind": False, "updated": False, "head": "", "origin_head": "", "reason": ""}
    cwd = REPO_ROOT
    if not cwd.exists():
        result["reason"] = f"repo not found: {cwd}"
        return result
    ok, status = _run_git(["status", "--porcelain"], cwd)
    if not ok:
        result["reason"] = "git status failed"
        return result
    lines = [l for l in status.splitlines() if l.strip()]
    dirty = False
    for l in lines:
        if len(l) < 2:
            continue
        if l[0] == '?' and l[1] == '?':
            continue
        if l[0] != ' ' or l[1] != ' ':
            dirty = True
            break
    result["dirty"] = dirty
    if dirty:
        result["reason"] = "worktree has local modifications"
        return result
    ok, head = _run_git(["rev-parse", "HEAD"], cwd)
    if ok:
        result["head"] = head
    ok, fetch = _run_git(["fetch", "origin"], cwd)
    if not ok:
        result["reason"] = "git fetch failed"
        return result
    ok, origin_head = _run_git(["rev-parse", "origin/main"], cwd)
    if ok:
        result["origin_head"] = origin_head
    if head and origin_head and head != origin_head:
        result["was_behind"] = True
        ok, merge = _run_git(["merge", "--ff-only", "origin/main"], cwd)
        if not ok:
            result["reason"] = "ff-only failed"
            return result
        result["updated"] = True
        ok, head = _run_git(["rev-parse", "HEAD"], cwd)
        if ok:
            result["head"] = head
    result["ok"] = True
    return result


def observation_digest(obs: dict) -> str:
    material = {
        "project_id": obs.get("project_id"),
        "observation_digest": obs.get("observation_digest"),
        "evidence_summary": obs.get("evidence_summary"),
    }
    blob = json.dumps(material, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def make_observation(project_id: str, report: dict) -> dict:
    facts = report.get("facts", [])
    summary = "; ".join(facts) if facts else (report.get("reason") or "no facts")
    obs_digest = hashlib.sha256(
        json.dumps(
            {
                "project_id": project_id,
                "fresh": report["fresh"],
                "reason": report["reason"],
                "facts": report["facts"],
                "newest": report.get("newest_evidence_at", ""),
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "project_id": project_id,
        "observed_at": now_cairo(),
        "source_type": report["source"],
        "source_freshness": "",
        "observation_digest": obs_digest,
        "evidence_summary": summary[:300],
        "fresh": report["fresh"],
        "reason": report["reason"],
        "facts": facts[:20],
    }


def generate_correlation_id(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True) + str(time.time())
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def find_runs_since(cutoff_seconds: int = 300) -> list[dict]:
    try:
        result = subprocess.run(
            [
                "gh",
                "run",
                "list",
                "-R",
                "dfwconnect0-creator/Staff-agent-B",
                "--workflow",
                "project-observation.yml",
                "--limit",
                "20",
                "--json",
                "databaseId,createdAt,status,conclusion,event,headBranch",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return []
        runs = json.loads(result.stdout)
        now = datetime.now(timezone.utc)
        recent = []
        for r in runs:
            try:
                created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00"))
                age = (now - created).total_seconds()
                if age <= cutoff_seconds:
                    recent.append(r)
            except Exception:
                continue
        return recent
    except Exception:
        return []


def dispatch_workflow_safe(payload: dict) -> tuple[str, dict | None]:
    corr_id = generate_correlation_id(payload)
    payload_with_corr = dict(payload)
    payload_with_corr["correlation_id"] = corr_id

    dispatch_failed = False
    try:
        payload_json = json.dumps(payload_with_corr)
        result = subprocess.run(
            [
                "gh",
                "workflow",
                "run",
                "project-observation.yml",
                "-R",
                "dfwconnect0-creator/Staff-agent-B",
                "-f",
                f"payload={payload_json}",
                "-f",
                f"correlation_id={corr_id}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return "dispatched", {"correlation_id": corr_id}
        dispatch_failed = True
    except subprocess.TimeoutExpired:
        dispatch_failed = True
    except Exception:
        dispatch_failed = True

    runs = find_runs_since(300)
    if runs:
        return "accepted", {"correlation_id": corr_id, "recent_runs": len(runs)}
    return "no_run", {"correlation_id": corr_id}


def reconcile_uncertain():
    try:
        uncertain = load_uncertain()
        if not uncertain:
            return
    except Exception:
        # Fail closed - don't corrupt
        return
    try:
        runs = find_runs_since(600)
        if runs:
            clear_uncertain()
            return
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-dispatch", action="store_true")
    args = parser.parse_args()

    if not acquire_lock():
        print("watcher already running, exiting", file=sys.stderr)
        return 0

    try:
        repo_state = refresh_local_repo_safely()
        if not repo_state["ok"]:
            if repo_state["dirty"]:
                print(f"repo not clean, skipping: {repo_state['reason']}", file=sys.stderr)
            else:
                print(f"repo refresh failed: {repo_state['reason']}", file=sys.stderr)
            return 0

        sys.path.insert(0, str(REPO_ROOT))
        import src.projects as registry
        import src.project_sources as sources
        import src.project_state as project_state
        import src.events as events_log

        reconcile_uncertain()

        catalog = registry.load_registry()
        eligible = []
        for pid, entry in catalog.items():
            if entry.tracking != "active":
                continue
            if entry.evidence_source in ("event_only",):
                continue
            if entry.refreshable_in_actions == "no":
                eligible.append((pid, entry))

        try:
            pending = load_pending()
        except Exception:
            pending = []
        try:
            uncertain = load_uncertain()
        except Exception:
            uncertain = []

        changed_observations = []
        for pid, entry in eligible:
            try:
                report = sources.inspect(pid, entry.evidence_source, entry.evidence_path)
            except Exception:
                continue
            obs = make_observation(pid, report)
            previous = events_log.last_project_evidence(pid)
            identical = bool(
                previous
                and previous.get("fresh") == obs["fresh"]
                and previous.get("reason") == obs["reason"]
                and previous.get("facts") == obs["facts"]
            )
            if not identical:
                changed_observations.append(obs)

        all_to_dispatch = list(pending) + list(uncertain)
        for obs in changed_observations:
            d = observation_digest(obs)
            if not any(observation_digest(p) == d for p in all_to_dispatch):
                all_to_dispatch.append(obs)

        if args.dry_run:
            print(json.dumps({"eligible": len(eligible), "inspected": len(eligible), "changed": len(changed_observations), "pending": len(pending), "uncertain": len(uncertain), "would_dispatch": len(all_to_dispatch), "to_dispatch": len(all_to_dispatch)}, indent=2))
            return 0

        if not all_to_dispatch:
            try:
                clear_pending()
                clear_uncertain()
            except Exception:
                pass
            return 0

        payload = {
            "schema_version": 1,
            "observations": all_to_dispatch[:50],
        }

        if args.force_dispatch:
            print(json.dumps(payload, indent=2))
            try:
                clear_pending()
                clear_uncertain()
            except Exception:
                pass
            return 0

        status, info = dispatch_workflow_safe(payload)
        if status == "dispatched" or status == "accepted":
            try:
                clear_pending()
                clear_uncertain()
            except Exception:
                pass
            print(f"dispatched/accepted {len(all_to_dispatch)} observations: {status}")
            return 0
        elif status == "no_run":
            try:
                save_pending(all_to_dispatch)
                clear_uncertain()
            except Exception:
                pass
            print(f"dispatch failed, queued as pending: no remote run found", file=sys.stderr)
            return 1
        elif status == "uncertain":
            try:
                save_uncertain(all_to_dispatch)
                clear_pending()
            except Exception:
                pass
            print(f"dispatch uncertain, queued for reconciliation", file=sys.stderr)
            return 1
        else:
            try:
                save_pending(all_to_dispatch)
                clear_uncertain()
            except Exception:
                pass
            print(f"dispatch failed, queued: {status}", file=sys.stderr)
            return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
