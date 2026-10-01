"""Independent verification of the saved artifacts.

Re-derives each acceptance criterion from the files on disk in
tests/artifacts/episodic-loop-20261001/, without importing the pipeline.
If this and the pipeline disagree, the artifacts are what count.

Usage: uv run python -m tests.verify_artifacts
"""

import json
import re
import sys
from pathlib import Path

ART = Path(__file__).resolve().parent.parent / "tests" / "artifacts" / "episodic-loop-20261001"

EXPECTED_AFTER = {
    "state_after_cycle_1.md": {
        "reply_ingestion": "verified",
        "storage": "verified",
        "retrieval": "not_verified",
        "use_in_decision": "not_verified",
        "feedback_loop": "not_verified",
    },
    "state_after_cycle_2.md": {
        "reply_ingestion": "verified",
        "storage": "verified",
        "retrieval": "verified",
        "use_in_decision": "not_verified",
        "feedback_loop": "not_verified",
    },
    "final_state.md": {
        "reply_ingestion": "verified",
        "storage": "verified",
        "retrieval": "verified",
        "use_in_decision": "verified",
        "feedback_loop": "verified",
    },
}

EXPECTED_ACTIONS = {
    1: "Verify reply_ingestion.",
    2: "Verify retrieval.",
    3: "Verify use_in_decision.",
    4: "No evidence-backed intervention needed.",
}

results = []


def check(criterion, ok, detail=""):
    results.append((criterion, bool(ok), detail))


def checkpoints(text):
    out = {}
    for name, value in re.findall(r"^- ([a-z0-9_]+) = ([a-z_]+)", text, re.MULTILINE):
        out[name] = value
    return out


def next_action(text):
    m = re.search(r"^next_action: (.*)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def main():
    required = [
        "initial_state.md", "briefing_1.txt", "reply_1.json", "state_after_cycle_1.md",
        "briefing_2.txt", "reply_2.json", "state_after_cycle_2.md",
        "briefing_3.txt", "reply_3.json", "state_after_cycle_3.md",
        "final_state.md", "briefing_4.txt",
    ]
    missing = [f for f in required if not (ART / f).exists()]
    check("C0 all required artifacts present", not missing, f"missing: {missing}")

    events = [json.loads(l) for l in (ART / "events.jsonl").read_text().splitlines() if l.strip()]
    replies = [e for e in events if e["event_type"] == "user_reply"]
    sent = [e for e in events if e["event_type"] == "briefing_sent"]
    delivered = [e for e in events if e["event_type"] == "briefing_delivered"]
    updates = [e for e in events if e["event_type"] == "state_update"]

    # C1 unique briefing ids
    ids = [e["briefing_id"] for e in sent]
    check("C1 every briefing has a unique ID",
          ids == ["briefing_20261001_001", "briefing_20261001_002",
                  "briefing_20261001_003", "briefing_20261001_004"] and len(set(ids)) == 4,
          f"ids={ids}")

    # C2 replies stored with provenance
    ok = all(
        r.get("telegram_update_id") is not None
        and r.get("telegram_message_id") is not None
        and r.get("timestamp_cairo")
        and r.get("text")
        and r.get("event_id")
        and r.get("source") == "telegram"
        for r in replies
    )
    check("C2 every reply stored with provenance", ok and len(replies) == 3,
          f"{len(replies)} replies, all with update/message ids: {ok}")

    # C3 reply linked to the briefing it answered
    link_ok = True
    detail = []
    for i, r in enumerate(replies, start=1):
        expected = f"briefing_20261001_00{i}"
        matching = [d for d in delivered if d["telegram_message_id"] == r["reply_to_message_id"]]
        good = r["briefing_id"] == expected and bool(matching) and matching[0]["briefing_id"] == expected
        link_ok &= good
        detail.append(f"{r['event_id']}->{r['briefing_id']} (reply_to={r['reply_to_message_id']})")
    check("C3 reply linked to the briefing it answered", link_ok, "; ".join(detail))

    # C4 new replies update operational state
    state4 = []
    for name in ("state_after_cycle_1.md", "state_after_cycle_2.md", "final_state.md"):
        state4.append(checkpoints((ART / name).read_text()))
    check("C4 replies update operational state",
          all(a != b for a, b in zip(state4, state4[1:])),
          "checkpoint tables differ after each cycle")

    # C5 state changes inspectable
    final_raw = (ART / "final_state.md").read_text()
    transitions = re.findall(r"^- \S+ \| (evt_\d+) \| (\w+): (\w+) -> (\w+) \| .*signal '([^']+)'",
                             final_raw, re.MULTILINE)
    check("C5 state changes inspectable in the state file",
          len(transitions) == 5
          and all(t[2] == "not_verified" and t[3] == "verified" for t in transitions)
          and {t[1] for t in transitions} == set(EXPECTED_AFTER["final_state.md"]),
          f"{len(transitions)} transitions, each with evidence id, from->to and signal")

    # C6/C7/C8 briefing N+1 reflects cycle N
    actions = {}
    for n in (1, 2, 3, 4):
        text = (ART / f"briefing_{n}.txt").read_text()
        m = re.search(r"Smallest next action:\n(.*)", text)
        actions[n] = m.group(1).strip() if m else None
        cps = re.search(r"State:\n(.*)", text)
        actions[f"state_{n}"] = cps.group(1).strip() if cps else None

    check("C6 briefing 2 reflects cycle 1",
          actions[2] == EXPECTED_ACTIONS[2]
          and "reply_ingestion = verified" in actions["state_2"]
          and "storage = verified" in actions["state_2"],
          f"action={actions[2]!r}")
    check("C6 briefing 3 reflects cycle 2",
          actions[3] == EXPECTED_ACTIONS[3] and "retrieval = verified" in actions["state_3"],
          f"action={actions[3]!r}")

    def action_section(n):
        text = (ART / f"briefing_{n}.txt").read_text()
        m = re.search(r"Smallest next action:\n(.*?)(?:\n\n|\Z)", text, re.DOTALL)
        return m.group(1).strip() if m else ""

    check("C7 briefing 2 does not re-test verified steps",
          action_section(2) == "Verify retrieval."
          and "reply_ingestion" not in action_section(2)
          and "storage" not in action_section(2),
          f"action section = {action_section(2)!r}")
    check("C7 briefing 3 does not re-test verified steps",
          action_section(3) == "Verify use_in_decision."
          and "reply_ingestion" not in action_section(3)
          and "storage" not in action_section(3)
          and "retrieval" not in action_section(3),
          f"action section = {action_section(3)!r}")

    check("C8 recommendation changes with the evidence",
          [actions[n] for n in (1, 2, 3)] == [EXPECTED_ACTIONS[1], EXPECTED_ACTIONS[2],
                                              EXPECTED_ACTIONS[3]],
          f"{actions[1]} -> {actions[2]} -> {actions[3]}")

    # C9 target recognised as achieved
    b4 = (ART / "briefing_4.txt").read_text()
    check("C9 briefing 4 recognises the target was achieved",
          actions[4] == EXPECTED_ACTIONS[4]
          and "No mismatch" in b4
          and "reply_ingestion = not_verified" not in b4
          and "retrieval = not_verified" not in b4
          and "use_in_decision = not_verified" not in b4,
          f"action={actions[4]!r}")

    # C10 no manual state edits between cycles
    consumed_all = {eid for u in updates for eid in u["consumes"]}
    check("C10 every state change traces to a consumed reply event",
          consumed_all == {r["event_id"] for r in replies} and len(updates) == 3,
          f"{len(updates)} state_update events consuming {sorted(consumed_all)}")

    # expected checkpoint values per cycle, straight from the spec
    for name, expected in EXPECTED_AFTER.items():
        got = checkpoints((ART / name).read_text())
        check(f"C-spec {name} matches the expected state", got == expected,
              f"got {got}" if got != expected else "")

    # reply files on disk match the spec's three test replies
    expected_reply_fragments = ["completed reply ingestion", "Retrieval worked",
                                "correctly used the retrieved information"]
    ok = True
    for n, frag in enumerate(expected_reply_fragments, start=1):
        ok &= frag in (ART / f"reply_{n}.json").read_text()
    check("C-spec the three prescribed replies were used verbatim", ok)

    # C11 same core logic for scheduled runs
    briefing_src = (Path(__file__).resolve().parent.parent / "src" / "briefing.py").read_text()
    wf = (Path(__file__).resolve().parent.parent / ".github" / "workflows" / "daily-briefing.yml").read_text()
    check("C11 the scheduled workflow runs the same entrypoint",
          "python -m src.briefing" in wf
          and "state_update_mod.update_state_from_events()" in briefing_src,
          "workflow_dispatch and cron both call src.briefing.main via the same module")

    # C12 no unrelated architecture
    import subprocess
    baseline = subprocess.run(
        ["git", "show", "HEAD:pyproject.toml"],
        cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True,
    ).stdout
    current = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    check("C12 no unrelated architecture introduced",
          baseline == current
          and not (Path(__file__).resolve().parent.parent / "src" / "vector_store.py").exists(),
          "pyproject.toml byte-identical to HEAD; no new dependency")

    width = max(len(c) for c, _, _ in results)
    failed = 0
    for criterion, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        failed += not ok
        print(f"{status:4}  {criterion.ljust(width)}  {detail}")
    print()
    print(f"{len(results) - failed}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
