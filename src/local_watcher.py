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
        # Ignore untracked files (??)
        if l[0] == '?' and l[1] == '?':
            continue
        # Any other char means modified/staged
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
