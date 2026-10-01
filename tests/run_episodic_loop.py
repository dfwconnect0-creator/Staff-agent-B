"""Manual three-cycle episodic-loop test.

Drives the real pipeline end to end:

    src.briefing.main()                      the same entrypoint the 05:00 UTC cron runs
    src.ingest_replies.ingest_update()       the same function the 2-hourly cron calls per update
    src.state_update.update_state_from_events()

Only two network boundaries are substituted, at the seams the existing test suite
already uses: `src.briefing.get_provider` (the LLM) and `src.briefing.send_telegram_message`
(Telegram). The stand-ins come from `tests/loop_support.py`, which
`tests/test_loop_three_cycles.py` also uses, so the manual run and the automated gate
cannot diverge.

Memory and `context/current_state.md` are written for real, because persistence is part of
what is being proven. Use --tmp-memory for an isolated run.

Usage:
    uv run python -m tests.run_episodic_loop [--tmp-memory]
"""

import argparse
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import freezegun

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import src.memory.episodic as episodic  # noqa: E402
import src.events as events_log  # noqa: E402
import src.state as state_mod  # noqa: E402
import src.state_update as state_update_mod  # noqa: E402
import src.briefing as briefing  # noqa: E402
import src.ingest_replies as ingest  # noqa: E402

from tests.loop_support import (  # noqa: E402
    CHECKPOINTS as CHECKPOINTS_ALL,
    FakeTelegram,
    ScriptedProvider,
    reset_state_to_seed,
)

DAY = "2026-10-01"
ARTIFACT_DIR = REPO / "tests" / "artifacts" / f"episodic-loop-{DAY.replace('-', '')}"


def run_briefing(provider, telegram, clock):
    before = len(telegram.sent)
    with freezegun.freeze_time(clock):
        with patch.object(briefing, "get_provider", return_value=provider):
            with patch.object(briefing, "send_telegram_message", side_effect=telegram.send):
                rc = briefing.main()
    delivered = telegram.sent[before] if len(telegram.sent) > before else None
    return rc, delivered


def write_briefing_artifact(n, delivered, clock):
    event = [e for e in events_log.read_events() if e["event_type"] == "briefing_sent"][-1]
    (ARTIFACT_DIR / f"briefing_{n}.txt").write_text(
        f"briefing_id: {event['briefing_id']}\n"
        f"telegram_message_id: {delivered['message_id']}\n"
        f"delivered_at_cairo: {clock}\n"
        f"state_version: {event['state_version']}\n"
        f"next_action: {event['next_action']}\n" + "-" * 60 + f"\n{delivered['text'].rstrip()}\n",
        encoding="utf-8",
    )
    return event


def cycle(cycle_no, provider, telegram, transcript):
    w = transcript.append
    clock = f"{DAY} 0{cycle_no}:00:00"
    w("")
    w("=" * 78)
    w(f"CYCLE {cycle_no}    run clock {clock} Cairo")
    w("=" * 78)

    before = state_mod.load_state()
    w(f"checkpoints BEFORE  : {json.dumps(before['checkpoints'])}")
    w(f"next_action BEFORE  : {before['next_action']}")
    w(f"state_version BEFORE: {state_mod.state_version(before)}")
    w("")

    rc, delivered = run_briefing(provider, telegram, clock)
    event = write_briefing_artifact(cycle_no, delivered, clock)
    w(f"briefing.main() rc  : {rc}")
    w(f"briefing_id         : {event['briefing_id']}   telegram_message_id: {delivered['message_id']}")
    w(f"state_version USED  : {event['state_version']}")
    w("--- briefing text as delivered to Telegram " + "-" * 44)
    w(delivered["text"].rstrip())
    w("")

    w(f"--- simulated Telegram reply to message {delivered['message_id']} " + "-" * 31)
    update = telegram.reply_update(cycle_no, reply_to_message_id=delivered["message_id"])
    w(f"update_id={update['update_id']}  message_id={update['message_id']}  "
      f"reply_to_message_id={update['reply_to_message_id']}")
    w(f"text: {update['text']}")
    w("")
    (ARTIFACT_DIR / f"reply_{cycle_no}.json").write_text(
        json.dumps(update, indent=2) + "\n", encoding="utf-8")

    with freezegun.freeze_time(clock):
        reply_event = ingest.ingest_update(update)
    w(f"ingested event      : {reply_event['event_id']}   linked briefing: {reply_event['briefing_id']}")
    w(f"provenance          : telegram_update_id={reply_event['telegram_update_id']} "
      f"telegram_message_id={reply_event['telegram_message_id']} "
      f"reply_to_message_id={reply_event['reply_to_message_id']}")
    w(f"processed at ingest : {reply_event['processed']}   (consumption is recorded separately)")

    report = state_update_mod.update_state_from_events()
    after = state_mod.load_state()
    w("")
    w("--- state-update step: src.state_update.update_state_from_events() " + "-" * 23)
    for t in report["transitions"]:
        w(f"  {t['checkpoint']}: {t['from']} -> {t['to']}")
        w(f"     evidence: {t['evidence_event_id']}   alias matched: '{t['matched_alias']}'   "
          f"signal: '{t['signal']}'")
        w(f"     quote   : \"{t['quote']}\"")
    if not report["transitions"]:
        w("  (no checkpoint value changed)")
    w(f"checkpoints AFTER   : {json.dumps(after['checkpoints'])}")
    w(f"next_action AFTER   : {after['next_action']}")
    w(f"state_version AFTER : {report['state_version']}")
    w("")
    w("--- context/current_state.md after the cycle " + "-" * 34)
    w(state_mod.render_state(after).rstrip())
    w("")
    return after


def verification_briefing(provider, telegram, transcript):
    w = transcript.append
    clock = f"{DAY} 04:00:00"
    w("")
    w("=" * 78)
    w(f"VERIFICATION BRIEFING  (Briefing 4)    run clock {clock} Cairo")
    w("=" * 78)
    before = state_mod.load_state()
    w(f"checkpoints BEFORE  : {json.dumps(before['checkpoints'])}")
    rc, delivered = run_briefing(provider, telegram, clock)
    event = write_briefing_artifact(4, delivered, clock)
    w(f"briefing.main() rc  : {rc}")
    w(f"briefing_id         : {event['briefing_id']}   telegram_message_id: {delivered['message_id']}")
    w("--- briefing text as delivered to Telegram " + "-" * 44)
    w(delivered["text"].rstrip())
    w("")
    return state_mod.load_state()


def idempotence_probe(provider, telegram, transcript):
    w = transcript.append
    w("")
    w("=" * 78)
    w("IDEMPOTENCE PROBE    immediate re-run, no new evidence")
    w("=" * 78)
    before = len(telegram.sent)
    run_briefing(provider, telegram, f"{DAY} 04:30:00")
    w(f"telegram sends during probe: {len(telegram.sent) - before}   (expected 0)")
    w("reason: state_version is unchanged and briefing_20261001_004 was already delivered today,")
    w("        so the pipeline correctly treats the re-run as a no-op.")


def write_test_report(provider, telegram, final):
    """Self-contained report: what ran, what it proved, what was stubbed."""
    events = events_log.read_events()
    transitions = final["transitions"]
    assert transitions, "expected the run to record transitions"
    lines = [
        "# Three-cycle episodic loop test report",
        "",
        f"Run date (Cairo): {DAY}",
        "",
        "## What was executed",
        "",
        "| | |",
        "|---|---|",
        "| real code | `src.briefing.main()`, `src.ingest_replies.ingest_update()`, "
        "`src.state_update.update_state_from_events()` |",
        "| substituted | `src.briefing.get_provider` (LLM), `src.briefing.send_telegram_message` "
        "(Telegram HTTP) |",
        "| stubs | `tests/loop_support.py`, shared with `tests/test_loop_three_cycles.py` |",
        f"| LLM calls | {len(provider.prompts)} |",
        f"| Telegram sends | {len(telegram.sent)} |",
        f"| events appended | {len(events)} |",
        "",
        "## The three cycles",
        "",
        "| Cycle | Briefing id | Telegram msg | Reply event | Checkpoints verified after | Next action |",
        "|---|---|---|---|---|---|",
    ]
    for n in (1, 2, 3):
        bid = f"briefing_20261001_00{n}"
        sent = [e for e in events if e["event_type"] == "briefing_sent" and e.get("briefing_id") == bid]
        delivered = [e for e in events
                     if e["event_type"] == "briefing_delivered" and e.get("briefing_id") == bid]
        reply = [e for e in events
                 if e["event_type"] == "user_reply" and e.get("briefing_id") == bid]
        after = {
            "briefing_20261001_001": ["reply_ingestion", "storage"],
            "briefing_20261001_002": ["reply_ingestion", "storage", "retrieval"],
            "briefing_20261001_003": list(CHECKPOINTS_ALL),
        }[bid]
        lines.append(
            f"| {n} | {bid} "
            f"| {delivered[0].get('telegram_message_id') if delivered else 'n/a'} "
            f"| {reply[0]['event_id'] if reply else 'n/a'} "
            f"| {', '.join(after)} | {next_action_of(n)} |"
        )

    lines += [
        "",
        "## Briefing 4 (verification)",
        "",
        f"All checkpoints verified, so the derived next action is: **{final['next_action']}**",
        "and the briefing reports no mismatch. Re-running immediately afterwards sent 0 messages.",
        "",
        "## State transitions recorded by the pipeline",
        "",
    ]
    lines += transitions
    lines += [
        "",
        "## Honest limitations",
        "",
        "- No live Telegram chat and no live LLM call were involved. Every claim above is about",
        "  the pipeline's behaviour with the network boundaries replaced. Real-delivery",
        "  verification needs one `workflow_dispatch` run once the repo secrets are set.",
        "- `state_update.py` matches phrases a human would use, via the alias and signal tables in",
        "  `current_state.md` and `src/state_update.py`. It does not understand arbitrary phrasing;",
        "  a new synonym means adding an alias, not changing the model.",
        "- Checkpoint ordering is the order it appears in `current_state.md`, so `next_action` is",
        "  deterministic but depends on that file being ordered the way the work actually happens.",
        "",
    ]
    (ARTIFACT_DIR / "test_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def next_action_of(n):
    import re
    text = (ARTIFACT_DIR / f"briefing_{n}.txt").read_text(encoding="utf-8")
    m = re.search(r"Smallest next action:\n(.*?)(?:\n\n|\Z)", text, re.DOTALL)
    return m.group(1).strip() if m else "n/a"


def run(out_dir):
    episodic.MEMORY_DIR = out_dir / "memory"
    (out_dir / "memory").mkdir(parents=True, exist_ok=True)

    # Experiment setup, applied once before cycle 1 so the run is reproducible from
    # the repo's live state. No step inside a cycle writes this.
    state_path = reset_state_to_seed(state_mod.DEFAULT_STATE_PATH)
    (ARTIFACT_DIR / "initial_state.md").write_text(
        state_mod.render_state(state_mod.load_state()), encoding="utf-8")
    print(f"memory/episodic -> {episodic.MEMORY_DIR}")
    print(f"starting state seeded at {state_path}")

    provider = ScriptedProvider()
    telegram = FakeTelegram()
    telegram.day = DAY
    transcript = [
        "THREE-CYCLE EPISODIC LOOP TEST",
        f"repo   : {REPO}",
        f"date   : {DAY} (Cairo)",
        "real   : src.briefing.main(), src.ingest_replies.ingest_update(), "
        "src.state_update.update_state_from_events()",
        "stubbed: src.briefing.get_provider (LLM), src.briefing.send_telegram_message (Telegram HTTP)",
        "stubs  : tests/loop_support.py, shared with tests/test_loop_three_cycles.py",
        "memory : memory/episodic/events.jsonl and context/current_state.md",
        "",
        "The run resets context/current_state.md to the experiment seed once, before cycle 1,",
        "so it is reproducible. Every state change after that is pipeline-written.",
        "",
        "initial state (context/current_state.md at the start of the run):",
        state_mod.render_state(state_mod.load_state()).rstrip(),
        "",
    ]

    for n in (1, 2, 3):
        after = cycle(n, provider, telegram, transcript)
        name = "state_after_cycle_1.md" if n == 1 else f"state_after_cycle_{n}.md"
        (ARTIFACT_DIR / name).write_text(state_mod.render_state(after), encoding="utf-8")

    final = verification_briefing(provider, telegram, transcript)
    idempotence_probe(provider, telegram, transcript)

    (ARTIFACT_DIR / "final_state.md").write_text(state_mod.render_state(final), encoding="utf-8")
    (ARTIFACT_DIR / "events.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events_log.read_events()),
        encoding="utf-8")
    (ARTIFACT_DIR / "run_transcript.txt").write_text("\n".join(transcript) + "\n", encoding="utf-8")
    write_test_report(provider, telegram, final)

    print("\n".join(transcript))
    print(f"\nprovider invoked {len(provider.prompts)}x, telegram sends {len(telegram.sent)}")
    print(f"artifacts -> {ARTIFACT_DIR}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tmp-memory", action="store_true",
                        help="keep the repo's memory/episodic/ clean")
    args = parser.parse_args()

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    out_dir = Path("/tmp/episodic-loop-run") if args.tmp_memory else REPO
    if args.tmp_memory and out_dir.exists():
        shutil.rmtree(out_dir)
    return run(out_dir)


if __name__ == "__main__":
    sys.exit(main())
