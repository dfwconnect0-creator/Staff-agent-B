"""The recovery store fails closed on anything it cannot fully account for.

The store is the only durable record of what this machine already sent. Two shapes are
therefore not equivalent, and the difference is the whole point of this file:

* **absent** — "this machine has never dispatched". A complete, provable statement.
* **present but carrying no records** — equally the shape left by a write that was cut
  short. Reading it as the first is how a dispatch happens twice.

So an empty file and a whitespace-only file are corruption, not an empty queue. On top of
that, a file can parse and still be untrustworthy: a record missing a field, mistyped, or
whose payload no longer hashes to its stored digest cannot be sent, matched, or aged, so
every rule that reads it would be reading a guess.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src import local_watcher as lw  # noqa: E402
from tests.watcher_support import (  # noqa: E402
    GitHub,
    build_observation,
    build_payload,
    dispatch_count,
    run_traced_in_fresh_process,
    run_watcher_main,
)
from tests.watcher_support import watcher  # noqa: E402,F401

pytestmark = pytest.mark.usefixtures("watcher")


@pytest.fixture
def github(monkeypatch):
    return GitHub().install(monkeypatch)


def _store() -> Path:
    return lw.dispatch_records_path()


def _write_store(text: str) -> None:
    lw.ensure_dirs()
    _store().write_text(text, encoding="utf-8")


def _good_record(**overrides) -> dict:
    record = lw.new_record("a" * 32, build_payload([build_observation()]))
    record.update(overrides)
    return record


# --- 1 / 2: the two states that are genuinely empty -----------------------------


def test_a_missing_recovery_file_is_a_valid_empty_store(watcher):
    assert not _store().exists()
    assert lw.load_records() == []


def test_a_valid_empty_json_list_is_an_intentional_empty_store(watcher):
    _write_store("[]")
    assert lw.load_records() == []


# --- 3 / 4: present, but carrying no records ------------------------------------


def test_a_zero_byte_recovery_file_is_corruption_not_an_empty_queue(watcher):
    """An interrupted write leaves exactly this. Accepting it loses the receipt."""
    _write_store("")
    with pytest.raises(lw.CorruptRecoveryStore):
        lw.load_records()


@pytest.mark.parametrize("blank", ["   ", "\n\n", "\t", " \n\t \r\n"])
def test_a_whitespace_only_recovery_file_is_corruption_not_an_empty_queue(watcher, blank):
    _write_store(blank)
    with pytest.raises(lw.CorruptRecoveryStore):
        lw.load_records()


def test_a_zero_byte_store_is_distinguished_from_a_missing_one(watcher):
    """The two must not be the same answer, or the whole guarantee collapses."""
    assert lw.load_records() == []
    lw.ensure_dirs()
    lw.dispatch_records_path().write_bytes(b"")
    with pytest.raises(lw.CorruptRecoveryStore):
        lw.load_records()


# --- 5 / 6 / 7: content that cannot be parsed ------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        '[{"correlation_id": "aaa',  # truncated mid-object
        '[{"correlation_id": "aaaaaaaaaaaa"},',  # truncated mid-array
        "",
    ],
    ids=["truncated-object", "truncated-array", "empty"],
)
def test_a_truncated_store_fails_closed(watcher, body):
    if body == "":
        _write_store("")
    else:
        _write_store(body)
    with pytest.raises((ValueError, lw.CorruptRecoveryStore)):
        lw.load_records()


@pytest.mark.parametrize("body", ["{ not json at all", "nonsense", "{'single': 'quotes'}"])
def test_invalid_json_fails_closed(watcher, body):
    _write_store(body)
    with pytest.raises((ValueError, lw.CorruptRecoveryStore)):
        lw.load_records()


@pytest.mark.parametrize("body", ['{"not": "a list"}', '"a string"', "42", "null", "true"])
def test_a_wrong_top_level_shape_fails_closed(watcher, body):
    _write_store(body)
    with pytest.raises(ValueError):
        lw.load_records()


def test_a_store_that_is_not_utf8_fails_closed(watcher):
    lw.ensure_dirs()
    _store().write_bytes(b'[{"correlation_id": "\xff\xfe"}]')
    with pytest.raises(ValueError):
        lw.load_records()


# --- 8 / 9: records that parse but cannot be trusted -----------------------------


def test_a_malformed_record_fails_closed(watcher):
    """A receipt missing the fields every rule depends on is not a receipt."""
    _write_store(json.dumps([{"correlation_id": "a" * 32, "state": "dispatching"}]))
    with pytest.raises(ValueError, match="missing required field"):
        lw.load_records()


@pytest.mark.parametrize(
    "overrides,expected",
    [
        ({"correlation_id": "NOT-HEX"}, "correlation_id"),
        ({"state": "sent"}, "state"),
        ({"created_at": "2026-10-03 12:00:00"}, "created_at"),
        ({"created_at": "not a timestamp"}, "created_at"),
        ({"payload": "not an object"}, "payload"),
        ({"payload": {"observations": []}}, "no sendable observations"),
        ({"run_id": "7001"}, "run_id"),
        ({"run_status": 7}, "run_status"),
    ],
)
def test_each_mistyped_field_fails_closed(watcher, overrides, expected):
    record = _good_record()
    record.update(overrides)
    _write_store(json.dumps([record]))
    with pytest.raises(ValueError, match=expected):
        lw.load_records()


def test_a_payload_digest_that_does_not_match_its_payload_fails_closed(watcher):
    """The digest is the only evidence the stored payload is the one that was sent."""
    record = _good_record()
    record["payload"]["observations"][0]["facts"] = ["quietly rewritten"]
    _write_store(json.dumps([record]))
    with pytest.raises(ValueError, match="payload_digest does not match"):
        lw.load_records()


def test_a_resealed_payload_passes_validation(watcher):
    """Recomputing the digest makes the record valid again — the check is real, not a formality."""
    record = _good_record()
    record["payload"]["observations"][0]["facts"] = ["legitimately rewritten"]
    record["payload_digest"] = lw.payload_digest(record["payload"])
    _write_store(json.dumps([record]))
    assert len(lw.load_records()) == 1


def test_a_valid_record_is_accepted_unchanged(watcher):
    record = _good_record()
    _write_store(json.dumps([record]))
    assert lw.load_records() == [record]


# --- 10: corruption stops the cycle before it can rediscover anything ------------


def test_a_corrupt_receipt_stops_the_cycle_before_it_dispatches(watcher, github, monkeypatch, tmp_path, capsys):
    from tests.watcher_support import sandbox_projects

    _write_store(json.dumps([_good_record(state="dispatching"), {"correlation_id": "broken"}]))
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})

    assert run_watcher_main(monkeypatch) == 1
    assert github.dispatch_calls == [], "a corrupt store still sent observations"
    assert "unreadable" in capsys.readouterr().err


def test_a_corrupt_store_leaves_the_good_receipt_on_disk(watcher, github, monkeypatch, tmp_path):
    """Refusing to act must not also mean destroying the evidence."""
    from tests.watcher_support import sandbox_projects

    record = _good_record(state="dispatching")
    _write_store(json.dumps([record, {"correlation_id": "broken"}]))
    sandbox_projects(tmp_path, monkeypatch)
    monkeypatch.setattr(lw, "refresh_local_repo_safely", lambda **_kwargs: {"ok": True, "reason": ""})

    run_watcher_main(monkeypatch)
    assert json.loads(_store().read_text(encoding="utf-8"))[0]["correlation_id"] == "a" * 32


def test_a_malformed_record_is_never_repaired_dropped_or_resent(watcher, github):
    """Not repaired, not dropped, and the payload is not freed for another dispatch."""
    _write_store(json.dumps([_good_record(state="dispatching", run_id="7001")]))
    with pytest.raises(ValueError):
        lw.reconcile_records()
    assert github.dispatch_calls == []


# --- 22: the Codex zero-byte failure, across two real processes -------------------


DISPATCH_BODY = """
from tests.watcher_support import build_observation, build_payload
outcome, record = lw.dispatch_observations(build_payload([build_observation()]))
print(json.dumps({"outcome": outcome, "correlation_id": record["correlation_id"]}))
"""

MAIN_BODY = """
import sys
_code = lw.main()
print(json.dumps({"exit": _code}))
sys.exit(_code)
"""


def test_the_zero_byte_codex_failure_across_two_processes_dispatches_once(watcher):
    """Process 1 sends and is accepted; the store is truncated to nothing; process 2 starts.

    Before the repair, process 2 read a zero-byte file as an empty queue, found the same
    observations waiting to be discovered, and sent them again. It now refuses to act on a
    store it cannot account for, so the total across both processes is one dispatch.
    """
    state = lw.state_dir()

    first = run_traced_in_fresh_process(state, DISPATCH_BODY, allow_dispatch=True, expect_returncode=0)
    assert dispatch_count(first["calls"]) == 1
    assert len(lw.load_records()) == 1

    # The receipt is destroyed the way an interrupted write would destroy it.
    lw.dispatch_records_path().write_bytes(b"")
    assert lw.dispatch_records_path().exists()

    second = run_traced_in_fresh_process(state, MAIN_BODY, expect_returncode=1)

    assert dispatch_count(second["calls"]) == 0, "process 2 dispatched from a corrupt store"
    assert second["result"]["exit"] == 1
    assert "unreadable" in second["stderr"]
    assert lw.dispatch_records_path().read_bytes() == b"", "the corrupt store was silently overwritten"


def test_a_whitespace_only_store_across_two_processes_also_dispatches_once(watcher):
    state = lw.state_dir()

    first = run_traced_in_fresh_process(state, DISPATCH_BODY, allow_dispatch=True, expect_returncode=0)
    assert dispatch_count(first["calls"]) == 1

    lw.dispatch_records_path().write_text("  \n\t \n", encoding="utf-8")

    second = run_traced_in_fresh_process(state, MAIN_BODY, expect_returncode=1)

    assert dispatch_count(second["calls"]) == 0
    assert second["result"]["exit"] == 1