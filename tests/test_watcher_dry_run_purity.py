"""--dry-run performs no persistent mutation at all.

The dry run used to be a report with side effects: it took the exclusive lock file, ran
`git fetch` and `git merge --ff-only`, and — worst — reconciled through the same code path
as a real cycle, so an exact successful run *deleted* its receipt and an unresolved one was
*restated*. Any of those turns "tell me what would happen" into "do half of it".

The assertion that matters here is not that the report looks right. It is that the recovery
store, the tracked files, the project state and the repository refs are byte-for-byte
identical afterwards, including in the cases where reconciliation would normally accept,
pend, acknowledge, or delete.
"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import local_watcher as lw  # noqa: E402
from tests.watcher_support import (  # noqa: E402
    REAL_RUN,
    GitHub,
    build_observation,
    build_payload,
    dispatch_count,
    git_subcommands,
    run_traced_in_fresh_process,
    run_watcher_main,
    sandbox_projects,
)
from tests.watcher_support import watcher  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("watcher")

DRY_RUN_ARGV = ["local_watcher.py", "--dry-run"]

RECONCILE_THEN_REPORT = """
import sys
sys.argv = ["local_watcher.py", "--dry-run"]
_code = lw.main()
print(json.dumps({"exit": _code}))
"""


@pytest.fixture
def github(monkeypatch):
    return GitHub().install(monkeypatch)


def _digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_digest() -> dict[str, str]:
    """A hash per tracked file, so a change to any one of them is visible."""
    root = lw.REPO_ROOT
    out = REAL_RUN(
        ["git", "ls-files"], cwd=str(root), capture_output=True, text=True, check=True
    )
    return {name: _digest(root / name) for name in out.stdout.split()}


def _refs() -> tuple[str, str, str]:
    def git(*args):
        return REAL_RUN(
            ["git", *args], cwd=str(lw.REPO_ROOT), capture_output=True, text=True, check=True
        ).stdout.strip()

    return git("rev-parse", "HEAD"), git("rev-parse", "origin/main"), git("status", "--porcelain")


def _snapshot(tmp_path: Path) -> dict:
    return {
        "store": _digest(lw.dispatch_records_path()),
        "tree": _tree_digest(),
        "refs": _refs(),
    }


def _seed_queued(github, correlation="ab12cd34ef56", run_id=7001):
    """A receipt whose exact run is still in flight: reconciliation would mark it accepted."""
    record = lw.new_record(correlation, build_payload([build_observation()], correlation))
    record["state"] = lw.STATE_DISPATCHING
    lw.save_records([record])
    github.add_run(correlation, run_id, "queued", None)
    return record


def _seed_failed(github, correlation="ab12cd34ef56", run_id=7002):
    """A receipt whose exact run failed: reconciliation would move it to pending."""
    record = lw.new_record(correlation, build_payload([build_observation()], correlation))
    record["state"] = lw.STATE_DISPATCHING
    lw.save_records([record])
    github.add_run(correlation, run_id, "completed", "failure")
    return record


def _seed_successful(github, correlation="ab12cd34ef56", run_id=7003):
    """A receipt whose exact run succeeded: reconciliation would delete it outright."""
    record = lw.new_record(correlation, build_payload([build_observation()], correlation))
    record["state"] = lw.STATE_ACCEPTED
    lw.save_records([record])
    github.add_run(correlation, run_id, "completed", "success")
    return record


# --- 17 / 18 / 19: the three transitions a dry run must not perform ---------------


def test_a_dry_run_over_a_queued_run_mutates_nothing(watcher, github, monkeypatch, tmp_path):
    _seed_queued(github)
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})
    before = _snapshot(tmp_path)

    assert run_watcher_main(monkeypatch, argv=DRY_RUN_ARGV) == 0

    assert _snapshot(tmp_path) == before
    assert lw.load_records()[0]["state"] == lw.STATE_DISPATCHING, "a dry run accepted an in-flight run"
    assert github.dispatch_calls == []


def test_a_dry_run_over_a_failed_run_mutates_nothing(watcher, github, monkeypatch, tmp_path):
    _seed_failed(github)
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})
    before = _snapshot(tmp_path)

    assert run_watcher_main(monkeypatch, argv=DRY_RUN_ARGV) == 0

    assert _snapshot(tmp_path) == before
    assert lw.load_records()[0]["state"] == lw.STATE_DISPATCHING, "a dry run pended a failed run"


def test_a_dry_run_over_a_successful_run_does_not_delete_the_receipt(watcher, github, monkeypatch, tmp_path):
    """The destructive case: acknowledgement removes the record and its payload."""
    _seed_successful(github)
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})
    before = _snapshot(tmp_path)

    assert run_watcher_main(monkeypatch, argv=DRY_RUN_ARGV) == 0

    assert _snapshot(tmp_path) == before
    stored = lw.load_records()
    assert len(stored) == 1, "a dry run acknowledged a run and deleted its receipt"
    assert stored[0]["state"] == lw.STATE_ACCEPTED
    assert stored[0]["payload"]["observations"], "the payload was lost by a dry run"


def test_a_dry_run_does_not_write_a_store_that_did_not_exist(watcher, github, monkeypatch, tmp_path):
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})

    assert run_watcher_main(monkeypatch, argv=DRY_RUN_ARGV) == 0
    assert not lw.dispatch_records_path().exists()
    assert not lw.lock_path().exists(), "a dry run created the exclusive lock file"


def test_a_dry_run_creates_no_state_directory_when_there_is_none(watcher, monkeypatch, tmp_path):
    """Reading must not be a way of creating durable state."""
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})

    assert run_watcher_main(monkeypatch, argv=DRY_RUN_ARGV) == 0
    assert not Path(lw.state_dir()).exists()


# --- 20: no fetch, no merge ------------------------------------------------------


def test_the_read_only_refresh_never_fetches_or_merges(tmp_path, monkeypatch):
    """The real function, against a real repository, with the remote genuinely behind."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}

    def git(*args, cwd=repo):
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env={**env, **os.environ})

    git("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "first")

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(repo), str(clone)], capture_output=True, env={**env, **os.environ})

    # Move the origin forward so the clone is genuinely behind.
    (repo / "a.txt").write_text("two\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "second")

    # The clone learns the remote moved — as it would on any earlier cycle. What it must
    # not then do is close that gap itself.
    subprocess.run(["git", "fetch", "-q", "origin"], cwd=str(clone), capture_output=True, env={**env, **os.environ})
    before_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=str(clone), capture_output=True, text=True
    ).stdout.strip()
    origin_head = subprocess.run(
        ["git", "rev-parse", "origin/main"], cwd=str(clone), capture_output=True, text=True
    ).stdout.strip()
    assert origin_head != before_head, "the fixture must leave the clone genuinely behind"

    monkeypatch.setattr(lw, "REPO_ROOT", clone)
    issued: list[list[str]] = []
    real_run = subprocess.run

    def record(argv, *args, **kwargs):
        issued.append([str(part) for part in argv])
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", record)
    result = lw.refresh_local_repo_safely(read_only=True)

    assert result["was_behind"] is True
    assert result["updated"] is False
    assert git_subcommands(issued) == ["status", "rev-parse", "rev-parse"], (
        "the read-only path issued a repository-changing command"
    )
    after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(clone), capture_output=True, text=True).stdout.strip()
    assert after == before_head


def test_a_dry_run_in_a_fresh_process_issues_no_write_commands(watcher):
    sandbox = Path(lw.state_dir())
    lw.ensure_dirs()
    record = lw.new_record("ab12cd34ef56", build_payload([build_observation()], "ab12cd34ef56"))
    record["state"] = lw.STATE_ACCEPTED
    lw.save_records([record])
    runs = [
        {
            "databaseId": 7003,
            "status": "completed",
            "conclusion": "success",
            "displayTitle": "project-observation-ab12cd34ef56",
            "createdAt": "2026-10-03T10:00:00Z",
        }
    ]
    before = _digest(lw.dispatch_records_path())

    traced = run_traced_in_fresh_process(
        sandbox,
        RECONCILE_THEN_REPORT,
        runs=runs,
        github_available=True,
        expect_returncode=0,
    )

    assert dispatch_count(traced["calls"]) == 0
    assert _digest(lw.dispatch_records_path()) == before
    assert "fetch" not in git_subcommands(traced["calls"])
    assert "merge" not in git_subcommands(traced["calls"])
    assert "push" not in git_subcommands(traced["calls"])
    assert "commit" not in git_subcommands(traced["calls"])


# --- 21: the report says what would happen, without doing it --------------------


def _dry_run_report(capsys) -> dict:
    out = capsys.readouterr().out
    return json.loads(out.split("reconciled: ", 1)[1].split("\n", 1)[1])


def test_a_dry_run_reports_what_would_be_reconciled(watcher, github, monkeypatch, tmp_path, capsys):
    _seed_queued(github)
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})

    assert run_watcher_main(monkeypatch, argv=DRY_RUN_ARGV) == 0
    report = _dry_run_report(capsys)

    assert report["would_reconcile"] == {"accepted": 1}
    assert report["repository"] == {"read_only": True, "behind": False, "fetched": False, "merged": False}


def test_a_dry_run_reports_a_would_delete_without_deleting(watcher, github, monkeypatch, tmp_path, capsys):
    _seed_successful(github)
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})

    run_watcher_main(monkeypatch, argv=DRY_RUN_ARGV)
    report = _dry_run_report(capsys)

    assert report["would_reconcile"] == {"acknowledged": 1}
    assert len(lw.load_records()) == 1, "it reported a deletion and then performed one"


def test_a_dry_run_reports_what_it_would_dispatch(watcher, github, monkeypatch, tmp_path, capsys):
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})
    import src.project_sources as sources

    monkeypatch.setattr(
        sources,
        "inspect",
        lambda pid, kind, path: {
            "source": kind,
            "path": path,
            "fresh": True,
            "reason": "",
            "newest_evidence_at": "2026-10-01T10:00:00+03:00",
            "facts": [f"{pid} status line"],
        },
    )

    assert run_watcher_main(monkeypatch, argv=DRY_RUN_ARGV) == 0
    report = _dry_run_report(capsys)

    assert report["eligible"] == 2
    assert report["would_dispatch"] == 2
    assert report["would_retire"] == 0
    assert github.dispatch_calls == []


def test_a_dry_run_over_a_corrupt_store_still_refuses(watcher, github, monkeypatch, tmp_path, capsys):
    """Read-only is not a licence to act on a store that cannot be accounted for."""
    lw.ensure_dirs()
    lw.dispatch_records_path().write_text("{ broken", encoding="utf-8")
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})

    assert run_watcher_main(monkeypatch, argv=DRY_RUN_ARGV) == 1
    assert lw.dispatch_records_path().read_text(encoding="utf-8") == "{ broken"
    assert github.dispatch_calls == []


import os  # noqa: E402  (used by the repository fixture below)