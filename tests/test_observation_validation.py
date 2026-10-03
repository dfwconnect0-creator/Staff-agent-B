"""The entire batch is validated before the entire batch is applied.

Every test asserts *zero mutation* on rejection, not just a non-zero exit: no event
appended, no Level 2 state file, no Level 3 portfolio file. A validation rule that only
logs a warning after the first observation was already written is not a validation rule.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.events as events_log  # noqa: E402
import src.project_observation as writer  # noqa: E402
import src.project_state as project_state  # noqa: E402
import src.portfolio as portfolio  # noqa: E402
import src.projects as registry  # noqa: E402
from src.observation_contract import MAX_OBSERVATIONS  # noqa: E402
from tests.watcher_support import (  # noqa: E402
    build_observation,
    build_payload,
    reseal,
    sandbox_projects,
)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    sandbox_projects(tmp_path, monkeypatch)
    return tmp_path


@pytest.fixture
def nothing_written(tmp_path):
    """The observable evidence that a rejected payload left no trace at all."""

    class Check:
        def __call__(self):
            assert events_log.read_events() == [], "an event was appended by a rejected payload"
            assert not (tmp_path / "projects").exists(), "a Level 2 state file was written"
            assert not (tmp_path / "portfolio.md").exists(), "the Level 3 portfolio was written"
            return True

    return Check()


def _apply(payload) -> int:
    return writer.apply_payload(payload)


def _problems(payload) -> list[str]:
    return writer.validate_payload(payload, registry.load_registry())


def _reject(payload, nothing_written, expected_fragment):
    """Every rejection path: non-zero exit, an explanation, and no mutation."""
    problems = _problems(payload)
    assert any(expected_fragment in problem for problem in problems), (
        f"expected a problem mentioning {expected_fragment!r}, got {problems}"
    )
    assert _apply(payload) == 1
    nothing_written()


# --- 20: schema_version ----------------------------------------------------------


def test_a_wrong_schema_version_rejects_the_batch(nothing_written):
    payload = build_payload([build_observation()])
    payload["schema_version"] = 2
    _reject(payload, nothing_written, "payload.schema_version")


def test_a_missing_schema_version_rejects_the_batch(nothing_written):
    payload = build_payload([build_observation()])
    del payload["schema_version"]
    _reject(payload, nothing_written, "payload.schema_version: required")


def test_a_string_schema_version_rejects_the_batch(nothing_written):
    payload = build_payload([build_observation()])
    payload["schema_version"] = "1"
    _reject(payload, nothing_written, "expected the integer 1")


def test_a_boolean_schema_version_is_not_accepted_as_one(nothing_written):
    payload = build_payload([build_observation()])
    payload["schema_version"] = True
    _reject(payload, nothing_written, "expected the integer 1")


# --- correlation_id --------------------------------------------------------------


def test_a_missing_correlation_id_rejects_the_batch(nothing_written):
    payload = build_payload([build_observation()])
    del payload["correlation_id"]
    _reject(payload, nothing_written, "payload.correlation_id: required")


@pytest.mark.parametrize("bad", ["", "not-hex-at-all", "ABCDEF0123456789", 12345, "a" * 65])
def test_a_malformed_correlation_id_rejects_the_batch(bad, nothing_written):
    payload = build_payload([build_observation()])
    payload["correlation_id"] = bad
    _reject(payload, nothing_written, "payload.correlation_id")


# --- 21: `fresh` must be a real boolean -----------------------------------------


def test_the_string_false_is_not_accepted_as_a_boolean(nothing_written):
    observation = build_observation()
    observation["fresh"] = "false"
    _reject(build_payload([observation]), nothing_written, "expected a real boolean")


@pytest.mark.parametrize("bad", ["true", 0, 1, "False", [], None])
def test_every_non_boolean_fresh_is_rejected(bad, nothing_written):
    observation = build_observation()
    observation["fresh"] = bad
    _reject(build_payload([observation]), nothing_written, "expected a real boolean")


def test_a_string_false_does_not_write_high_confidence(tmp_path):
    observation = build_observation()
    observation["fresh"] = "false"
    assert _apply(build_payload([observation])) == 1
    assert not (tmp_path / "projects").exists()


# --- 22: missing required fields -------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "project_id",
        "source_type",
        "fresh",
        "reason",
        "facts",
        "evidence_summary",
        "observed_at",
        "newest_evidence_at",
        "observation_digest",
    ],
)
def test_a_missing_required_field_rejects_the_batch(field, nothing_written):
    observation = build_observation()
    del observation[field]
    _reject(build_payload([observation]), nothing_written, f".{field}: required")


def test_a_missing_project_id_rejects_the_batch(nothing_written):
    observation = build_observation()
    observation.pop("project_id")
    _reject(build_payload([observation]), nothing_written, "project_id")


def test_an_empty_project_id_rejects_the_batch(nothing_written):
    observation = build_observation()
    observation["project_id"] = "   "
    _reject(build_payload([observation]), nothing_written, "non-empty string")


def test_an_unexpected_field_rejects_the_batch(nothing_written):
    observation = build_observation()
    observation["phase"] = "validating"
    _reject(build_payload([observation]), nothing_written, "unexpected field 'phase'")


def test_an_empty_observations_list_rejects_the_batch(nothing_written):
    _reject({"schema_version": 1, "correlation_id": "a" * 32, "observations": []}, nothing_written, "at least 1")


# --- 23: `facts` must really be a list of bounded strings ------------------------


@pytest.mark.parametrize("bad", ["a string", 42, {"a": 1}, None, 3.5])
def test_facts_of_the_wrong_type_reject_the_batch(bad, nothing_written):
    observation = build_observation()
    observation["facts"] = bad
    _reject(build_payload([observation]), nothing_written, "facts: expected a list")


def test_a_non_string_fact_rejects_the_batch(nothing_written):
    observation = build_observation()
    observation["facts"] = ["fine", 7]
    _reject(build_payload([observation]), nothing_written, "facts[1]: expected a string")


def test_too_many_facts_reject_the_batch(nothing_written):
    observation = build_observation()
    observation["facts"] = [f"fact {i}" for i in range(21)]
    _reject(build_payload([observation]), nothing_written, "exceeds the maximum of 20")


def test_an_overlong_fact_rejects_the_batch(nothing_written):
    observation = build_observation()
    observation["facts"] = ["x" * 301]
    _reject(build_payload([observation]), nothing_written, "longer than 300")


def test_an_overlong_reason_rejects_the_batch(nothing_written):
    observation = build_observation()
    observation["reason"] = "r" * 501
    _reject(build_payload([observation]), nothing_written, "longer than 500")


def test_an_overlong_evidence_summary_rejects_the_batch(nothing_written):
    observation = build_observation()
    observation["evidence_summary"] = "s" * 301
    _reject(build_payload([observation]), nothing_written, "longer than 300")


def test_a_reason_of_the_wrong_type_rejects_the_batch(nothing_written):
    observation = build_observation()
    observation["reason"] = 12
    _reject(build_payload([observation]), nothing_written, "reason: expected a string or null")


def test_an_explicit_null_reason_is_allowed(tmp_path):
    observation = reseal({**build_observation(), "reason": None})
    assert _apply(build_payload([observation])) == 0
    assert events_log.project_events("alpha", ("project_evidence",))[0]["reason"] == ""


# --- 24: timestamps ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["yesterday", "2026-13-45T99:99:99+03:00", "", None, 1750000000, "2026-10-03"])
def test_an_invalid_observed_at_rejects_the_batch(bad, nothing_written):
    observation = build_observation()
    observation["observed_at"] = bad
    _reject(build_payload([observation]), nothing_written, "observed_at")


def test_a_naive_observed_at_is_rejected(nothing_written):
    """An ambiguous clock reading cannot be ordered against other evidence."""
    observation = build_observation()
    observation["observed_at"] = "2026-10-03T12:00:00"
    _reject(build_payload([observation]), nothing_written, "observed_at")


@pytest.mark.parametrize("bad", ["last tuesday", "2026-10-03T12:00:00", 42])
def test_an_invalid_evidence_timestamp_rejects_the_batch(bad, nothing_written):
    observation = build_observation()
    observation["newest_evidence_at"] = bad
    _reject(build_payload([observation]), nothing_written, "newest_evidence_at")


@pytest.mark.parametrize("allowed", [None, "", "2026-10-01T10:00:00+03:00"])
def test_an_allowed_empty_evidence_timestamp_is_accepted(allowed, tmp_path):
    observation = reseal({**build_observation(fresh=False), "newest_evidence_at": allowed})
    assert _apply(build_payload([observation])) == 0
    assert len(events_log.project_events("alpha", ("project_evidence",))) == 1


# --- 25: the observation digest ---------------------------------------------------


@pytest.mark.parametrize("bad", ["", "nothex", "ABCDEF0123456789", 123, "a" * 15, "a" * 17, None])
def test_a_malformed_digest_rejects_the_batch(bad, nothing_written):
    observation = build_observation()
    observation["observation_digest"] = bad
    _reject(build_payload([observation]), nothing_written, "observation_digest")


def test_a_digest_that_does_not_describe_the_observation_rejects_the_batch(nothing_written):
    observation = build_observation(facts=["honest"])
    observation["facts"] = ["tampered with after the digest was computed"]
    _reject(build_payload([observation]), nothing_written, "does not describe this observation")


def test_a_digest_of_a_different_observation_rejects_the_batch(nothing_written):
    honest = build_observation(facts=["honest"])
    forged = build_observation(facts=["different"])
    forged["observation_digest"] = honest["observation_digest"]
    _reject(build_payload([forged]), nothing_written, "does not describe this observation")


# --- 26: unknown project rejects the whole batch ----------------------------------


def test_an_unknown_project_rejects_the_whole_batch(nothing_written):
    observation = build_observation(project_id="does-not-exist")
    payload = build_payload([build_observation(project_id="alpha"), observation])
    _reject(payload, nothing_written, "unknown project 'does-not-exist'")


def test_an_unknown_project_is_not_merely_logged_and_still_succeeds():
    payload = build_payload([build_observation(project_id="does-not-exist")])
    assert _apply(payload) == 1
    assert events_log.read_events() == []


def test_an_inactive_project_rejects_the_batch(nothing_written):
    _reject(
        build_payload([build_observation(project_id="gamma")]),
        nothing_written,
        "is paused, not active",
    )


def test_a_source_type_the_registry_does_not_declare_rejects_the_batch(nothing_written):
    observation = build_observation(project_id="alpha", source_type="repo")
    _reject(build_payload([observation]), nothing_written, "registry declares 'local_file'")


@pytest.mark.parametrize("bad", ["database", "", None, 5])
def test_a_source_type_outside_the_allowed_set_rejects_the_batch(bad, nothing_written):
    observation = build_observation()
    observation["source_type"] = bad
    _reject(build_payload([observation]), nothing_written, "source_type")


# --- 27: the batch bound ----------------------------------------------------------


def test_fifty_one_observations_reject_the_whole_batch(nothing_written):
    observations = [build_observation(project_id="alpha") for _ in range(51)]
    payload = build_payload(observations)
    assert len(payload["observations"]) == MAX_OBSERVATIONS + 1

    problems = _problems(payload)
    assert any("exceeds the maximum of 50" in problem for problem in problems)
    assert _apply(payload) == 1
    nothing_written()


def test_fifty_one_observations_are_not_silently_truncated(nothing_written):
    """Truncating would look like success while quietly dropping evidence."""
    observations = [build_observation(project_id="alpha", facts=[f"fact {i}"]) for i in range(51)]
    assert _apply(build_payload(observations)) == 1
    nothing_written()


def test_exactly_fifty_observations_are_accepted(tmp_path):
    """The bound is inclusive: fifty distinct observations all land."""
    observations = [build_observation(project_id="alpha", facts=[f"fact {i}"]) for i in range(MAX_OBSERVATIONS)]
    assert _apply(build_payload(observations)) == 0
    assert len(events_log.project_events("alpha", ("project_evidence",))) == MAX_OBSERVATIONS


# --- 28: valid first + invalid second -> zero mutation -----------------------------


def test_a_valid_first_observation_does_not_survive_an_invalid_second(nothing_written):
    valid = build_observation(project_id="alpha", facts=["this one is fine"])
    invalid = build_observation(project_id="beta")
    invalid["fresh"] = "false"

    payload = build_payload([valid, invalid])
    problems = _problems(payload)

    assert not any("observations[0]" in problem for problem in problems)
    assert any("observations[1].fresh" in problem for problem in problems)
    assert _apply(payload) == 1
    nothing_written()


def test_a_valid_first_observation_does_not_survive_an_unknown_second(nothing_written):
    valid = build_observation(project_id="alpha")
    payload = build_payload([valid, build_observation(project_id="nope")])
    assert _apply(payload) == 1
    nothing_written()


def test_a_valid_first_observation_does_not_survive_an_invalid_timestamp_second(nothing_written):
    valid = build_observation(project_id="alpha")
    second = build_observation(project_id="beta")
    second["observed_at"] = "not a time"
    assert _apply(build_payload([valid, second])) == 1
    nothing_written()


def test_a_valid_first_observation_does_not_survive_an_over_bound_batch(nothing_written):
    """The count check runs before any per-observation work, so nothing is half-applied."""
    observations = [build_observation(project_id="alpha") for _ in range(MAX_OBSERVATIONS + 1)]
    assert _apply(build_payload(observations)) == 1
    nothing_written()


def test_the_malformed_payload_itself_is_rejected(nothing_written):
    for malformed in ["not a dict", 42, None, ["a"], True]:
        assert _apply(malformed) == 1
    nothing_written()


def test_an_unparseable_payload_exits_non_zero_without_writing(monkeypatch, nothing_written):
    monkeypatch.setenv("OBSERVATION_PAYLOAD", "{not json")
    monkeypatch.setattr(sys, "argv", ["project_observation", "apply"])
    assert writer.main() == 1
    nothing_written()


def test_a_missing_payload_exits_non_zero_without_writing(monkeypatch, nothing_written):
    monkeypatch.delenv("OBSERVATION_PAYLOAD", raising=False)
    monkeypatch.delenv("PAYLOAD", raising=False)
    monkeypatch.setattr(sys, "argv", ["project_observation", "apply"])
    assert writer.main() == 1
    nothing_written()


# --- 29: a valid batch applies normally --------------------------------------------


def test_a_valid_batch_applies_normally(tmp_path):
    payload = build_payload(
        [
            build_observation(project_id="alpha", facts=["alpha is alive"]),
            build_observation(project_id="beta", facts=["beta is alive"]),
        ]
    )
    assert _apply(payload) == 0

    events = events_log.read_events()
    assert [e["project_id"] for e in events] == ["alpha", "beta"]
    assert all(e["event_type"] == "project_evidence" for e in events)
    assert all(e["observation_digest"] for e in events)
    assert project_state.load_state("alpha")["evidence"]
    assert project_state.load_state("beta")["evidence"]
    assert portfolio.load_state()["project_count"] == "2"


def test_a_valid_batch_reads_evidence_time_and_scan_time_separately(tmp_path):
    observation = build_observation(
        project_id="alpha",
        facts=["Status: done"],
        newest_evidence_at="2020-01-01T00:00:00+03:00",
        observed_at="2026-10-03T12:00:00+03:00",
    )
    assert _apply(build_payload([observation])) == 0

    event = events_log.project_events("alpha", ("project_evidence",))[0]
    assert event["observed_at"] == "2026-10-03T12:00:00+03:00"
    assert event["evidence_at"] == "2020-01-01T00:00:00+03:00"
    # A two-year-old source is not recorded as current just because we scanned it now.
    assert project_state.load_state("alpha")["source_freshness"] == "very_stale"


def test_a_valid_batch_reports_its_correlation_id(tmp_path, capsys):
    payload = build_payload([build_observation()], correlation_id="b" * 32)
    assert _apply(payload) == 0
    assert any("b" * 32 in line for line in capsys.readouterr().err.splitlines())


def test_a_valid_batch_of_the_watcher_shape_is_accepted(tmp_path):
    """What `local_watcher.make_observation` produces must satisfy the writer's rules."""
    from src import local_watcher as lw
    import src.project_sources as sources

    report = sources.inspect("alpha", "local_file", str(tmp_path / "alpha.md"))
    observation = lw.make_observation("alpha", report)
    assert _apply(build_payload([observation])) == 0
    assert len(events_log.project_events("alpha", ("project_evidence",))) == 1


def test_json_round_trips_through_the_environment_without_changing_the_verdict(tmp_path, monkeypatch):
    payload = build_payload([build_observation()])
    monkeypatch.setenv("OBSERVATION_PAYLOAD", json.dumps(payload))
    monkeypatch.setattr(sys, "argv", ["project_observation", "apply"])
    assert writer.main() == 0
    assert len(events_log.project_events("alpha", ("project_evidence",))) == 1
