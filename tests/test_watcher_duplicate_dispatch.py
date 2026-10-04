"""A bounded negative lookup is not proof that nothing exists.

The defect this file exists for: the watcher asked GitHub for the newest N runs, and read
"the exact run is not among them" as "the exact run does not exist". Those are different
statements whenever the run is real but older than N — a dispatch buried behind unrelated
runs was therefore re-sent.

Three things have to be true for a resend to be allowed, and each has its own test:

1. a run id already on the receipt is used directly, with no listing at all;
2. a search that has not found the run keeps going until it has either seen the end of the
   workflow's history or reached back past the dispatch's own timestamp — and if it runs
   out of pages first it says so instead of claiming absence;
3. a run GitHub has accepted but not yet listed is given a grace window, so the very first
   empty answer is never a resend licence.
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import local_watcher as lw  # noqa: E402
from tests.watcher_support import (  # noqa: E402
    RECONCILE_BODY,
    GitHub,
    build_observation,
    build_payload,
    dispatch_count,
    run_in_fresh_process,
    run_traced_in_fresh_process,
)
from tests.watcher_support import watcher  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("watcher")

RUN_NAME = "project-observation-{}"


@pytest.fixture
def github(monkeypatch):
    return GitHub().install(monkeypatch)


def _iso(minutes_ago: float) -> str:
    moment = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return moment.isoformat(timespec="seconds")


def _staged(github) -> list[dict]:
    return [] if github.list_returncode != 0 else list(github.runs)


def _available(github) -> bool:
    return github.list_returncode == 0


def _accepted_receipt(correlation_id: str, run_id: int, created_minutes_ago: float = 30) -> dict:
    record = lw.new_record(
        correlation_id,
        build_payload([build_observation()], correlation_id),
    )
    record["state"] = lw.STATE_ACCEPTED
    record["run_id"] = run_id
    record["run_status"] = "queued"
    record["created_at"] = (datetime.now(CAIRO()) - timedelta(minutes=created_minutes_ago)).isoformat(
        timespec="seconds"
    )
    record["updated_at"] = record["created_at"]
    return record


def CAIRO():
    return timezone(timedelta(hours=3))


# --- 11: the exact run exists, further back than one page -----------------------


def test_an_exact_run_outside_the_first_page_is_still_found(github):
    """More runs exist than one page holds, and the exact one is behind all of them."""
    correlation = "ab12cd34ef56"
    github.bury_run_behind(correlation, 5001, newer=lw.GITHUB_QUERY_PAGE_SIZE + 25)

    outcome, run = lw.find_run_by_correlation(correlation, created_at=_iso(60))

    assert outcome == lw.MATCH
    assert run["run_id"] == 5001
    assert len(github.list_calls) > 1, "the search gave up instead of paginating"


def test_a_buried_run_never_causes_a_redispatch(github):
    correlation = "ab12cd34ef56"
    lw.save_records([_accepted_receipt(correlation, 5001)])
    github.bury_run_behind(correlation, 5001, newer=lw.GITHUB_QUERY_PAGE_SIZE + 25, status="queued", conclusion=None)

    state, _ = lw.reconcile_dispatch(lw.load_records()[0])

    assert state == lw.STATE_ACCEPTED
    assert github.dispatch_calls == []
    assert len(lw.load_records()) == 1, "the receipt was resolved away instead of held"


def test_a_search_stopped_by_the_page_cap_reports_incomplete_not_absent(github):
    """A search that could not finish must not be allowed to claim the run is absent."""
    correlation = "ab12cd34ef56"
    github.bury_run_behind(correlation, 5001, newer=lw.GITHUB_QUERY_PAGE_SIZE * (lw.GITHUB_QUERY_MAX_PAGES + 5))

    outcome, run = lw.find_run_by_correlation(correlation)

    assert outcome == lw.SEARCH_INCOMPLETE
    assert run is None


def test_an_incomplete_search_never_becomes_pending(github):
    correlation = "ab12cd34ef56"
    record = _accepted_receipt(correlation, None)
    lw.save_records([record])
    github.bury_run_behind(correlation, 5001, newer=lw.GITHUB_QUERY_PAGE_SIZE * (lw.GITHUB_QUERY_MAX_PAGES + 5))
    # The receipt is older than the grace, so only the completeness of the search stands
    # between this record and a resend.
    aged = dict(record)
    aged["created_at"] = (datetime.now(CAIRO()) - timedelta(days=2)).isoformat(timespec="seconds")
    lw.save_records([aged])

    state, _ = lw.reconcile_dispatch(lw.load_records()[0])

    assert state == lw.STATE_UNCERTAIN
    assert github.dispatch_calls == []


# --- 12: a stored run id is a complete lookup key -------------------------------


def test_a_stored_run_id_is_queried_directly_without_listing(github):
    correlation = "ab12cd34ef56"
    github.bury_run_behind(correlation, 5002, newer=lw.GITHUB_QUERY_PAGE_SIZE + 40, status="queued", conclusion=None)

    state, record = lw.reconcile_dispatch(_accepted_receipt(correlation, 5002))

    assert state == lw.STATE_ACCEPTED
    assert github.view_calls, "the stored run id was not used"
    assert github.list_calls == [], "a stored run id should not need a recent-runs search"
    assert github.dispatch_calls == []


def test_a_direct_lookup_verifies_the_run_name_before_trusting_the_id(github):
    """A stale or wrong id must not adopt somebody else's run."""
    correlation = "ab12cd34ef56"
    github.add_unrelated_run(5003, title="Project Observation")
    receipt = _accepted_receipt(correlation, 5003, created_minutes_ago=1)

    state, _ = lw.reconcile_dispatch(receipt)

    # The direct lookup refused the name, so the search ran and found nothing either.
    assert state == lw.STATE_UNCERTAIN
    assert github.list_calls, "it fell back to a search rather than trusting the name"
    assert github.dispatch_calls == []


def test_an_unknown_stored_run_id_falls_back_to_the_search(github):
    correlation = "ab12cd34ef56"
    github.add_run(correlation, 5004, "completed", "success")
    receipt = _accepted_receipt(correlation, 999999)

    state, _ = lw.reconcile_dispatch(receipt)

    assert state == lw.OUTCOME_ACKNOWLEDGED
    assert github.view_calls and github.list_calls


# --- 13 / 15: the run exists but is not listed yet ------------------------------


def test_a_temporary_visibility_delay_causes_no_duplicate_dispatch(github):
    """The exact run is accepted remotely; the first complete lookup does not show it."""
    correlation = "ab12cd34ef56"
    # Inside the grace window: this is a run GitHub accepted minutes ago.
    receipt = _accepted_receipt(correlation, None, created_minutes_ago=1)
    receipt["run_id"] = None
    receipt["state"] = lw.STATE_DISPATCHING
    lw.save_records([receipt])
    # An empty workflow history: nothing listed at all.
    github.runs = []

    state, _ = lw.reconcile_dispatch(lw.load_records()[0])

    assert state == lw.STATE_UNCERTAIN, "an invisible run was treated as absent"
    assert github.dispatch_calls == []


def test_visibility_grace_survives_a_fresh_process_restart(github):
    """The window lives in the persisted timestamp, so a restart cannot reset it."""
    correlation = "ab12cd34ef56"
    receipt = _accepted_receipt(correlation, None, created_minutes_ago=1)
    receipt["run_id"] = None
    receipt["state"] = lw.STATE_DISPATCHING
    lw.save_records([receipt])

    fresh = run_in_fresh_process(lw.state_dir(), RECONCILE_BODY, runs=[], github_available=True)

    assert fresh["summary"][lw.STATE_UNCERTAIN] == 1
    assert fresh["records"][0]["state"] == lw.STATE_UNCERTAIN
    assert dispatch_count_in_fresh(lw.state_dir()) == 0


def dispatch_count_in_fresh(state_dir) -> int:
    traced = run_traced_in_fresh_process(state_dir, RECONCILE_BODY, runs=[], github_available=True)
    return dispatch_count(traced["calls"])


def test_a_restarted_process_still_holds_the_receipt_after_the_grace(github):
    """Even past the grace, an *authoritative* negative is required before a resend."""
    correlation = "ab12cd34ef56"
    receipt = _accepted_receipt(correlation, None)
    receipt["run_id"] = None
    receipt["created_at"] = (datetime.now(CAIRO()) - timedelta(days=1)).isoformat(timespec="seconds")
    receipt["updated_at"] = receipt["created_at"]
    receipt["payload_digest"] = lw.payload_digest(receipt["payload"])
    lw.save_records([receipt])

    fresh = run_in_fresh_process(lw.state_dir(), RECONCILE_BODY, runs=[], github_available=True)

    # The history really is empty, so absence is authoritative and the grace has expired.
    assert fresh["summary"][lw.STATE_PENDING] == 1


# --- 14: the question could not be asked ----------------------------------------


def test_an_unavailable_query_is_uncertain_not_pending(github):
    github.make_query_unavailable()
    receipt = _accepted_receipt("ab12cd34ef56", None)

    state, _ = lw.reconcile_dispatch(receipt)

    assert state == lw.STATE_UNCERTAIN
    assert github.dispatch_calls == []


def test_an_unavailable_query_never_becomes_pending_however_old_the_receipt(github):
    github.make_query_unavailable()
    receipt = _accepted_receipt("ab12cd34ef56", None)
    receipt["created_at"] = (datetime.now(CAIRO()) - timedelta(days=30)).isoformat(timespec="seconds")

    state, _ = lw.reconcile_dispatch(receipt)

    assert state == lw.STATE_UNCERTAIN


def test_a_query_unavailable_during_a_full_cycle_dispatches_nothing(github, monkeypatch, tmp_path):
    from tests.watcher_support import sandbox_projects

    import src.project_sources as sources

    github.make_query_unavailable()
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

    # The very observation the scan is about to rediscover, already owned by a receipt
    # whose remote fate cannot be established while GitHub is unreachable.
    seeded = lw.new_record(
        "ab12cd34ef56",
        build_payload([build_observation(facts=["alpha status line"])], "ab12cd34ef56"),
    )
    seeded["state"] = lw.STATE_UNCERTAIN
    lw.save_records([seeded])

    import sys as _sys

    monkeypatch.setattr(_sys, "argv", ["local_watcher.py"])
    # Exit 1 is the honest report here: beta was sent, but nothing could be confirmed.
    assert lw.main() == 1

    # Beta's observation is genuinely new and may go; alpha's is owned by the unresolved
    # receipt and must not travel again while its remote fate is unknown.
    sent = []
    for argv in github.dispatch_calls:
        argument = next(part for part in argv if part.startswith("payload="))
        sent.extend(json.loads(argument[len("payload=") :])["observations"])
    assert [obs["project_id"] for obs in sent] == ["beta"], (
        "the payload owned by an unresolved receipt was dispatched again"
    )
    assert lw.load_records()[0]["state"] == lw.STATE_UNCERTAIN


# --- 16: what an authoritative negative actually requires -----------------------


def test_an_authoritative_negative_after_the_grace_becomes_pending(github):
    receipt = _accepted_receipt("ab12cd34ef56", None, created_minutes_ago=1)
    receipt["state"] = lw.STATE_DISPATCHING

    state, _ = lw.reconcile_dispatch(receipt)

    assert state == lw.STATE_UNCERTAIN, "still inside the grace window"

    aged = datetime.now(CAIRO()) + timedelta(seconds=lw.VISIBILITY_GRACE_SECONDS + 1)
    state, _ = lw.reconcile_dispatch(receipt, now=aged)

    assert state == lw.STATE_PENDING


def test_a_search_that_crossed_the_dispatch_boundary_is_authoritative(github):
    """Every run on the page is older than the dispatch, so the run cannot exist."""
    correlation = "ab12cd34ef56"
    for index in range(lw.GITHUB_QUERY_PAGE_SIZE):
        github.add_unrelated_run(6000 + index, title="project-observation-{:032x}".format(index), created_at=_iso(600))

    outcome, _ = lw.find_run_by_correlation(correlation, created_at=_iso(30))

    assert outcome == lw.NO_MATCH
    assert len(github.list_calls) == 1, "it paginated past a page that already settled the question"


def test_seeing_the_end_of_the_history_is_authoritative_without_a_boundary(github):
    correlation = "ab12cd34ef56"
    github.add_unrelated_run(7001, title="Project Observation")

    outcome, _ = lw.find_run_by_correlation(correlation)

    assert outcome == lw.NO_MATCH
    assert len(github.list_calls) == 1


def test_an_unreadable_run_timestamp_does_not_manufacture_an_absence(github):
    """A row we cannot date might be the one we are looking for, so keep searching."""
    correlation = "ab12cd34ef56"
    for index in range(lw.GITHUB_QUERY_PAGE_SIZE):
        github.add_unrelated_run(8000 + index, title="project-observation-{:032x}".format(index), created_at="???")
    github.add_run(correlation, 8999, "completed", "success")

    outcome, run = lw.find_run_by_correlation(correlation, created_at=_iso(30))

    assert outcome == lw.MATCH
    assert run["run_id"] == 8999


# --- 23: the Codex >50-run failure, across two real processes -------------------


def test_the_buried_run_codex_failure_across_two_processes_dispatches_once(watcher):
    """Process 1 sends and GitHub accepts; 60 newer runs appear; process 2 reconciles.

    Before the repair, process 2's single 50-run page did not contain the exact run, so it
    concluded the run did not exist and freed the payload for another dispatch. The search
    now reaches past the newer runs, finds the exact run, and the total is one dispatch.
    """
    state = lw.state_dir()

    # Process 1 dispatches; the run is accepted remotely and its id is not yet known.
    first = run_traced_in_fresh_process(
        state,
        DISPATCH_ONCE,
        runs=[],
        allow_dispatch=True,
        expect_returncode=0,
    )
    assert dispatch_count(first["calls"]) == 1
    assert first["result"]["outcome"] == lw.STATE_DISPATCHING

    receipt = lw.load_records()[0]
    correlation = first["result"]["correlation_id"]
    assert receipt["correlation_id"] == correlation
    assert receipt["run_id"] is None, "process 1 had not yet learned the run id"

    # Sixty unrelated runs land, so the exact run is no longer inside the newest page.
    runs = [
        {
            "databaseId": 9000 + index,
            "status": "completed",
            "conclusion": "success",
            "displayTitle": "project-observation-{:032x}".format(index + 1),
            "createdAt": _iso(1),
        }
        for index in range(60)
    ]
    runs.append(
        {
            "databaseId": 9500,
            "status": "queued",
            "conclusion": None,
            "displayTitle": RUN_NAME.format(correlation),
            "createdAt": _iso(5),
        }
    )

    second = run_traced_in_fresh_process(
        state,
        RECONCILE_BODY,
        runs=runs,
        allow_dispatch=False,
        expect_returncode=0,
    )

    assert dispatch_count(second["calls"]) == 0, "process 2 resent a payload GitHub already had"
    assert second["result"]["summary"][lw.STATE_ACCEPTED] == 1
    assert len(second["result"]["records"]) == 1
    assert second["result"]["records"][0]["run_id"] == 9500


DISPATCH_ONCE = """
from tests.watcher_support import build_observation, build_payload
outcome, record = lw.dispatch_observations(build_payload([build_observation()]))
print(json.dumps({"outcome": outcome, "correlation_id": record["correlation_id"]}))
"""