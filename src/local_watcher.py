#!/usr/bin/env python3
"""Autonomous local project watcher."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

CAIRO_TZ = timezone(timedelta(hours=3))

REPO_ROOT = Path(__file__).parent.parent
XDG_RUNTIME_DIR = os.environ.get("XDG_RUNTIME_DIR")
if XDG_RUNTIME_DIR:
    LOCK_PATH = Path(XDG_RUNTIME_DIR) / "staff-agent" / "watcher.lock"
else:
    LOCK_PATH = Path.home() / ".local" / "state" / "staff-agent" / "watcher.lock"

PENDING_OBS_PATH = Path.home() / ".local" / "state" / "staff-agent" / "pending-observations.json"


def now_cairo() -> str:
    return datetime.now(CAIRO_TZ).isoformat(timespec="seconds")


def ensure_dirs():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    PENDING_OBS_PATH.parent.mkdir(parents=True, exist_ok=True)


def acquire_lock() -> bool:
    ensure_dirs()
    try:
        LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
        return True
    except Exception:
        return False


def release_lock():
    try:
        LOCK_PATH.unlink(missing_ok=True)
    except Exception:
        pass


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


def load_pending() -> list[dict]:
    ensure_dirs()
    try:
        if PENDING_OBS_PATH.exists():
            return json.loads(PENDING_OBS_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def save_pending(observations: list[dict]):
    ensure_dirs()
    try:
        PENDING_OBS_PATH.write_text(json.dumps(observations, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def clear_pending():
    ensure_dirs()
    try:
        if PENDING_OBS_PATH.exists():
            PENDING_OBS_PATH.unlink(missing_ok=True)
    except Exception:
        pass


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


def dispatch_workflow(payload: dict) -> tuple[bool, str, str | None]:
    try:
        payload_json = json.dumps(payload)
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
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return False, result.stderr or result.stdout or "dispatch failed", None
        return True, result.stdout or "dispatched", None
    except subprocess.TimeoutExpired:
        return False, "dispatch timeout (client)", None
    except Exception as e:
        return False, str(e), None


def _obs_material_equal(obs1: dict, obs2: dict) -> bool:
    return (
        obs1.get("fresh") == obs2.get("fresh")
        and obs1.get("reason") == obs2.get("reason")
        and obs1.get("facts") == obs2.get("facts")
        and obs1.get("project_id") == obs2.get("project_id")
    )


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

        catalog = registry.load_registry()
        eligible = []
        for pid, entry in catalog.items():
            if entry.tracking != "active":
                continue
            if entry.evidence_source in ("event_only",):
                continue
            if entry.refreshable_in_actions == "no":
                eligible.append((pid, entry))

        pending = load_pending()
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

        all_to_dispatch = list(pending)
        for obs in changed_observations:
            d = observation_digest(obs)
            if not any(observation_digest(p) == d for p in all_to_dispatch):
                all_to_dispatch.append(obs)

        if args.dry_run:
            print(json.dumps({"eligible": len(eligible), "changed": len(changed_observations), "pending": len(pending), "to_dispatch": len(all_to_dispatch)}, indent=2))
            return 0

        if not all_to_dispatch:
            clear_pending()
            return 0

        payload = {
            "schema_version": 1,
            "observations": all_to_dispatch[:50],
        }

        if args.force_dispatch:
            print(json.dumps(payload, indent=2))
            clear_pending()
            return 0

        ok, msg, run_id = dispatch_workflow(payload)
        if ok:
            clear_pending()
            print(f"dispatched {len(all_to_dispatch)} observations: {msg}")
            return 0
        else:
            save_pending(all_to_dispatch)
            print(f"dispatch failed, queued: {msg}", file=sys.stderr)
            return 1
    finally:
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
