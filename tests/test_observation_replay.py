"""A delayed replay must not supersede newer evidence.

Replay protection here is *historical* and *project-scoped*, not adjacent. Comparing only
against the newest evidence event means A, B, then a late replay of A would be accepted as
fresh and would overwrite B's newer metadata — so the tests drive exactly that sequence
and read the event log afterwards.

Identity is built from evidence, never from the scan clock. That is what lets a project
legitimately return to a similar state later and still be recorded.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.events as events_log  # noqa: E402
import src.project_observation as writer  # noqa: E402
import src.project_state as project_state  # noqa: E402
import src.projects as registry  # noqa: E402
from src.observation_contract import observation_identity, project_scoped_identity  # noqa: E402
from tests.watcher_support import build_observation, build_payload, sandbox_projects  # noqa: E402

T1 = "2026-10-01T10:00:00+03:00"
T2 = "2026-10-02T10:00:00+03:00"
T3 = "2026-10-03T10:00:00+03:00"


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    sandbox_projects(tmp_path, monkeypatch)
    return tmp_path


def _apply(*observations, correlation="a" * 32) -> int:
    return writer.apply_payload(build_payload(observations, correlation))


def _evidence_events():
    return events_log.project_events("alpha", ("project_evidence",))


def _alpha_evidence(project_id="alpha"):
    return events_log.project_events(project_id, ("project_evidence",))


# --- 16: an adjacent duplicate is one event ------------------------------------


def test_the_same_observation_twice_produces_one_event():
    observation = build_observation(project_id="alpha", facts=["phase 2 drawing"], newest_evidence_at=T1)

    assert _apply(observation) == 0
    assert _apply(build_observation(project_id="alpha", facts=["phase 2 drawing"], newest_evidence_at=T1)) == 0

    assert len(_alpha_evidence()) == 1


def test_a_duplicate_is_reported_as_a_replay_and_writes_nothing_more():
    observation = build_observation(project_id="alpha", facts=["phase 2 drawing"], newest_evidence_at=T1)
    _apply(observation)
    before = _alpha_evidence()

    replay = build_observation(project_id="alpha", facts=["phase 2 drawing"], newest_evidence_at=T1)
    applied, message = writer.apply_observation(replay, registry.load_registry())

    assert applied is False
    assert "replay of already-applied observation" in message
    assert _alpha_evidence() == before


def test_the_same_observation_in_one_batch_is_applied_once():
    observation = build_observation(project_id="alpha", facts=["only once"], newest_evidence_at=T1)

    assert _apply(observation, build_observation(project_id="alpha", facts=["only once"], newest_evidence_at=T1)) == 0
    assert len(_alpha_evidence()) == 1


# --- 17: A -> B -> delayed old A ------------------------------------------------


def test_a_delayed_replay_of_a_after_b_is_rejected():
    observation_a = build_observation(project_id="alpha", facts=["phase 2 drawing"], newest_evidence_at=T1)
    observation_b = build_observation(project_id="alpha", facts=["phase 3 rendering"], newest_evidence_at=T2)
    assert observation_a["observation_digest"] != observation_b["observation_digest"]

    assert _apply(observation_a) == 0
    assert _apply(observation_b) == 0

    # A replayed late, long after B landed.
    delayed_a = build_observation(
        project_id="alpha",
        facts=["phase 2 drawing"],
        newest_evidence_at=T1,
        observed_at="2026-10-04T09:00:00+03:00",
    )
    applied, message = writer.apply_observation(delayed_a, registry.load_registry())

    assert applied is False
    assert "replay" in message

    events = _alpha_evidence()
    assert len(events) == 2, "the delayed replay appended a third event"
    assert events[-1]["facts"] == ["phase 3 rendering"], "the stale replay superseded newer evidence"
    assert project_state.load_state("alpha")["evidence"][-1].endswith("phase 3 rendering")


def test_a_delayed_replay_does_not_rewrite_a_second_project_either():
    beta = build_observation(project_id="beta", facts=["beta one"], newest_evidence_at=T1)
    _apply(beta)
    beta_later = build_observation(project_id="beta", facts=["beta two"], newest_evidence_at=T2)
    _apply(beta_later)

    delayed_beta = build_observation(
        project_id="beta", facts=["beta one"], newest_evidence_at=T1, observed_at="2026-10-05T09:00:00+03:00"
    )
    applied, _ = writer.apply_observation(delayed_beta, registry.load_registry())

    assert applied is False
    assert len(_alpha_evidence("beta")) == 2


# --- 18: A -> B -> genuinely new A ----------------------------------------------


def test_a_genuinely_newer_a_is_accepted_after_b():
    observation_a = build_observation(project_id="alpha", facts=["phase 2 drawing"], newest_evidence_at=T1)
    observation_b = build_observation(project_id="alpha", facts=["phase 3 rendering"], newest_evidence_at=T2)
    _apply(observation_a)
    _apply(observation_b)

    # Same facts as the original A, but the source's own evidence has moved on.
    newer_a = build_observation(
        project_id="alpha",
        facts=["phase 2 drawing"],
        newest_evidence_at=T3,
        observed_at="2026-10-04T09:00:00+03:00",
    )
    applied, message = writer.apply_observation(newer_a, registry.load_registry())

    assert applied is True
    assert newer_a["observation_digest"] != observation_a["observation_digest"]
    events = _alpha_evidence()
    assert len(events) == 3
    assert events[-1]["observation_digest"] == newer_a["observation_digest"]
    assert events[-1]["evidence_at"] == T3
    assert project_state.load_state("alpha")["evidence"][-1].endswith("phase 2 drawing")


def test_a_project_can_return_to_identical_facts_when_the_evidence_time_moves():
    """Identity is evidence identity, not "facts seen before"."""
    original = build_observation(project_id="alpha", facts=["status: blocked"], newest_evidence_at=T1)
    _apply(original)
    _apply(build_observation(project_id="alpha", facts=["status: shipped"], newest_evidence_at=T2))

    returned = build_observation(
        project_id="alpha", facts=["status: blocked"], newest_evidence_at=T3, observed_at="2026-10-06T09:00:00+03:00"
    )
    assert observation_identity(returned) != observation_identity(original)
    assert writer.apply_observation(returned, registry.load_registry())[0] is True


def test_identity_ignores_the_scan_clock():
    """Re-scanning unchanged evidence minutes later must not mint a new identity."""
    first = build_observation(
        project_id="alpha", facts=["same"], newest_evidence_at=T1, observed_at="2026-10-03T10:00:00+03:00"
    )
    second = build_observation(
        project_id="alpha", facts=["same"], newest_evidence_at=T1, observed_at="2026-10-03T10:30:00+03:00"
    )
    assert first["observation_digest"] == second["observation_digest"]


def test_identity_distinguishes_a_real_change_in_the_facts():
    changed = build_observation(project_id="alpha", facts=["different"], newest_evidence_at=T1)
    same = build_observation(project_id="alpha", facts=["same"], newest_evidence_at=T1)
    assert changed["observation_digest"] != same["observation_digest"]


# --- 19: project isolation ------------------------------------------------------


def test_the_same_digest_in_two_projects_does_not_deduplicate_across_them():
    alpha_observation = build_observation(project_id="alpha", facts=["identical"], newest_evidence_at=T1)
    assert _apply(alpha_observation) == 0

    # Force beta's observation to carry alpha's digest: the replay memory must still be
    # consulted per project, so beta's own evidence is not mistaken for alpha's replay.
    beta_observation = build_observation(project_id="beta", facts=["identical"], newest_evidence_at=T1)
    assert beta_observation["observation_digest"] != alpha_observation["observation_digest"]
    beta_observation["observation_digest"] = alpha_observation["observation_digest"]

    applied, _ = writer.apply_observation(beta_observation, registry.load_registry())

    assert applied is True
    assert len(_alpha_evidence("beta")) == 1
    assert len(_alpha_evidence("alpha")) == 1


def test_applied_identities_are_looked_up_per_project():
    alpha_observation = build_observation(project_id="alpha", facts=["isolated"], newest_evidence_at=T1)
    _apply(alpha_observation)
    digest = alpha_observation["observation_digest"]

    assert events_log.applied_observation_identities("alpha") == {digest}
    assert events_log.applied_observation_identities("beta") == set()
    assert events_log.has_applied_observation("alpha", digest) is True
    assert events_log.has_applied_observation("beta", digest) is False


def test_the_deduplication_key_is_project_scoped():
    alpha_observation = build_observation(project_id="alpha", facts=["shared"], newest_evidence_at=T1)
    beta_observation = dict(alpha_observation, project_id="beta")
    assert project_scoped_identity(alpha_observation) != project_scoped_identity(beta_observation)


def test_an_adjacent_duplicate_of_a_different_project_is_still_applied():
    """Per-project comparison: beta's first observation is not alpha's duplicate."""
    _apply(build_observation(project_id="alpha", facts=["x"], newest_evidence_at=T1))
    assert _apply(build_observation(project_id="beta", facts=["x"], newest_evidence_at=T1)) == 0
    assert len(_alpha_evidence("alpha")) == 1
    assert len(_alpha_evidence("beta")) == 1
