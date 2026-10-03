"""A remote GitHub run counts as this dispatch only when its identity proves it.

Each test names one way a looser rule would answer wrongly: a different correlation id, a
run that merely happened nearby, a manual run with no correlation at all, and an exact run
that failed. Run age, workflow name and branch are never consulted.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import local_watcher as lw  # noqa: E402
from src.observation_contract import run_name_for  # noqa: E402
from tests.watcher_support import GitHub, build_observation, build_payload  # noqa: E402
from tests.watcher_support import watcher  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("watcher")


@pytest.fixture
def github(monkeypatch):
    return GitHub().install(monkeypatch)


# --- the matching decision itself ---------------------------------------------


def test_exact_correlation_matches_the_expected_run(github):
    github.add_run("ab12cd34ef56", 9001, "completed", "success")
    outcome, run = lw.find_run_by_correlation("ab12cd34ef56")
    assert outcome == lw.MATCH
    assert run == {"run_id": 9001, "status": "completed", "conclusion": "success"}
    assert github.dispatch_calls == []
    assert len(github.list_calls) == 1


def test_a_different_correlation_does_not_match(github):
    github.add_run("bbbbbbbbbbbb", 9002, "completed", "success")
    outcome, run = lw.find_run_by_correlation("ab12cd34ef56")
    assert outcome == lw.NO_MATCH
    assert run is None


def test_a_nearby_unrelated_run_does_not_match(github):
    """Newest-first, seconds old, same workflow: still a different dispatch."""
    github.add_run("ffffffffffff", 9003, "completed", "success")
    github.add_run("ab12cd34ef56", 9004, "completed", "failure")
    outcome, run = lw.find_run_by_correlation("ffffffffffff")
    assert outcome == lw.MATCH
    assert run["run_id"] == 9003
    assert lw.find_run_by_correlation("ab12cd34ef56")[1]["run_id"] == 9004


def test_a_manual_run_without_a_correlation_does_not_match(github):
    """A human pressing "Run workflow" produces the bare workflow name, not ours."""
    github.add_unrelated_run(9005, title="Project Observation")
    assert lw.find_run_by_correlation("ab12cd34ef56") == (lw.NO_MATCH, None)


def test_a_run_name_that_merely_starts_with_our_correlation_does_not_match(github):
    """Prefix matching would let a longer id satisfy a shorter expected one."""
    github.add_unrelated_run(9006, title=run_name_for("ab12cd34ef56ffff"))
    assert lw.find_run_by_correlation("ab12cd34ef56")[0] == lw.NO_MATCH


def test_an_empty_query_is_a_reliable_no_match_not_an_unavailable_one(github):
    assert lw.find_run_by_correlation("ab12cd34ef56") == (lw.NO_MATCH, None)


def test_a_query_failure_is_reported_as_unavailable_not_as_no_run(github):
    github.make_query_unavailable()
    outcome, run = lw.find_run_by_correlation("ab12cd34ef56")
    assert outcome == lw.QUERY_UNAVAILABLE
    assert run is None


def test_a_query_timeout_is_reported_as_unavailable(github, monkeypatch):
    import subprocess as sp

    def explode(argv, *args, **kwargs):
        if argv[0] == "git":
            from tests.watcher_support import REAL_RUN

            return REAL_RUN([str(a) for a in argv], *args, **kwargs)
        raise sp.TimeoutExpired(cmd=argv, timeout=30)

    monkeypatch.setattr(sp, "run", explode)
    assert lw.find_run_by_correlation("ab12cd34ef56")[0] == lw.QUERY_UNAVAILABLE


def test_a_malformed_query_answer_is_reported_as_unavailable(monkeypatch):
    import subprocess as sp

    monkeypatch.setattr(
        sp,
        "run",
        lambda argv, **kwargs: sp.CompletedProcess(argv, 0, stdout="{not json", stderr=""),
    )
    assert lw.find_run_by_correlation("ab12cd34ef56")[0] == lw.QUERY_UNAVAILABLE


def test_the_expected_run_name_is_the_one_the_workflow_publishes():
    """The matcher and the workflow must agree on the name, or matching is theatre."""
    workflow = (Path(__file__).parent.parent / ".github/workflows/project-observation.yml").read_text(
        encoding="utf-8"
    )
    assert "run-name: project-observation-${{ inputs.correlation_id }}" in workflow
    assert run_name_for("ab12cd34ef56") == "project-observation-ab12cd34ef56"


# --- dispatch outcomes decided from the exact run ------------------------------


def test_an_exact_queued_run_is_recognised_as_in_flight(github):
    payload = build_payload([build_observation()])
    _, record = lw.dispatch_observations(payload)
    github.add_run(record["correlation_id"], 9100, "queued", None)

    state, settled = lw.reconcile_dispatch(record)
    assert state == lw.STATE_ACCEPTED
    assert settled["run_status"] == "queued"

    stored = lw.load_records()
    assert len(stored) == 1
    assert stored[0]["state"] == lw.STATE_ACCEPTED
    assert stored[0]["run_id"] == 9100
    assert len(github.dispatch_calls) == 1


def test_an_exact_running_run_is_still_in_flight(github):
    payload = build_payload([build_observation()])
    lw.dispatch_observations(payload)
    record_id = lw.load_records()[0]["correlation_id"]
    github.add_run(record_id, 9101, "in_progress", None)
    lw.reconcile_records()
    stored = lw.load_records()
    assert len(stored) == 1
    assert stored[0]["state"] == lw.STATE_ACCEPTED
    assert stored[0]["run_status"] == "in_progress"


def test_an_exact_successful_run_is_acknowledged_and_removed(github):
    payload = build_payload([build_observation()])
    outcome, record = lw.dispatch_observations(payload)
    github.add_run(record["correlation_id"], 9102, "completed", "success")

    outcome, record = lw.reconcile_dispatch(record)
    assert outcome == lw.OUTCOME_ACKNOWLEDGED
    assert lw.load_records() == []
    assert len(github.dispatch_calls) == 1


def test_an_exact_failed_run_preserves_its_payload_for_retry(github):
    payload = build_payload([build_observation()])
    _, record = lw.dispatch_observations(payload)
    github.add_run(record["correlation_id"], 9103, "completed", "failure")

    outcome, record = lw.reconcile_dispatch(record)
    assert outcome == lw.STATE_PENDING

    stored = lw.load_records()
    assert len(stored) == 1
    assert stored[0]["state"] == lw.STATE_PENDING
    assert stored[0]["payload"]["observations"][0]["project_id"] == "alpha"
    assert stored[0]["run_conclusion"] == "failure"


def test_an_exact_cancelled_run_preserves_its_payload_for_retry(github):
    payload = build_payload([build_observation()])
    _, record = lw.dispatch_observations(payload)
    github.add_run(record["correlation_id"], 9104, "completed", "cancelled")

    outcome, record = lw.reconcile_dispatch(record)
    assert outcome == lw.STATE_PENDING
    stored = lw.load_records()
    assert stored[0]["state"] == lw.STATE_PENDING
    assert stored[0]["run_conclusion"] == "cancelled"


def test_a_successful_dispatch_with_a_failed_exact_run_is_not_acceptance_as_success(github):
    """`gh` said OK; the exact run it created had already failed. That is a failure, not a send."""
    github.on_dispatch(lambda corr: github.add_run(corr, 9105, "completed", "failure"))
    outcome, record = lw.dispatch_observations(build_payload([build_observation()]))

    assert outcome == lw.STATE_PENDING
    assert lw.load_records()[0]["state"] == lw.STATE_PENDING
    assert lw.load_records()[0]["run_conclusion"] == "failure"


def test_a_failed_exact_run_next_to_an_unrelated_success_does_not_acknowledge(github):
    payload = build_payload([build_observation()])
    _, record = lw.dispatch_observations(payload)
    github.add_run(record["correlation_id"], 9106, "completed", "failure")
    github.add_unrelated_run(9107, title=run_name_for("cccccccccccc"), status="completed", conclusion="success")

    assert lw.reconcile_records()["pending"] == 1
    assert len(lw.load_records()) == 1
    assert lw.load_records()[0]["state"] == lw.STATE_PENDING


def test_dispatch_is_not_deleted_on_success_before_the_exact_run_is_resolved(github):
    """Acceptance is not delivery: the receipt must survive until the run says otherwise."""
    payload = build_payload([build_observation()])
    outcome, record = lw.dispatch_observations(payload)

    assert outcome == lw.STATE_DISPATCHING
    stored = lw.load_records()
    assert len(stored) == 1
    assert stored[0]["state"] == lw.STATE_DISPATCHING
    assert stored[0]["payload_digest"]
    assert stored[0]["created_at"]
    assert stored[0]["payload"]["observations"][0]["project_id"] == "alpha"
