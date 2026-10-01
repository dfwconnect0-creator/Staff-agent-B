"""Tests for the Version 2 project layer: registry, state, sources, routing, portfolio.

Every test here is deterministic and offline. Local project directories are pointed at
``tmp_path`` fixtures, never at the user's real projects, so a test run cannot read —
or worse rewrite — anything on the real disk.
"""

import io
import json
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

import src.events as events_log
import src.ingest_replies as ingest
import src.memory.episodic as episodic
import src.portfolio as portfolio
import src.portfolio_briefing as portfolio_briefing
import src.project_routing as routing
import src.project_sources as sources
import src.project_state as project_state
import src.project_update as project_update
import src.projects as registry
import src.state as state


REGISTRY = """# Project Registry

## Projects

### alpha

- id: alpha
- name: Alpha Project
- aliases: alpha, alpha app
- tracking: active
- evidence_source: event_only
- evidence_path: none
- refreshable_in_actions: no
- onboarded_at: 2026-10-01

### beta

- id: beta
- name: Beta Project
- aliases: beta
- tracking: active
- evidence_source: event_only
- evidence_path: none
- refreshable_in_actions: no
- onboarded_at: 2026-10-01

### gamma

- id: gamma
- name: Gamma Project
- aliases: gamma
- tracking: paused
- evidence_source: event_only
- evidence_path: none
- refreshable_in_actions: no
- onboarded_at: 2026-10-01
"""


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Point every Version 2 path at tmp_path. The repo's real files are untouchable."""
    monkeypatch.setattr(episodic, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(state, "DEFAULT_STATE_PATH", tmp_path / "current_state.md")
    monkeypatch.setattr(project_state, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(registry, "DEFAULT_REGISTRY_PATH", tmp_path / "projects.md")
    monkeypatch.setattr(portfolio, "PORTFOLIO_STATE_PATH", tmp_path / "portfolio.md")
    (tmp_path / "projects.md").write_text(REGISTRY, encoding="utf-8")
    (tmp_path / "current_state.md").write_text(
        "target_output: x\nnext_action: Verify alpha.\nblocker: none\n"
        "accountability_source: telegram\nlast_evidence: none\nconfidence: unknown\n"
        "updated_at: 2026-10-01T00:00:00+03:00\n\n## Checkpoints\n\n_none_\n\n## Transitions\n\n_none_\n",
        encoding="utf-8",
    )
    return tmp_path


@contextmanager
def _stdin(payload: str):
    """Feed JSON to a function that reads stdin."""
    original = sys.stdin
    sys.stdin = io.StringIO(payload)
    try:
        yield
    finally:
        sys.stdin = original


def seed_project(project_id: str, **fields) -> str:
    catalog = registry.load_registry()
    entry = catalog.get(project_id)
    state = project_state.load_state(project_id)
    state["name"] = entry.name if entry else project_id
    state.update(fields)
    project_state.write_state(project_id, state)
    return project_id


def seed_healthy(project_id: str, **fields) -> str:
    """Seed a project that produces no intervention, then apply the specific overrides."""
    defaults = {
        "target_output": "Reach the stated target",
        "required_transition": "Do the next thing",
        "phase": "validating",
        "artifact_state": "present",
        "outcome_quality": "validated",
        "externalization": "published",
        "source_freshness": "current",
        "scope": "active",
    }
    defaults.update(fields)
    return seed_project(project_id, **defaults)


# --- Test 1: registry parsing and self-consistency -----------------------------


def test_registry_parses_every_declared_project():
    catalog = registry.load_registry()
    assert set(catalog) == {"alpha", "beta", "gamma"}
    assert catalog["alpha"].name == "Alpha Project"
    assert catalog["alpha"].aliases == ["alpha", "alpha app"]
    assert catalog["gamma"].tracking == "paused"


def test_registry_reports_no_problems_for_a_coherent_file():
    assert registry.validate_registry(registry.load_registry()) == []


def test_registry_flags_an_alias_claimed_by_two_projects():
    catalog = registry.load_registry()
    catalog["beta"].aliases = ["beta", "alpha app"]
    problems = registry.validate_registry(catalog)
    assert any("alpha app" in p and "ambiguous" in p for p in problems)


def test_registry_flags_a_project_with_no_alias():
    catalog = registry.load_registry()
    catalog["alpha"].aliases = []
    assert any("no aliases" in p for p in registry.validate_registry(catalog))


def test_registry_flags_a_project_with_no_name_and_no_alias():
    """A brand-new project whose name equals its id still routes, because the id is an alias."""
    catalog = registry.load_registry()
    catalog["alpha"].name = "alpha"
    catalog["alpha"].aliases = []
    assert any("no aliases" in p for p in registry.validate_registry(catalog))
    index = registry.alias_index(catalog)
    assert index["alpha"] == ["alpha"]


# --- Test 2: project state round-trips and stays separate from the loop --------


def test_project_state_round_trips_every_field():
    seed_project(
        "alpha",
        target_output="Ship the thing",
        current_state="Half done",
        required_transition="Write the test",
        blocker="Waiting on credentials",
        phase="blocked",
        work_type="build",
        artifact_state="present",
        outcome_quality="unvalidated",
        externalization="internal_only",
        accountability_source="file",
        scope="active",
        source_freshness="current",
    )
    loaded = project_state.load_state("alpha")
    assert loaded["target_output"] == "Ship the thing"
    assert loaded["phase"] == "blocked"
    assert loaded["blocker"] == "Waiting on credentials"
    assert loaded["artifact_state"] == "present"


def test_project_state_lives_in_its_own_file_not_the_loop_state(tmp_path):
    seed_project("alpha", phase="blocked")
    project_state.write_state("alpha", project_state.load_state("alpha"))
    assert (tmp_path / "projects" / "alpha" / "current_state.md").exists()
    assert "phase: blocked" not in (tmp_path / "current_state.md").read_text()


def test_unknown_project_loads_as_unknown_rather_than_raising():
    loaded = project_state.load_state("does-not-exist")
    assert loaded["phase"] == "unknown"
    assert loaded["target_output"] == "unknown"


def test_project_state_version_changes_when_a_project_moves():
    seed_project("alpha", phase="validating")
    before = project_state.state_version(project_state.load_state("alpha"))
    seed_project("alpha", phase="blocked")
    after = project_state.state_version(project_state.load_state("alpha"))
    assert before != after


def test_portfolio_version_is_stable_for_identical_project_states():
    a = project_state.load_state("alpha")
    b = project_state.load_state("beta")
    assert project_state.portfolio_version({"alpha": a, "beta": b}) == project_state.portfolio_version(
        {"beta": b, "alpha": a}
    )


# --- Test 3: evidence sources are read honestly --------------------------------


def test_inspect_repo_reports_a_real_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "first"], check=True)

    report = sources.inspect("x", "repo", str(repo))
    assert report["fresh"] is True
    assert any("first" in fact for fact in report["facts"])


def test_inspect_repo_says_so_when_there_is_no_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    report = sources.inspect("x", "repo", str(plain))
    assert report["fresh"] is False
    assert "not a git repository" in report["reason"]


def test_inspect_repo_says_so_for_a_missing_path(tmp_path):
    report = sources.inspect("x", "repo", str(tmp_path / "nope"))
    assert report["fresh"] is False
    assert "does not exist" in report["reason"]


def test_inspect_repo_records_a_repository_with_no_commits(tmp_path):
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    report = sources.inspect("x", "repo", str(repo))
    assert report["reason"] == "repository has no commits yet; no project history to read"
    assert "no commits yet" in report["facts"]


def test_inspect_event_only_project_reports_no_source_rather_than_success():
    report = sources.inspect("alpha", "event_only", "none")
    assert report["fresh"] is False
    assert "no automatic source" in report["reason"]


def test_inspect_local_file_quotes_the_stated_status(tmp_path):
    f = tmp_path / "STATUS.md"
    f.write_text("# Project\n\n**Status:** implemented, awaiting phone test\n", encoding="utf-8")
    report = sources.inspect("x", "local_file", str(f))
    assert report["fresh"] is True
    assert report["facts"] == ["Status: implemented, awaiting phone test"]


def test_freshness_reflects_age_without_implying_health():
    assert sources.freshness(sources.now_cairo()) == "current"
    assert sources.freshness("2026-01-01T00:00:00+03:00") == "very_stale"
    assert sources.freshness("not a timestamp") == "unknown"


# --- Test 4: conservative reply routing ----------------------------------------


def test_reply_naming_one_project_routes_to_it():
    index = registry.alias_index(registry.load_registry())
    decision = routing.resolve_project("Alpha works now", index)
    assert decision["project_id"] == "alpha"
    assert decision["matched_alias"] == "alpha"


def test_reply_naming_nothing_routes_to_nothing():
    index = registry.alias_index(registry.load_registry())
    decision = routing.resolve_project("the live feedback loop worked", index)
    assert decision["project_id"] is None
    assert "no explicit project alias" in decision["reason"]


def test_reply_naming_two_projects_is_ambiguous_not_pick_one():
    index = registry.alias_index(registry.load_registry())
    decision = routing.resolve_project("alpha and beta both need attention", index)
    assert decision["project_id"] is None
    assert decision["candidates"] == ["alpha", "beta"]


def test_routing_does_not_match_a_substring():
    index = registry.alias_index(registry.load_registry())
    assert routing.resolve_project("alphabet soup", index)["project_id"] is None


def test_a_negative_signal_outranks_a_positive_one_in_the_same_sentence():
    index = registry.alias_index(registry.load_registry())
    updates = routing.derive_field_updates("alpha is not verified yet", "alpha", index)
    assert updates[0]["polarity"] == "negative"
    assert updates[0]["value"] == "blocked"


def test_only_sentences_naming_the_project_change_its_state():
    index = registry.alias_index(registry.load_registry())
    text = "alpha works. beta is still broken."
    updates = routing.derive_field_updates(text, "alpha", index)
    assert [u["quote"] for u in updates] == ["alpha works"]


# --- Test 5: a routed reply updates that project and not the others -------------


def _ingest(text: str, message_id: int) -> dict:
    update = {
        "update_id": message_id,
        "message_id": message_id + 1,
        "text": text,
        "timestamp_cairo": "2026-10-01 18:00",
        "reply_to_message_id": None,
    }
    return ingest.ingest_update(update)


def test_a_routed_reply_changes_the_named_projects_phase():
    seed_project("alpha", phase="validating")
    seed_project("beta", phase="building")
    event = _ingest("alpha is still broken", 900)
    routed = ingest.apply_project_routing([event])
    assert routed == 1
    assert project_state.load_state("alpha")["phase"] == "blocked"
    assert project_state.load_state("beta")["phase"] == "building"


def test_an_ambiguous_reply_changes_no_project():
    seed_project("alpha", phase="validating")
    seed_project("beta", phase="building")
    event = _ingest("alpha and beta are both broken", 901)
    assert ingest.apply_project_routing([event]) == 0
    assert project_state.load_state("alpha")["phase"] == "validating"
    assert project_state.load_state("beta")["phase"] == "building"


def test_an_unrouted_reply_is_still_stored_as_a_reply():
    event = _ingest("what do you mean", 902)
    assert event["event_type"] == "user_reply"
    assert ingest.apply_project_routing([event]) == 0
    stored = [e for e in events_log.read_events() if e["event_type"] == "user_reply"]
    assert any(e["text"] == "what do you mean" for e in stored)


def test_routing_records_the_reply_it_acted_on():
    event = _ingest("alpha now works", 903)
    routed = ingest.apply_project_routing([event])
    evidence = [e for e in events_log.read_events() if e["event_type"] == "project_evidence"]
    assert any(e.get("reply_event_id") == event["event_id"] for e in evidence)
    assert routed == 1


# --- Test 6: the portfolio decides, or stays silent ---------------------------


def test_a_blocked_project_becomes_the_portfolio_attention():
    seed_project("alpha", phase="blocked", blocker="No credentials", required_transition="Add credentials")
    seed_project("beta", phase="validating", required_transition="Run the retest")
    states = portfolio.load_states()
    decision = portfolio.decide(states)
    assert decision["attention_project"] == "alpha"
    assert "Add credentials" in decision["required_action"]


def test_paused_projects_are_not_escalated_for_staleness():
    seed_healthy("alpha", source_freshness="very_stale", scope="paused")
    seed_healthy("beta")
    decision = portfolio.decide(portfolio.load_states())
    assert decision["rule"] != "active_project_evidence_is_stale"


def test_an_active_stale_project_is_flagged():
    seed_healthy("alpha", source_freshness="very_stale")
    seed_healthy("beta")
    decision = portfolio.decide(portfolio.load_states())
    assert decision["rule"] == "active_project_evidence_is_stale"
    assert decision["attention_project"] == "alpha"


def test_a_healthy_portfolio_says_no_intervention_is_needed():
    seed_healthy("alpha")
    seed_healthy("beta", phase="building")
    decision = portfolio.decide(portfolio.load_states())
    assert decision["rule"] == "none"
    assert decision["required_action"] == portfolio.NO_INTERVENTION


def test_a_recorded_constraint_alone_does_not_make_a_project_blocked():
    """Every project has some constraint; treating any of them as a block flattens the portfolio."""
    seed_healthy("alpha", blocker="Needs a phone to test")
    seed_healthy("beta", blocker="Nothing")
    built = portfolio.build_state(portfolio.load_states())
    assert built["blocked_count"] == "0"
    assert built["silence_is_correct"] == "yes"


def test_a_project_with_no_target_is_flagged_below_a_hard_block():
    seed_project("alpha", phase="blocked", blocker="Nothing", required_transition="Unblock")
    seed_project("beta", phase="validating", target_output="")
    decision = portfolio.decide(portfolio.load_states())
    assert decision["attention_project"] == "alpha"


def test_an_unexternalized_artifact_outranks_a_missing_target():
    seed_healthy("alpha", phase="delivering", externalization="internal_only")
    seed_project("beta", phase="validating", target_output="", scope="active")
    decision = portfolio.decide(portfolio.load_states())
    assert decision["rule"] == "finished_artifact_never_left_the_machine"


def test_ties_break_the_same_way_every_run():
    for _ in range(3):
        seed_project("alpha", phase="blocked", blocker="x", required_transition="do alpha")
        seed_project("beta", phase="blocked", blocker="y", required_transition="do beta")
        decision = portfolio.decide(portfolio.load_states())
        assert decision["attention_project"] == "alpha"


def test_portfolio_state_file_round_trips():
    portfolio.write_state(portfolio.build_state(portfolio.load_states()))
    loaded = portfolio.load_state()
    assert loaded["attention_project"] in {"alpha", "beta", "gamma", "none"}


# --- Test 7: the portfolio briefing is a short rendering of decided state ------


def test_briefing_names_the_attention_project_and_its_transition():
    seed_project("alpha", phase="blocked", blocker="No credentials", required_transition="Add credentials")
    catalog = registry.load_registry()
    text = portfolio_briefing.render(portfolio.load_states(), catalog)
    assert "Add credentials" in text
    assert "Alpha Project" in text
    assert len(text.splitlines()) < 20


def test_briefing_says_so_explicitly_when_silence_is_correct():
    seed_healthy("alpha")
    text = portfolio_briefing.render(portfolio.load_states(), registry.load_registry())
    assert "No intervention needed today." in text


def test_briefing_never_invents_a_project_that_is_not_registered():
    seed_project("delta", phase="building", target_output="unregistered", name="Delta")
    catalog = registry.load_registry()
    states = {"alpha": seed_healthy("alpha") and project_state.load_state("alpha")}
    text = portfolio_briefing.render(states, catalog)
    assert "delta" not in text.lower()


# --- Test 8: project_update is the only writer, and it is evented --------------


def test_refresh_appends_evidence_before_writing_state():
    catalog = registry.load_registry()
    report = sources.inspect("alpha", catalog["alpha"].evidence_source, catalog["alpha"].evidence_path)
    event = project_update.record_evidence("alpha", report, catalog["alpha"])
    evidence_events = [e for e in events_log.read_events() if e["event_type"] == "project_evidence"]
    assert event["event_id"] in [e["event_id"] for e in evidence_events]
    assert project_state.load_state("alpha")["last_evidence"].startswith(event["event_id"])


def test_onboard_records_a_change_as_an_event():
    with _stdin(json.dumps({"phase": "blocked", "blocker": "No credentials"})):
        assert project_update.onboard("alpha") == 0
    assert project_state.load_state("alpha")["phase"] == "blocked"
    assert any(
        e["event_type"] == "project_evidence" and e.get("evidence_source") == "onboarding"
        for e in events_log.read_events()
    )


def test_onboard_refuses_an_unknown_field():
    with _stdin(json.dumps({"phase": "blocked", "not_a_field": "x"})):
        assert project_update.onboard("alpha") == 1


def test_onboard_refuses_an_unregistered_project():
    with _stdin(json.dumps({"phase": "blocked"})):
        assert project_update.onboard("not-registered") == 1


def test_onboard_does_not_change_the_loop_state_file(tmp_path):
    before = (tmp_path / "current_state.md").read_text()
    with _stdin(json.dumps({"phase": "blocked"})):
        project_update.onboard("alpha")
    assert (tmp_path / "current_state.md").read_text() == before


# --- Test 9: the event log stays backward compatible --------------------------


def test_version1_event_types_still_append():
    for event_type in ("briefing_sent", "briefing_delivered", "user_reply", "state_update"):
        assert events_log.append_event(event_type).get("event_id")


def test_an_unknown_event_type_is_still_rejected():
    with pytest.raises(ValueError):
        events_log.append_event("not_a_type")


def test_event_ids_stay_positional_across_new_types():
    first = events_log.append_event("project_evidence", project_id="alpha")
    second = events_log.append_event("user_reply", text="x", processed=False)
    assert int(second["event_id"].split("_")[1]) == int(first["event_id"].split("_")[1]) + 1


def test_a_reply_consumed_by_a_project_is_marked_processed():
    event = events_log.append_event("user_reply", processed=False, text="x")
    events_log.append_event("project_state_update", project_id="alpha", consumes=[event["event_id"]])
    stored = [e for e in events_log.read_events() if e["event_id"] == event["event_id"]]
    assert stored[0]["processed"] is True


def test_a_reply_consumed_by_the_loop_is_still_marked_processed():
    event = events_log.append_event("user_reply", processed=False, text="x")
    events_log.mark_processed([event["event_id"]], [], "v1")
    stored = [e for e in events_log.read_events() if e["event_id"] == event["event_id"]]
    assert stored[0]["processed"] is True


def test_project_events_are_scoped_to_their_project():
    events_log.append_event("project_evidence", project_id="alpha", facts=["a"])
    events_log.append_event("project_evidence", project_id="beta", facts=["b"])
    assert len(events_log.project_events("alpha")) == 1
    assert len(events_log.project_events("beta")) == 1


def test_a_version1_briefing_defaults_to_the_staff_agent_scope():
    assert events_log.briefing_scope({"event_type": "briefing_delivered"}) == "staff-agent"
    assert events_log.briefing_scope({"briefing_scope": "portfolio"}) == "portfolio"


def test_a_reply_to_a_portfolio_briefing_resolves_its_scope():
    events_log.append_event("briefing_sent", briefing_id="briefing_20261001_001", briefing_scope="portfolio")
    events_log.append_event("briefing_delivered", briefing_id="briefing_20261001_001", telegram_message_id=555)
    briefing_id, scope = events_log.resolve_reply_target(555)
    assert (briefing_id, scope) == ("briefing_20261001_001", "portfolio")
    assert events_log.resolve_reply_target(None) == (None, "staff-agent")


# --- Test 10: the real registry is coherent and honest ------------------------


def test_the_real_registry_parses_and_validates(tmp_path):
    catalog = registry.load_registry(registry.REPO_ROOT / "context" / "portfolio" / "projects.md")
    assert "staff-agent" in catalog
    assert len([p for p in catalog if p != "staff-agent"]) >= 2
    assert registry.validate_registry(catalog) == []


def test_the_real_registry_does_not_claim_ci_can_read_local_projects():
    catalog = registry.load_registry(registry.REPO_ROOT / "context" / "portfolio" / "projects.md")
    for pid, project in catalog.items():
        if project.evidence_path.startswith("/home/"):
            assert project.refreshable_in_actions == "no", (
                f"{pid} points at a local path but claims Actions can refresh it"
            )


def test_the_real_onboarded_projects_all_have_a_target_and_transition(monkeypatch, tmp_path):
    """Read the real committed state files, without the sandbox fixture's redirection."""
    monkeypatch.setattr(project_state, "PROJECTS_DIR", project_state.REPO_ROOT / "context" / "projects")
    monkeypatch.setattr(registry, "DEFAULT_REGISTRY_PATH", registry.REPO_ROOT / "context" / "portfolio" / "projects.md")
    states = portfolio.load_states()
    assert len(states) >= 2
    for pid, st in states.items():
        assert st["target_output"] not in ("unknown", ""), f"{pid} has no target_output"
        assert st["required_transition"] not in ("unknown", ""), f"{pid} has no required_transition"


# --- Test 11: the workflows commit the new files and stay wired correctly ------


REPO_ROOT = Path(__file__).parent.parent
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


@pytest.mark.parametrize("name", ["ingest-replies.yml", "daily-briefing.yml"])
def test_both_workflows_commit_the_project_and_portfolio_state(name):
    """A project state change that is never committed is a state change the loop loses."""
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert "context/projects/" in text
    assert "context/portfolio/" in text
    assert re.search(r"git add .*context/projects/", text)


@pytest.mark.parametrize("name", ["ingest-replies.yml", "daily-briefing.yml"])
def test_both_workflows_still_share_one_concurrency_group(name):
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    assert re.search(r"^concurrency:\s*\n\s+group:\s*staff-agent-memory-writes\s*$", text, re.MULTILINE)
    assert re.search(r"^\s+cancel-in-progress:\s*false\s*$", text, re.MULTILINE)


def test_the_ingest_schedule_is_still_every_ten_minutes():
    text = (WORKFLOWS / "ingest-replies.yml").read_text(encoding="utf-8")
    assert re.findall(r'-\s*cron:\s*"([^"]+)"', text) == ["*/10 * * * *"]


def test_the_portfolio_briefing_never_calls_a_model():
    source = (REPO_ROOT / "src" / "portfolio_briefing.py").read_text(encoding="utf-8")
    for forbidden in ("get_provider", "src.llm", "llm.factory"):
        assert forbidden not in source, f"portfolio briefing must not reference {forbidden}"


def test_the_ingest_path_makes_no_portfolio_briefing_call():
    source = (REPO_ROOT / "src" / "ingest_replies.py").read_text(encoding="utf-8")
    assert "src.portfolio_briefing" not in source


def test_the_daily_workflow_sends_the_portfolio_briefing():
    text = (WORKFLOWS / "daily-briefing.yml").read_text(encoding="utf-8")
    assert "src.portfolio_briefing --send" in text
    assert "src.project_update portfolio" in text


def test_the_staff_agent_briefing_is_tagged_staff_agent_scope():
    source = (REPO_ROOT / "src" / "briefing.py").read_text(encoding="utf-8")
    assert source.count('briefing_scope="staff-agent"') >= 2


# --- Test 12: the real end-to-end refresh path, offline -------------------------


def test_refresh_over_local_fixtures_appends_evidence_and_rebuilds_the_portfolio(tmp_path, monkeypatch):
    """The real `refresh` path, against fixture projects instead of the user's disk."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial work"], check=True)

    registry_file = tmp_path / "projects.md"
    registry_file.write_text(
        REGISTRY.replace("- evidence_source: event_only\n- evidence_path: none", f"- evidence_source: repo\n- evidence_path: {repo}"),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "DEFAULT_REGISTRY_PATH", registry_file)

    assert project_update.refresh([]) == 0

    evidence = [e for e in events_log.read_events() if e["event_type"] == "project_evidence"]
    assert {e["project_id"] for e in evidence} == {"alpha", "beta", "gamma"}
    assert all(e["fresh"] is True for e in evidence)
    assert any("initial work" in fact for e in evidence for fact in e["facts"])
    assert portfolio.load_state()["project_count"] == "3"


def test_refresh_reports_an_unavailable_source_without_failing_the_run(tmp_path, monkeypatch):
    registry_file = tmp_path / "projects.md"
    registry_file.write_text(
        REGISTRY.replace("- evidence_source: event_only\n- evidence_path: none", "- evidence_source: repo\n- evidence_path: /nonexistent/path"),
        encoding="utf-8",
    )
    monkeypatch.setattr(registry, "DEFAULT_REGISTRY_PATH", registry_file)

    assert project_update.refresh([]) == 0
    alpha = project_state.load_state("alpha")
    assert alpha["source_freshness"] == "unavailable"
    assert alpha["confidence"] == "low"


def test_a_still_needs_phrase_reads_as_a_constraint_not_a_pass():
    index = registry.alias_index(registry.load_registry())
    updates = routing.derive_field_updates("alpha still needs the phone retest", "alpha", index)
    assert updates[0]["polarity"] == "negative"
    assert updates[0]["signal"] == "still needs"


def test_routing_an_already_correct_value_produces_no_state_change():
    seed_project("alpha", phase="blocked")
    event = _ingest("alpha is still broken", 910)
    ingest.apply_project_routing([event])
    before = project_state.load_state("alpha")["evidence"]
    event = _ingest("alpha is still broken", 911)
    ingest.apply_project_routing([event])
    assert project_state.load_state("alpha")["evidence"] == before


# --- Test 13: the portfolio briefing send is idempotent and scoped ------------


def _sent_ids(monkeypatch) -> list[int]:
    sent: list[int] = []
    monkeypatch.setattr(
        "src.portfolio_briefing.send_telegram_message",
        lambda text: (sent.append(len(sent) + 5001) or sent[-1]),
    )
    return sent


def test_a_portfolio_briefing_is_sent_once_per_changed_portfolio(monkeypatch):
    from datetime import date

    seed_healthy("alpha")
    sent = _sent_ids(monkeypatch)
    today = date(2026, 10, 1)

    assert portfolio_briefing.send(portfolio.load_states(), registry.load_registry(), today) == 0
    assert len(sent) == 1

    # Same portfolio version, same day: no second message.
    assert portfolio_briefing.send(portfolio.load_states(), registry.load_registry(), today) == 0
    assert len(sent) == 1


def test_a_changed_portfolio_does_send_again(monkeypatch):
    from datetime import date

    seed_healthy("alpha")
    sent = _sent_ids(monkeypatch)
    today = date(2026, 10, 1)
    portfolio_briefing.send(portfolio.load_states(), registry.load_registry(), today)

    seed_project("alpha", phase="blocked", blocker="No credentials", required_transition="Add credentials")
    portfolio_briefing.send(portfolio.load_states(), registry.load_registry(), today)
    assert len(sent) == 2


def test_portfolio_briefing_events_are_tagged_with_the_portfolio_scope(monkeypatch):
    from datetime import date

    seed_healthy("alpha")
    _sent_ids(monkeypatch)
    portfolio_briefing.send(portfolio.load_states(), registry.load_registry(), date(2026, 10, 1))
    delivered = [e for e in events_log.read_events() if e.get("briefing_scope") == "portfolio"]
    assert {e["event_type"] for e in delivered} == {"briefing_sent", "briefing_delivered"}


def test_a_portfolio_briefing_does_not_satisfy_the_staff_agent_skip_check(monkeypatch):
    """The loop's idempotence check must not match a different layer's briefing."""
    from datetime import date

    seed_healthy("alpha")
    _sent_ids(monkeypatch)
    today = date(2026, 10, 1)
    portfolio_briefing.send(portfolio.load_states(), registry.load_registry(), today)

    assert events_log.last_delivered_briefing(today, scope="portfolio") is not None
    assert events_log.last_delivered_briefing(today, scope="staff-agent") is None


# --- Test 14: refresh and portfolio rebuild are true no-ops when nothing changed --


def test_refreshing_an_unchanged_source_appends_no_second_event():
    project_update.refresh(["alpha"])
    first = events_log.last_project_evidence("alpha")["event_id"]
    project_update.refresh(["alpha"])
    assert events_log.last_project_evidence("alpha")["event_id"] == first
    assert len([e for e in events_log.read_events() if e["event_type"] == "project_evidence"]) == 1


def _repo_project(tmp_path, project_id: str = "delta") -> Path:
    """Register a project whose source is a real local git repo."""
    repo = tmp_path / f"{project_id}-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@e"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "first"], check=True)

    text = registry.DEFAULT_REGISTRY_PATH.read_text(encoding="utf-8") + (
        f"\n### {project_id}\n\n"
        f"- id: {project_id}\n"
        f"- name: Delta\n"
        f"- aliases: delta\n"
        f"- tracking: active\n"
        f"- evidence_source: repo\n"
        f"- evidence_path: {repo}\n"
        f"- refreshable_in_actions: no\n"
        f"- onboarded_at: 2026-10-01\n"
    )
    registry.DEFAULT_REGISTRY_PATH.write_text(text, encoding="utf-8")
    return repo


def test_a_changed_source_still_appends_a_new_event(tmp_path):
    repo = _repo_project(tmp_path)
    project_update.refresh(["delta"])
    first = events_log.last_project_evidence("delta")["event_id"]

    # A real new commit is a real change, so it must be recorded again.
    (repo / "b.txt").write_text("y", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "second"], check=True)
    project_update.refresh(["delta"])
    assert events_log.last_project_evidence("delta")["event_id"] != first


def test_writing_an_unchanged_portfolio_leaves_the_file_byte_identical():
    states = portfolio.load_states()
    project_update.update_portfolio()
    first = portfolio.PORTFOLIO_STATE_PATH.read_text()
    project_update.update_portfolio()
    assert portfolio.PORTFOLIO_STATE_PATH.read_text() == first


def test_writing_a_changed_portfolio_does_change_the_file():
    states = portfolio.load_states()
    seed_project("alpha", phase="blocked", blocker="No credentials")
    portfolio.write_state(portfolio.build_state(portfolio.load_states()))
    written = portfolio.PORTFOLIO_STATE_PATH.read_text()
    portfolio.write_state(portfolio.build_state(states))
    assert portfolio.PORTFOLIO_STATE_PATH.read_text() != written


# --- Test 15: the manual loop tool can never write the repo's real state --------


def test_the_manual_loop_tool_writes_nothing_into_the_repo(tmp_path, monkeypatch):
    """Regression: `--tmp-memory` only redirected MEMORY_DIR, so the tool reset the
    repo's real context/current_state.md to a synthetic seed and destroyed the live
    checkpoints. The sandbox must now cover the state file too."""
    from tests import run_episodic_loop as loop

    real_state = registry.REPO_ROOT / "context" / "current_state.md"
    real_events = registry.REPO_ROOT / "memory" / "episodic" / "events.jsonl"
    before = (real_state.read_text(), real_events.read_text())

    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    monkeypatch.setattr(loop, "ARTIFACT_DIR", artifacts)
    loop.run(tmp_path / "sandbox")

    assert (real_state.read_text(), real_events.read_text()) == before
    assert (tmp_path / "sandbox" / "current_state.md").exists()
