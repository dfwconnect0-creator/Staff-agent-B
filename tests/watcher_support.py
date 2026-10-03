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


class GitHub:
    """A scripted GitHub, queried and dispatched through strictly separate channels."""

    def __init__(self):
        self.dispatch_calls = []
        self.list_calls = []
        self.runs = []
        self.dispatch_returncode = 0
        self.dispatch_exception = None
        self.list_returncode = 0
        self.list_exception = None
        self.dispatch_hook = None
        self.invoked_with_shell = []

    # -- scripting the remote ----------------------------------------------------

    def add_run(self, correlation_id, run_id, status, conclusion):
        """A run that a dispatch with this correlation id really created."""
        from src.observation_contract import run_name_for

        self.runs.append(
            {
                "databaseId": run_id,
                "status": status,
                "conclusion": conclusion,
                "displayTitle": run_name_for(correlation_id),
            }
        )
        self.runs.sort(key=lambda run: -run["databaseId"])
        return run_id

    def add_unrelated_run(self, run_id, title, status="completed", conclusion="success"):
        """Any run the watcher did not create: a nearby dispatch, or a manual one."""
        self.runs.append(
            {
                "databaseId": run_id,
                "status": status,
                "conclusion": conclusion,
                "displayTitle": title,
            }
        )
        self.runs.sort(key=lambda run: -run["databaseId"])
        return run_id

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
        head = tuple(argv[:2])
        if head == ("git",):
            return REAL_RUN(argv, *args, **kwargs)
        if head == ("gh", "workflow"):
            self.dispatch_calls.append(argv)
            if self.dispatch_hook is not None:
                self.dispatch_hook(self._correlation_of(argv))
            if self.dispatch_exception is not None:
                raise self.dispatch_exception
            return _completed(self.dispatch_returncode, "")
        if head == ("gh", "run"):
            self.list_calls.append(argv)
            if self.list_exception is not None:
                raise self.list_exception
            if self.list_returncode != 0:
                return _completed(self.list_returncode, "could not resolve host: github.com")
            return _completed(0, json.dumps(self.runs))
        raise AssertionError(f"unexpected command issued by the watcher: {argv}")

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


class _GithubStub:
    def __call__(self, argv, *args, **kwargs):
        argv = [str(part) for part in argv]
        head = tuple(argv[:2])
        if head == ("git",):
            return _REAL_RUN(argv, *args, **kwargs)
        if head == ("gh", "workflow"):
            raise AssertionError("a reconciling process must never dispatch")
        if head == ("gh", "run"):
            if {github_available}:
                return _sp.CompletedProcess(argv, 0, stdout=json.dumps(_RUNS), stderr="")
            return _sp.CompletedProcess(argv, 1, stdout="", stderr="could not resolve host: github.com")
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
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        + GH_STUB.format(runs=list(runs), github_available=github_available)
        + "import src.local_watcher as lw\n"
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
    assert result.returncode == 0, f"fresh process failed: {result.stderr}"
    return json.loads(result.stdout.strip().splitlines()[-1])


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
