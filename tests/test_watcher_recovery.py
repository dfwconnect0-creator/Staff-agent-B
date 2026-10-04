"""The recovery guarantee, proved across process boundaries.

Local timeout, remote accepted run, process exits, next watcher run — zero duplicate
dispatches. Restart survival is demonstrated by reloading the durable records in a brand
new interpreter that shares nothing with this one except the state directory on disk.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import local_watcher as lw  # noqa: E402
from src.observation_contract import run_name_for  # noqa: E402
from tests.watcher_support import (  # noqa: E402
    RECONCILE_BODY,
    GitHub,
    build_observation,
    build_payload,
    records_in,
    run_in_fresh_process,
    run_watcher_main,
)
from tests.watcher_support import watcher  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("watcher")


@pytest.fixture
def github(monkeypatch):
    return GitHub().install(monkeypatch)


def _state_dir() -> Path:
    return Path(lw.state_dir())


def _staged_runs(github):
    """The runs the parent scripted, handed to the child as read-only remote truth."""
    if github.list_returncode != 0:
        return []
    return list(github.runs)


def _github_available(github) -> bool:
    return github.list_returncode == 0


# --- 9: timeout after the exact run was created --------------------------------


def test_a_timeout_after_the_exact_run_exists_does_not_dispatch_again(github):
    github.on_dispatch(lambda corr: github.add_run(corr, 7001, "queued", None))
    github.make_dispatch_timeout()

    outcome, record = lw.dispatch_observations(build_payload([build_observation()]))

    assert outcome == lw.STATE_ACCEPTED
    assert len(github.dispatch_calls) == 1
    stored = lw.load_records()
    assert len(stored) == 1
    assert stored[0]["state"] == lw.STATE_ACCEPTED
    assert stored[0]["run_id"] == 7001


# --- 10: timeout, reliable no-match -> pending ---------------------------------


def test_a_timeout_with_a_reliable_no_match_waits_out_the_visibility_grace(github):
    """An authoritative absence is still not immediately a licence to resend.

    An empty run history really does prove no run exists, but the dispatch that timed out
    was issued seconds earlier and GitHub can accept a request before the run shows up in
    any listing. Treating the very first empty answer as a resend licence is the duplicate
    dispatch this whole mechanism exists to prevent, so the receipt is held through the
    grace window instead.
    """
    github.make_dispatch_timeout()

    outcome, record = lw.dispatch_observations(build_payload([build_observation()]))

    assert outcome == lw.STATE_UNCERTAIN
    stored = lw.load_records()
    assert len(stored) == 1
    assert stored[0]["state"] == lw.STATE_UNCERTAIN
    assert stored[0]["payload"]["observations"][0]["project_id"] == "alpha"
    assert len(github.dispatch_calls) == 1


def test_an_uncertain_record_becomes_pending_only_after_the_grace_expires(github):
    """The grace is a delay, not a permanent hold: it ends in a real resend licence."""
    from datetime import datetime, timedelta

    record = lw.new_record("d" * 32, build_payload([build_observation()]))
    record["state"] = lw.STATE_UNCERTAIN
    lw.put_record(record)

    during = lw.reconcile_dispatch(dict(record), now=datetime.fromisoformat(record["created_at"]))
    assert during[0] == lw.STATE_UNCERTAIN

    expired = datetime.fromisoformat(record["created_at"]) + timedelta(seconds=lw.VISIBILITY_GRACE_SECONDS + 1)
    after = lw.reconcile_dispatch(dict(record), now=expired)
    assert after[0] == lw.STATE_PENDING
    assert lw.load_records()[0]["state"] == lw.STATE_PENDING


# --- 11: timeout, GitHub unqueryable -> uncertain -----------------------------


def test_a_timeout_with_an_unqueryable_github_leaves_the_record_uncertain(github):
    github.make_dispatch_timeout()
    github.make_query_unavailable()

    outcome, record = lw.dispatch_observations(build_payload([build_observation()]))

    assert outcome == lw.STATE_UNCERTAIN
    stored = lw.load_records()
    assert len(stored) == 1
    assert stored[0]["state"] == lw.STATE_UNCERTAIN
    assert stored[0]["payload"]["observations"][0]["project_id"] == "alpha"
    assert len(github.dispatch_calls) == 1


def test_an_unqueryable_github_is_not_read_as_proof_that_no_run_exists(github):
    github.make_query_unavailable()
    outcome, record = lw.dispatch_observations(build_payload([build_observation()]))
    assert outcome != lw.STATE_PENDING
    assert lw.load_records()[0]["state"] == lw.STATE_UNCERTAIN


def test_a_dispatch_error_is_resolved_the_same_way_a_timeout_is(github):
    github.make_dispatch_fail(returncode=1)
    github.on_dispatch(lambda corr: github.add_run(corr, 7002, "queued", None))
    outcome, record = lw.dispatch_observations(build_payload([build_observation()]))
    assert outcome == lw.STATE_ACCEPTED
    assert len(github.dispatch_calls) == 1


# --- 12: uncertainty survives process restart ----------------------------------


def test_an_uncertain_dispatch_survives_process_restart(github, tmp_path):
    github.make_dispatch_timeout()
    github.make_query_unavailable()
    outcome, record = lw.dispatch_observations(build_payload([build_observation()]))
    assert outcome == lw.STATE_UNCERTAIN
    correlation_id = record["correlation_id"]

    fresh = records_in(_state_dir())

    assert len(fresh) == 1
    assert fresh[0]["state"] == lw.STATE_UNCERTAIN
    assert fresh[0]["correlation_id"] == correlation_id
    assert fresh[0]["payload"]["observations"][0]["project_id"] == "alpha"
    assert fresh[0]["payload_digest"] == lw.payload_digest(fresh[0]["payload"])


def test_an_unlisted_dispatch_survives_the_process_that_wrote_it_dying(github, tmp_path):
    """The receipt is on disk before the request leaves, so a crash still leaves a trace.

    `gh` accepted the request and the run is not listed yet. Deleting the receipt here
    would leave the next process with no way to learn that this payload exists remotely.
    """
    outcome, record = lw.dispatch_observations(build_payload([build_observation()]))
    assert outcome == lw.STATE_DISPATCHING

    fresh = records_in(_state_dir())

    assert len(fresh) == 1
    assert fresh[0]["state"] == lw.STATE_DISPATCHING
    assert fresh[0]["correlation_id"] == record["correlation_id"]


# --- 13: the next run reconciles -----------------------------------------------


def test_the_next_run_reconciles_an_uncertain_record_against_its_exact_run(github, tmp_path, monkeypatch):
    github.make_dispatch_timeout()
    github.make_query_unavailable()
    _, record = lw.dispatch_observations(build_payload([build_observation()]))
    github.list_returncode = 0
    github.add_run(record["correlation_id"], 7003, "completed", "success")

    fresh = run_in_fresh_process(
        _state_dir(), RECONCILE_BODY, runs=_staged_runs(github), github_available=_github_available(github)
    )

    assert fresh["summary"][lw.OUTCOME_ACKNOWLEDGED] == 1
    assert fresh["records"] == []


def test_a_fresh_process_that_finds_the_exact_run_in_flight_keeps_the_record(github, tmp_path):
    github.make_dispatch_timeout()
    _, record = lw.dispatch_observations(build_payload([build_observation()]))
    github.list_returncode = 0
    github.add_run(record["correlation_id"], 7004, "in_progress", None)

    fresh = run_in_fresh_process(
        _state_dir(), RECONCILE_BODY, runs=_staged_runs(github), github_available=_github_available(github)
    )

    assert fresh["summary"][lw.STATE_ACCEPTED] == 1
    assert len(fresh["records"]) == 1
    assert fresh["records"][0]["state"] == lw.STATE_ACCEPTED


def test_a_fresh_process_that_finds_no_run_holds_the_record_through_the_grace(github, tmp_path):
    """A restart must not reset the window that is protecting the receipt."""
    github.make_dispatch_timeout()
    lw.dispatch_observations(build_payload([build_observation()]))

    fresh = run_in_fresh_process(
        _state_dir(), RECONCILE_BODY, runs=_staged_runs(github), github_available=_github_available(github)
    )

    assert fresh["summary"][lw.STATE_UNCERTAIN] == 1
    assert len(fresh["records"]) == 1
    assert fresh["records"][0]["state"] == lw.STATE_UNCERTAIN


# --- 14: reconciliation is idempotent ------------------------------------------


def test_repeated_reconciliation_of_a_queued_run_changes_nothing(github, tmp_path):
    github.make_dispatch_timeout()
    _, record = lw.dispatch_observations(build_payload([build_observation()]))
    github.list_returncode = 0
    github.add_run(record["correlation_id"], 7005, "queued", None)

    first = run_in_fresh_process(
        _state_dir(), RECONCILE_BODY, runs=_staged_runs(github), github_available=_github_available(github)
    )
    second = run_in_fresh_process(
        _state_dir(), RECONCILE_BODY, runs=_staged_runs(github), github_available=_github_available(github)
    )

    assert first["records"] == second["records"]
    assert len(second["records"]) == 1
    assert second["summary"][lw.STATE_ACCEPTED] == 1
    assert len(github.dispatch_calls) == 1


def test_repeated_reconciliation_after_acknowledgement_stays_acknowledged(github, tmp_path):
    github.make_dispatch_timeout()
    _, record = lw.dispatch_observations(build_payload([build_observation()]))
    github.list_returncode = 0
    github.add_run(record["correlation_id"], 7006, "completed", "success")

    first = run_in_fresh_process(
        _state_dir(), RECONCILE_BODY, runs=_staged_runs(github), github_available=_github_available(github)
    )
    second = run_in_fresh_process(
        _state_dir(), RECONCILE_BODY, runs=_staged_runs(github), github_available=_github_available(github)
    )

    assert first["records"] == []
    assert second["records"] == []
    assert second["summary"][lw.OUTCOME_ACKNOWLEDGED] == 0
    assert len(github.dispatch_calls) == 1


def test_repeated_reconciliation_while_github_is_unavailable_stays_uncertain(github, tmp_path):
    github.make_dispatch_timeout()
    github.make_query_unavailable()
    lw.dispatch_observations(build_payload([build_observation()]))

    first = run_in_fresh_process(
        _state_dir(), RECONCILE_BODY, runs=_staged_runs(github), github_available=_github_available(github)
    )
    second = run_in_fresh_process(
        _state_dir(), RECONCILE_BODY, runs=_staged_runs(github), github_available=_github_available(github)
    )

    assert len(second["records"]) == 1
    assert second["records"][0]["state"] == lw.STATE_UNCERTAIN
    assert first["records"][0]["state"] == second["records"][0]["state"]


# --- 15: an unresolved attempt is never duplicated -----------------------------


def test_an_unresolved_exact_run_never_gets_a_second_dispatch(github, monkeypatch, tmp_path):
    """Two full watcher cycles around one unresolved dispatch. One dispatch total."""
    import src.project_sources as sources
    import src.projects as registry
    from tests.watcher_support import sandbox_projects

    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})
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
    github.on_dispatch(lambda corr: github.add_run(corr, 7007, "queued", None))

    assert run_watcher_main(monkeypatch) == 0
    assert len(github.dispatch_calls) == 1
    assert lw.load_records()[0]["state"] == lw.STATE_ACCEPTED

    # The second cycle re-inspects and finds the same evidence. Because the first run is
    # still in flight remotely, that rediscovery is not new work.
    assert run_watcher_main(monkeypatch) == 0
    assert len(github.dispatch_calls) == 1, "the same observations were dispatched twice"
    assert len(lw.load_records()) == 1
    assert lw.load_records()[0]["state"] == lw.STATE_ACCEPTED


def test_a_pending_record_is_retried_with_a_new_correlation_id_only_once_resolved(github):
    from datetime import datetime, timedelta

    github.make_dispatch_timeout()
    _, record = lw.dispatch_observations(build_payload([build_observation()]))
    assert lw.load_records()[0]["state"] == lw.STATE_UNCERTAIN
    first_id = record["correlation_id"]

    # Age the receipt past its grace window, which is what actually earns the resend.
    aged = dict(lw.load_records()[0])
    expired = datetime.fromisoformat(aged["created_at"]) + timedelta(seconds=lw.VISIBILITY_GRACE_SECONDS + 1)
    lw.save_records([aged])
    assert lw.reconcile_dispatch(lw.load_records()[0], now=expired)[0] == lw.STATE_PENDING

    github.dispatch_calls.clear()
    records = lw.load_records()
    batch, superseded = lw.build_batch(records, [])
    assert len(batch) == 1
    outcome, second = lw.dispatch_observations(lw.build_payload(batch), superseded)

    assert len(github.dispatch_calls) == 1
    assert second["correlation_id"] != first_id
    stored = lw.load_records()
    assert len(stored) == 1
    assert stored[0]["correlation_id"] == second["correlation_id"]


def test_a_record_is_never_retired_twice_when_two_records_carry_the_same_observations(github):
    observation = build_observation()
    for correlation in ("a" * 32, "b" * 32):
        record = lw.new_record(correlation, build_payload([observation], correlation))
        record["state"] = lw.STATE_PENDING
        lw.put_record(record)

    records = lw.load_records()
    batch, superseded = lw.build_batch(records, [])
    assert len(batch) == 1
    assert sorted(superseded) == ["a" * 32, "b" * 32]

    outcome, fresh = lw.dispatch_observations(lw.build_payload(batch), superseded)
    stored = lw.load_records()
    assert len(stored) == 1
    assert stored[0]["correlation_id"] == fresh["correlation_id"]


def test_a_pending_record_partly_overflowing_the_batch_bound_is_not_retired(github):
    """Retiring a record whose payload did not fit would discard unsent observations."""
    many = [build_observation(project_id="alpha", facts=[f"fact {i}"]) for i in range(lw.MAX_OBSERVATIONS + 5)]
    overflowing = lw.new_record("c" * 32, build_payload(many))
    overflowing["state"] = lw.STATE_PENDING
    lw.put_record(overflowing)

    batch, superseded = lw.build_batch(lw.load_records(), [])
    assert len(batch) == lw.MAX_OBSERVATIONS
    assert superseded == [], "a record whose payload did not fully fit must not be retired"

    # The five that did not fit are still owned by the record, so the next cycle after
    # this one dispatches them rather than losing them.
    leftover = [o for o in many if o["observation_digest"] not in {b["observation_digest"] for b in batch}]
    assert len(leftover) == 5
    record = lw.load_records()[0]
    assert all(o["observation_digest"] in {p["observation_digest"] for p in record["payload"]["observations"]}
               for o in leftover)
