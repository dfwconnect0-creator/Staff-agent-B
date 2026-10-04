"""Boundaries the watcher and the writer must not cross, and repairs that must not regress.

Three groups:

* **30 / 31 — no LLM, no Telegram.** Asserted dynamically with an import guard installed on
  ``sys.meta_path``, not only by grepping the source: a grep can be satisfied by a lazy
  import inside a function nobody took.
* **The previous repairs.** Lock exclusivity across two live processes, atomic outbox
  replacement, fail-closed corruption, shell-safe payload transport, evidence time versus
  scan time, and a dry run that really does inspect.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import local_watcher as lw  # noqa: E402
import src.project_observation as writer  # noqa: E402
from tests.watcher_support import (  # noqa: E402
    REPO_ROOT,
    REAL_RUN,
    GitHub,
    build_observation,
    build_payload,
    run_watcher_main,
    sandbox_projects,
)
from tests.watcher_support import watcher  # noqa: E402,F401

FORBIDDEN_IMPORTS = ("llm", "telegram", "anthropic", "httpx")
WATCHER_SOURCES = ("src/local_watcher.py", "src/project_observation.py", "src/observation_contract.py")


class ForbiddenImports:
    """A meta-path finder that fails loudly if a forbidden package is imported."""

    def __init__(self, forbidden):
        self.forbidden = tuple(forbidden)
        self.attempts = []

    def find_spec(self, fullname, path=None, target=None):
        if any(part in self.forbidden for part in fullname.split(".")):
            self.attempts.append(fullname)
            raise AssertionError(f"the watcher/writer path imported {fullname}")
        return None

    def __enter__(self):
        sys.meta_path.insert(0, self)
        return self

    def __exit__(self, *exc):
        if self in sys.meta_path:
            sys.meta_path.remove(self)
        return False


def json_after_reconcile(out: str):
    """The cycle prints a `reconciled:` summary line before its JSON body."""
    return json.loads(out.split("reconciled: ", 1)[1].split("\n", 1)[1])


@pytest.fixture
def github(monkeypatch):
    return GitHub().install(monkeypatch)


# --- 30 / 31: no model, no message ---------------------------------------------


def test_the_whole_cycle_makes_no_llm_call(watcher, monkeypatch, tmp_path, github):
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
            "facts": [f"{pid} line"],
        },
    )
    github.on_dispatch(lambda corr: github.add_run(corr, 1, "queued", None))

    before = {name for name in sys.modules if any(p in name.split(".") for p in FORBIDDEN_IMPORTS)}
    with ForbiddenImports(FORBIDDEN_IMPORTS) as guard:
        assert run_watcher_main(monkeypatch) == 0
    after = {name for name in sys.modules if any(p in name.split(".") for p in FORBIDDEN_IMPORTS)}

    assert guard.attempts == []
    assert after == before, f"the cycle pulled in {sorted(after - before)}"


def test_applying_a_payload_makes_no_llm_call(monkeypatch, tmp_path):
    sandbox_projects(tmp_path, monkeypatch)
    before = {name for name in sys.modules if any(p in name.split(".") for p in FORBIDDEN_IMPORTS)}
    with ForbiddenImports(FORBIDDEN_IMPORTS) as guard:
        assert writer.apply_payload(build_payload([build_observation()])) == 0
    after = {name for name in sys.modules if any(p in name.split(".") for p in FORBIDDEN_IMPORTS)}

    assert guard.attempts == []
    assert after == before, f"the writer pulled in {sorted(after - before)}"


def test_the_whole_cycle_makes_no_telegram_call(watcher, monkeypatch, tmp_path, github):
    import src.telegram_client as telegram_client

    sent = []
    monkeypatch.setattr(telegram_client, "send_telegram_message", lambda *a, **k: sent.append(a))
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})
    github.on_dispatch(lambda corr: github.add_run(corr, 2, "queued", None))

    with ForbiddenImports(FORBIDDEN_IMPORTS) as guard:
        assert run_watcher_main(monkeypatch) == 0
        assert writer.apply_payload(build_payload([build_observation()])) == 0

    assert sent == []
    assert guard.attempts == []


@pytest.mark.parametrize("source", WATCHER_SOURCES)
def test_no_watcher_source_names_a_model_or_a_message_transport(source):
    text = (REPO_ROOT / source).read_text(encoding="utf-8")
    for forbidden in ("src.llm", "get_provider", "src.telegram_client", "send_telegram_message", "anthropic"):
        assert forbidden not in text, f"{source} references {forbidden}"


def test_the_observation_workflow_calls_no_model_and_sends_no_message():
    text = (REPO_ROOT / ".github/workflows/project-observation.yml").read_text(encoding="utf-8")
    for forbidden in ("anthropic", "GEMINI", "OPENAI", "curl -X POST https://api.telegram", "TELEGRAM_BOT_TOKEN"):
        assert forbidden not in text, f"the observation workflow references {forbidden}"


# --- the real entrypoint ---------------------------------------------------------


def test_the_module_has_a_callable_main_that_returns_an_exit_code(github):
    assert callable(lw.main)
    probe = REAL_RUN(
        [sys.executable, str(REPO_ROOT / "src" / "local_watcher.py"), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0
    assert "--dry-run" in probe.stdout


def test_one_watcher_cycle_dispatches_exactly_one_batch(watcher, github, monkeypatch, tmp_path):
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
            "facts": [f"{pid} line"],
        },
    )
    github.on_dispatch(lambda corr: github.add_run(corr, 3, "queued", None))

    assert run_watcher_main(monkeypatch) == 0
    assert len(github.dispatch_calls) == 1
    payload = json.loads(next(p for p in github.dispatch_calls[0] if p.startswith("payload="))[8:])
    assert {obs["project_id"] for obs in payload["observations"]} == {"alpha", "beta"}


# --- the lock is exclusive -------------------------------------------------------


LOCK_HOLDER = """
import json, sys, time
sys.path.insert(0, {repo!r})
import src.local_watcher as lw
print(json.dumps({{"acquired": lw.acquire_lock()}}), flush=True)
time.sleep(3)
print(json.dumps({{"released": True}}), flush=True)
"""


def _hold_the_lock(state_dir: Path) -> subprocess.Popen:
    import os

    env = dict(os.environ)
    env["STAFF_AGENT_STATE_DIR"] = str(state_dir)
    return subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER.format(repo=str(REPO_ROOT))],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_a_second_live_process_cannot_take_the_lock(watcher):
    holder = _hold_the_lock(lw.state_dir())
    try:
        assert json.loads(holder.stdout.readline())["acquired"] is True
        assert lw.acquire_lock() is False, "two live processes both held the lock"
    finally:
        holder.wait(timeout=30)


def test_the_lock_is_released_and_reacquirable(watcher):
    holder = _hold_the_lock(lw.state_dir())
    try:
        assert json.loads(holder.stdout.readline())["acquired"] is True
        assert lw.acquire_lock() is False
    finally:
        holder.wait(timeout=30)
    assert lw.acquire_lock() is True
    lw.release_lock()


def test_the_lock_is_a_real_flock_not_a_marker_file(watcher):
    lw.acquire_lock()
    try:
        assert lw.lock_path().exists()
        assert not (lw.lock_path().read_text(encoding="utf-8").strip()), (
            "the lock file carries no owner, so it cannot be a PID marker"
        )
    finally:
        lw.release_lock()


def test_the_lock_lives_in_the_overridden_state_directory(watcher, tmp_path):
    assert lw.lock_path().parent == lw.state_dir()
    assert lw.state_dir() == tmp_path / "state"


# --- outbox atomicity and fail-closed corruption ---------------------------------


def test_the_outbox_is_replaced_atomically_and_never_truncated_in_place(watcher, monkeypatch):
    import os

    path = lw.dispatch_records_path()
    lw.save_json(path, [{"correlation_id": "a" * 32}])

    observed = []
    real_replace = os.replace

    def spy(src, dst):
        observed.append(json.loads(Path(dst).read_text(encoding="utf-8")))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    lw.save_json(path, [{"correlation_id": "b" * 32}])

    assert observed == [[{"correlation_id": "a" * 32}]], "the destination was truncated before the swap"
    assert json.loads(path.read_text(encoding="utf-8")) == [{"correlation_id": "b" * 32}]
    assert list(path.parent.glob("*.tmp")) == []


def test_a_failed_outbox_write_keeps_the_previous_content(watcher, monkeypatch):
    import os

    path = lw.dispatch_records_path()
    lw.save_json(path, [{"correlation_id": "a" * 32}])

    def explode(fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(os, "fsync", explode)
    with pytest.raises(OSError):
        lw.save_json(path, [{"correlation_id": "c" * 32}])

    assert json.loads(path.read_text(encoding="utf-8")) == [{"correlation_id": "a" * 32}]
    assert list(path.parent.glob("*.tmp")) == [], "an interrupted write left a partial file behind"


def test_a_corrupt_record_store_fails_closed_rather_than_reading_as_empty(watcher):
    lw.ensure_dirs()
    lw.dispatch_records_path().write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        lw.load_records()


@pytest.mark.parametrize("body", ['["a bare string"]', "[null]", "[3]", '[[]]'])
def test_a_record_store_entry_that_is_not_a_receipt_is_refused(watcher, body):
    """Dropping an unreadable entry would drop the observations it stands for."""
    lw.ensure_dirs()
    lw.dispatch_records_path().write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        lw.load_records()


def test_a_record_store_of_the_wrong_shape_is_refused(watcher):
    lw.ensure_dirs()
    lw.dispatch_records_path().write_text('{"not": "a list"}', encoding="utf-8")
    with pytest.raises(ValueError):
        lw.load_records()


def test_a_corrupt_record_store_stops_the_watcher_before_it_dispatches(watcher, github, monkeypatch, tmp_path):
    lw.ensure_dirs()
    lw.dispatch_records_path().write_text("{ truncated", encoding="utf-8")
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})

    assert run_watcher_main(monkeypatch) == 1
    assert github.dispatch_calls == [], "a corrupt recovery store still sent observations"


def test_an_unreadable_record_is_never_silently_dropped(watcher):
    """A record that cannot be interpreted stops the cycle, and is still on disk after.

    The store is the only proof of what this machine already sent. When one entry of it is
    unreadable, the honest response is to refuse to act on any of it — not to drop the bad
    entry, which would free the observations it stands for to be rediscovered and sent
    again, and not to carry on with the entries that happen to parse.
    """
    good = lw.new_record("a" * 32, build_payload([build_observation()]))
    lw.save_json(lw.dispatch_records_path(), [good, {"correlation_id": "broken", "state": "dispatching"}])
    before = lw.dispatch_records_path().read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        lw.reconcile_records()

    assert lw.dispatch_records_path().read_text(encoding="utf-8") == before, (
        "a refused store must be left exactly as it was found"
    )


# --- payload transport is not shell code -----------------------------------------


def workflow_run_blocks(text: str) -> list[str]:
    """Every ``run:`` script body, by indentation. `env:` sits at the key's own level."""
    lines = text.splitlines()
    blocks = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped == "run:" or re.match(r"run:\s*[|>][-+]?\s*$", stripped)):
            continue
        key_indent = len(line) - len(line.lstrip())
        body = []
        for follow in lines[index + 1 :]:
            if not follow.strip():
                body.append("")
                continue
            if len(follow) - len(follow.lstrip()) <= key_indent:
                break
            body.append(follow.strip())
        blocks.append("\n".join(body).strip())
    return blocks


def test_no_workflow_run_block_interpolates_an_input_expression():
    """`run:` bodies must be literal. Inputs arrive through `env:`, which is not code."""
    text = (REPO_ROOT / ".github/workflows/project-observation.yml").read_text(encoding="utf-8")
    blocks = workflow_run_blocks(text)
    assert blocks, "no run block found, so this test would prove nothing"
    for block in blocks:
        assert "${{" not in block, f"a run block interpolates an expression: {block!r}"


def test_no_workflow_run_block_mentions_the_payload_or_the_correlation_id():
    text = (REPO_ROOT / ".github/workflows/project-observation.yml").read_text(encoding="utf-8")
    for block in workflow_run_blocks(text):
        for forbidden in ("OBSERVATION_PAYLOAD", "inputs.payload", "inputs.correlation_id", "$OBSERVATION"):
            assert forbidden not in block, f"a run block names {forbidden}: {block!r}"


def test_the_payload_reaches_the_writer_only_through_the_environment():
    text = (REPO_ROOT / ".github/workflows/project-observation.yml").read_text(encoding="utf-8")
    assert "OBSERVATION_PAYLOAD: ${{ inputs.payload }}" in text
    assert "OBSERVATION_PAYLOAD: ${{ env.OBSERVATION_PAYLOAD }}" in text
    source = (REPO_ROOT / "src/project_observation.py").read_text(encoding="utf-8")
    assert "os.environ.get(\"OBSERVATION_PAYLOAD\")" in source
    assert "sys.argv" not in source
    assert "sys.stdin" not in source


def test_a_payload_with_shell_metacharacters_stays_one_argument(watcher, github):
    hostile = "it's fine; rm -rf / $(whoami) `id` \"quoted\""
    observation = build_observation(facts=[hostile], reason=hostile)
    lw.dispatch_observations(build_payload([observation]))

    assert len(github.dispatch_calls) == 1
    argv = github.dispatch_calls[0]
    payload_arg = next(part for part in argv if part.startswith("payload="))
    assert json.loads(payload_arg[len("payload=") :])["observations"][0]["facts"] == [hostile]
    assert set(github.invoked_with_shell) == {False}, "a shell was used to invoke gh"


# --- evidence time is not scan time ----------------------------------------------


def test_the_observation_carries_source_evidence_time_and_scan_time_apart():
    report = {
        "source": "local_file",
        "path": "/x/STATUS.md",
        "fresh": True,
        "reason": "",
        "newest_evidence_at": "2020-01-01T00:00:00+03:00",
        "facts": ["Status: old"],
    }
    observation = lw.make_observation("alpha", report)

    assert observation["newest_evidence_at"] == "2020-01-01T00:00:00+03:00"
    assert observation["observed_at"] != "2020-01-01T00:00:00+03:00"
    assert observation["observed_at"].startswith("20")


def test_the_writer_uses_evidence_time_for_freshness_not_the_scan_clock(monkeypatch, tmp_path):
    sandbox_projects(tmp_path, monkeypatch)
    observation = build_observation(
        project_id="alpha",
        facts=["Status: abandoned"],
        newest_evidence_at="2020-01-01T00:00:00+03:00",
        observed_at=lw.now_cairo(),
    )
    assert writer.apply_payload(build_payload([observation])) == 0
    import src.project_state as project_state

    assert project_state.load_state("alpha")["source_freshness"] == "very_stale"


# --- dry run really inspects -----------------------------------------------------


def test_a_dry_run_inspects_the_registered_local_projects_and_dispatches_nothing(
    watcher, github, monkeypatch, tmp_path, capsys
):
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})
    import src.project_sources as sources

    inspected = []

    def fake_inspect(pid, kind, path):
        inspected.append(pid)
        return {
            "source": kind,
            "path": path,
            "fresh": True,
            "reason": "",
            "newest_evidence_at": "2026-10-01T10:00:00+03:00",
            "facts": [f"{pid} line"],
        }

    monkeypatch.setattr(sources, "inspect", fake_inspect)

    assert run_watcher_main(monkeypatch, argv=["local_watcher.py", "--dry-run"]) == 0

    assert sorted(inspected) == ["alpha", "beta"], "gamma is paused and delta is Actions-refreshable"
    report = json_after_reconcile(capsys.readouterr().out)
    assert report == {
        "eligible": 2,
        "inspected": 2,
        "new_observations": 2,
        "pending_records": 0,
        "unresolved_records": 0,
        "would_dispatch": 2,
        "would_reconcile": {},
        "would_retire": 0,
        "repository": {"read_only": True, "behind": False, "fetched": False, "merged": False},
    }
    assert github.dispatch_calls == []
    assert not lw.dispatch_records_path().exists(), "a dry run wrote durable state"


def test_a_dry_run_does_not_settle_an_unresolved_record(watcher, monkeypatch, tmp_path, capsys):
    github = GitHub()
    github.make_query_unavailable()
    github.install(monkeypatch)
    record = lw.new_record("a" * 32, build_payload([build_observation()]))
    record["state"] = lw.STATE_UNCERTAIN
    lw.put_record(record)

    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})
    assert run_watcher_main(monkeypatch, argv=["local_watcher.py", "--dry-run"]) == 0

    assert lw.load_records()[0]["state"] == lw.STATE_UNCERTAIN
    assert lw.load_records()[0]["updated_at"] == record["updated_at"]


def test_a_dirty_worktree_stops_the_cycle_without_dispatching(watcher, github, monkeypatch, tmp_path):
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(
        lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": False, "dirty": True, "reason": "worktree has local modifications"}
    )
    assert run_watcher_main(monkeypatch) == 0
    assert github.dispatch_calls == []


def test_a_second_watcher_does_not_run_while_the_first_holds_the_lock(monkeypatch, tmp_path, capsys):
    assert lw.acquire_lock() is True
    try:
        assert run_watcher_main(monkeypatch) == 0
        assert "already running" in capsys.readouterr().err
    finally:
        lw.release_lock()

# --- the watcher and the writer are one contract ---------------------------------


def test_a_dispatched_payload_is_the_payload_the_writer_accepts(watcher, github, monkeypatch, tmp_path):
    """The end-to-end wiring: what the watcher persists is what the writer validates.

    Written because the two ends drifted once already — the watcher minted a correlation
    id outside the payload while the writer required it inside, so every real dispatch
    would have been rejected. Each side passed its own tests while the loop was broken.
    """
    sandbox_projects(tmp_path, monkeypatch)
    import src.projects as registry

    state, record = lw.dispatch_observations(build_payload([build_observation()]))
    assert state == lw.STATE_DISPATCHING

    stored = json.loads(lw.dispatch_records_path().read_text(encoding="utf-8"))
    assert len(stored) == 1
    payload = stored[0]["payload"]

    assert payload["correlation_id"] == record["correlation_id"]
    assert lw.payload_digest(payload) == stored[0]["payload_digest"]
    assert writer.validate_payload(payload, registry.load_registry()) == []
    assert writer.apply_payload(payload) == 0
    assert lw.load_records() == stored, "applying the payload changed the durable receipt"


def test_the_correlation_id_the_watcher_sends_matches_the_one_in_the_payload(watcher, github):
    state, record = lw.dispatch_observations(build_payload([build_observation()]))
    assert github.dispatched_correlation_ids == [record["correlation_id"]]
    sent = json.loads(
        next(p for p in github.dispatch_calls[0] if p.startswith("payload="))[len("payload=") :]
    )
    assert sent["correlation_id"] == github.dispatched_correlation_ids[0]


def test_a_force_dispatch_preview_is_also_an_applicable_payload(watcher, github, monkeypatch, tmp_path, capsys):
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})
    assert run_watcher_main(monkeypatch, argv=["local_watcher.py", "--force-dispatch"]) == 0
    payload = json_after_reconcile(capsys.readouterr().out)
    import src.projects as registry

    assert writer.validate_payload(payload, registry.load_registry()) == []
    assert github.dispatch_calls == []
