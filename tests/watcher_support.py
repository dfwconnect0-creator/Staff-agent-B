"""GitHub transport doubles and sandboxes for the watcher tests.

The distinction this module exists to enforce: ``gh workflow run`` **dispatches** and
``gh run list`` **queries**. A mock that matches loose substrings catches both — and a
match on ``"run"`` also catches ``run list`` — so the answer a test meant to supply for
the query is never delivered and the test passes for the wrong reason. Everything below
keys on the first two argv words and asserts on anything unrecognised.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REAL_RUN = subprocess.run

REPO_ROOT = Path(__file__).resolve().parent.parent

REGISTRY = """# Project Registry

## Projects

### alpha

- id: alpha
- name: Alpha
- aliases: alpha
- tracking: active
- evidence_source: local_file
- evidence_path: {root}/alpha.md
- refreshable_in_actions: no
- onboarded_at: 2026-10-01

### beta

- id: beta
- name: Beta
- aliases: beta
- tracking: active
- evidence_source: local_file
- evidence_path: {root}/beta.md
- refreshable_in_actions: no
- onboarded_at: 2026-10-01

### gamma

- id: gamma
- name: Gamma
- aliases: gamma
- tracking: paused
- evidence_source: local_file
- evidence_path: {root}/gamma.md
- refreshable_in_actions: no
- onboarded_at: 2026-10-01

### delta

- id: delta
- name: Delta
- aliases: delta
- tracking: active
- evidence_source: repo
- evidence_path: {root}/delta
- refreshable_in_actions: yes
- onboarded_at: 2026-10-01
"""


def _completed(returncode: int, stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr="")


def _minutes_ago(minutes: float) -> str:
    """A UTC timestamp ``minutes`` in the past, as GitHub reports run creation times."""
    from datetime import datetime, timedelta, timezone

    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _argv_option(argv: list[str], name: str):
    """The value following ``name`` in an argv, or None. `--limit 50`, not `--limit=50`."""
    for index, part in enumerate(argv):
        if part == name and index + 1 < len(argv):
            return argv[index + 1]
    return None


class GitHub:
    """A scripted GitHub, queried and dispatched through strictly separate channels."""

    def __init__(self):
        self.dispatch_calls = []
        self.list_calls = []
        self.view_calls = []
        self.runs = []
        self.dispatch_returncode = 0
        self.dispatch_exception = None
        self.list_returncode = 0
        self.list_exception = None
        self.dispatch_hook = None
        self.invoked_with_shell = []

    # -- scripting the remote ----------------------------------------------------

    def add_run(self, correlation_id, run_id, status="completed", conclusion="success", created_at=None):
        """A run that a dispatch with this correlation id really created."""
        from src.observation_contract import run_name_for

        self.runs.append(
            {
                "databaseId": run_id,
                "status": status,
                "conclusion": conclusion,
                "displayTitle": run_name_for(correlation_id),
                "createdAt": created_at or _minutes_ago(5),
            }
        )
        self.runs.sort(key=lambda run: -run["databaseId"])
        return run_id

    def add_unrelated_run(self, run_id, title, status="completed", conclusion="success", created_at=None):
        """Any run the watcher did not create: a nearby dispatch, or a manual one."""
        self.runs.append(
            {
                "databaseId": run_id,
                "status": status,
                "conclusion": conclusion,
                "displayTitle": title,
                "createdAt": created_at or _minutes_ago(5),
            }
        )
        self.runs.sort(key=lambda run: -run["databaseId"])
        return run_id

    def bury_run_behind(self, correlation_id, run_id, newer=60, **kwargs):
        """Park one run behind ``newer`` newer runs.

        This is the shape that broke the watcher: the exact run exists, and a fixed-size
        "newest N" query cannot see it. The filler runs are given *newer* ids and newer
        timestamps so they legitimately sort ahead of it.
        """
        target = self.add_run(correlation_id, run_id, **kwargs)
        for index in range(newer):
            self.add_unrelated_run(
                run_id + 1 + index,
                title="project-observation-{:032x}".format(index + 1),
                created_at=_minutes_ago(1),
            )
        return target

    def make_query_unavailable(self, exception=None):
        self.list_returncode = 1
        self.list_exception = exception

    def make_dispatch_timeout(self):
        self.dispatch_exception = subprocess.TimeoutExpired(cmd=["gh", "workflow", "run"], timeout=60)

    def make_dispatch_fail(self, returncode=1):
        self.dispatch_returncode = returncode

    def on_dispatch(self, hook):
        """Register ``hook(correlation_id)``, fired when a dispatch is issued.

        A run can only be listed once it exists, and it exists *after* the correlation
        id is chosen but *before* the watcher queries. This hook is that window, so a
        test can stage the real ordering instead of pretending the run was always there.
        """
        self.dispatch_hook = hook
        return self

    @staticmethod
    def _correlation_of(argv):
        for part in argv:
            if part.startswith("correlation_id="):
                return part.split("=", 1)[1]
        raise AssertionError(f"dispatch carried no correlation_id: {argv}")

    # -- the transport itself ----------------------------------------------------

    def __call__(self, argv, *args, **kwargs):
        argv = [str(part) for part in argv]
        self.invoked_with_shell.append(kwargs.get("shell", False))
        # `argv[:2]` would be ("git", "status") for a git command, never ("git",), so the
        # dispatch channel has to be identified by its first word alone.
        if argv[:1] == ["git"]:
            return REAL_RUN(argv, *args, **kwargs)
        head = tuple(argv[:2])
        if head == ("gh", "workflow"):
            self.dispatch_calls.append(argv)
            if self.dispatch_hook is not None:
                self.dispatch_hook(self._correlation_of(argv))
            if self.dispatch_exception is not None:
                raise self.dispatch_exception
            return _completed(self.dispatch_returncode, "")
        if head == ("gh", "run"):
            if len(argv) > 2 and argv[2] == "view":
                return self._view_run(argv)
            self.list_calls.append(argv)
            if self.list_exception is not None:
                raise self.list_exception
            if self.list_returncode != 0:
                return _completed(self.list_returncode, "could not resolve host: github.com")
            # Honour --limit exactly as GitHub does: a truncated result is what the
            # watcher really gets back, so a test cannot accidentally prove pagination
            # works with a transport that has no pagination.
            limit = _argv_option(argv, "--limit")
            runs = self.runs if limit is None else self.runs[: int(limit)]
            return _completed(0, json.dumps(runs))
        raise AssertionError(f"unexpected command issued by the watcher: {argv}")

    def _view_run(self, argv) -> subprocess.CompletedProcess:
        """`gh run view <id>`: one run by database id, or a failure when it is not there."""
        self.view_calls.append(argv)
        if self.list_returncode != 0:
            return _completed(self.list_returncode, "could not resolve host: github.com")
        wanted = argv[3]
        for run in self.runs:
            if str(run["databaseId"]) == str(wanted):
                return _completed(0, json.dumps(run))
        return _completed(1, f"no run found with ID {wanted}")

    def install(self, monkeypatch) -> "GitHub":
        monkeypatch.setattr(subprocess, "run", self)
        return self

    @property
    def dispatched_correlation_ids(self) -> list[str]:
        found = []
        for argv in self.dispatch_calls:
            for part in argv:
                if part.startswith("correlation_id="):
                    found.append(part.split("=", 1)[1])
        return found


def build_observation(
    project_id="alpha",
    source_type="local_file",
    fresh=True,
    reason="",
    facts=None,
    newest_evidence_at="2026-10-01T10:00:00+03:00",
    observed_at="2026-10-03T12:00:00+03:00",
):
    """A well-formed observation whose digest matches its own evidence."""
    from src.observation_contract import observation_identity

    facts = list(facts) if facts is not None else [f"{project_id} status line"]
    summary = "; ".join(facts) if facts else (reason or "no facts")
    obs = {
        "project_id": project_id,
        "source_type": source_type,
        "fresh": fresh,
        "reason": reason,
        "facts": facts,
        "evidence_summary": summary[:300],
        "observed_at": observed_at,
        "newest_evidence_at": newest_evidence_at,
        "source_freshness": "",
    }
    obs["observation_digest"] = observation_identity(obs)
    return obs


def reseal(observation: dict) -> dict:
    """Recompute an observation's digest after a test mutated one of its fields.

    A stale digest is itself a validation failure, so tests that deliberately build an
    odd-but-well-formed observation must re-derive the digest — otherwise they would be
    proving the digest check instead of the rule they are named after.
    """
    from src.observation_contract import observation_identity

    observation["observation_digest"] = observation_identity(observation)
    return observation


def build_payload(observations, correlation_id="a" * 32):
    return {
        "schema_version": 1,
        "correlation_id": correlation_id,
        "observations": list(observations),
    }


def sandbox_state(tmp_path, monkeypatch) -> Path:
    """Point every durable watcher file at ``tmp_path``. Nothing touches the real home."""
    state = tmp_path / "state"
    monkeypatch.setenv("STAFF_AGENT_STATE_DIR", str(state))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    return state


def sandbox_projects(tmp_path, monkeypatch) -> Path:
    """Point the registry, project state, event log and portfolio at ``tmp_path``."""
    import src.memory.episodic as episodic
    import src.portfolio as portfolio
    import src.project_state as project_state
    import src.projects as registry

    registry_file = tmp_path / "projects.md"
    registry_file.write_text(REGISTRY.format(root=tmp_path), encoding="utf-8")
    monkeypatch.setattr(registry, "DEFAULT_REGISTRY_PATH", registry_file)
    monkeypatch.setattr(episodic, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(project_state, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(portfolio, "PORTFOLIO_STATE_PATH", tmp_path / "portfolio.md")
    return registry_file


@pytest.fixture
def watcher(tmp_path, monkeypatch):
    """Every test in the watcher suite gets an isolated durable state directory."""
    sandbox_state(tmp_path, monkeypatch)
    return tmp_path


GH_STUB = """
import subprocess as _sp
_REAL_RUN = _sp.run
_RUNS = {runs!r}
CALLS = []


def _option(argv, name):
    for index, part in enumerate(argv):
        if part == name and index + 1 < len(argv):
            return argv[index + 1]
    return None


class _GithubStub:
    def __call__(self, argv, *args, **kwargs):
        argv = [str(part) for part in argv]
        CALLS.append(argv)
        if argv[:1] == ["git"]:
            return _REAL_RUN(argv, *args, **kwargs)
        head = tuple(argv[:2])
        if head == ("gh", "workflow"):
            if not {allow_dispatch}:
                raise AssertionError("a reconciling process must never dispatch")
            return _sp.CompletedProcess(argv, 0, stdout="", stderr="")
        if head == ("gh", "run"):
            if not {github_available}:
                return _sp.CompletedProcess(argv, 1, stdout="", stderr="could not resolve host: github.com")
            if len(argv) > 2 and argv[2] == "view":
                wanted = argv[3]
                for run in _RUNS:
                    if str(run.get("databaseId")) == str(wanted):
                        return _sp.CompletedProcess(argv, 0, stdout=json.dumps(run), stderr="")
                return _sp.CompletedProcess(argv, 1, stdout="", stderr="no run found")
            # Same truncation rule as the in-process double, so a restart test is subject
            # to the same pagination semantics as a test that runs in one process.
            limit = _option(argv, "--limit")
            runs = _RUNS if limit is None else _RUNS[: int(limit)]
            return _sp.CompletedProcess(argv, 0, stdout=json.dumps(runs), stderr="")
        raise AssertionError("unexpected command: " + repr(argv))


_sp.run = _GithubStub()
"""


def run_in_fresh_process(state_dir: Path, body: str, runs=(), github_available: bool = True) -> dict:
    """Run ``body`` in a brand-new interpreter that shares only the state directory.

    Restart recovery is proven by reloading from persisted files in another process. An
    in-memory object carried across two calls in one process proves nothing about a
    process that died, and GitHub is stubbed *inside* the child so the child makes no
    network call at all.
    """
    traced = run_traced_in_fresh_process(
        state_dir, body, runs=runs, github_available=github_available, expect_returncode=0
    )
    return traced["result"]


def run_traced_in_fresh_process(
    state_dir: Path,
    body: str,
    runs=(),
    github_available: bool = True,
    allow_dispatch: bool = False,
    expect_returncode: int = 0,
) -> dict:
    """Run ``body`` in a fresh interpreter and report both its result and its commands.

    ``body`` must print exactly one JSON line. The returned ``calls`` list is every argv the
    child actually issued, which is the only honest way to assert that a run dispatched
    nothing, fetched nothing, or merged nothing: inspecting the tree afterwards cannot tell
    a forbidden command that happened to be a no-op from one that was never issued.

    ``allow_dispatch`` exists for the two-process reproductions, where the *first* process
    is the one legitimately sending. It is off by default because a reconciling process
    that dispatches is a failure, and the stub failing loudly beats a test that quietly
    passes because the assertion it needed was never reached.
    """
    script = (
        "import atexit, json, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        + GH_STUB.format(
            runs=list(runs),
            github_available=github_available,
            allow_dispatch=allow_dispatch,
        )
        + "import src.local_watcher as lw\n"
        # Registered rather than appended, so the trace is still emitted when the body
        # exits non-zero. A reproduction that dies early is exactly the one whose commands
        # most need accounting for.
        + "atexit.register(lambda: print(json.dumps({'__calls__': CALLS})))\n"
        + body
        + "\n"
    )
    env = dict(os.environ)
    env["STAFF_AGENT_STATE_DIR"] = str(state_dir)
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = REAL_RUN(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == expect_returncode, (
        f"fresh process exited {result.returncode}, expected {expect_returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    traced = json.loads(lines[-1])
    body_line = json.loads(lines[-2]) if len(lines) > 1 else None
    return {"result": body_line, "calls": traced["__calls__"], "stdout": result.stdout, "stderr": result.stderr}


def git_subcommands(calls) -> list[str]:
    """The git subcommands issued in a traced call list."""
    found = []
    for argv in calls:
        if argv[:1] == ["git"] and len(argv) > 1:
            found.append(argv[1])
    return found


def dispatch_count(calls) -> int:
    """How many workflow dispatches a traced call list contains."""
    return sum(1 for argv in calls if argv[:2] == ["gh", "workflow"])


RECONCILE_BODY = """
summary = lw.reconcile_records()
print(json.dumps({"summary": summary, "records": lw.load_records()}))
"""

LOAD_RECORDS_BODY = """
print(json.dumps(lw.load_records()))
"""


def run_watcher_main(monkeypatch, argv=("local_watcher.py",)) -> int:
    """Invoke the real watcher entrypoint with pytest's own argv kept out of argparse."""
    import sys as _sys

    monkeypatch.setattr(_sys, "argv", list(argv))
    from src import local_watcher as lw

    return lw.main()


def records_in(state_dir: Path) -> list[dict]:
    """Read the durable records from disk, through a separate interpreter."""
    return run_in_fresh_process(state_dir, LOAD_RECORDS_BODY)
