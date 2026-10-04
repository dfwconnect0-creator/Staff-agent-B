#!/usr/bin/env python3
"""Autonomous local project watcher.

One cycle, in this order:

1. reconcile every dispatch left unresolved by an earlier process;
2. inspect each registered, active, local-only project;
3. dispatch the observations that are genuinely new.

No LLM, no Telegram, no model judgement. This is a reader and a sender, and the only
thing it decides for itself is whether a remote run is provably the run it created.

Run as ``python src/local_watcher.py`` (the systemd unit does exactly that), so the
repository root is placed on ``sys.path`` before the ``src.`` package is imported.
"""

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.observation_contract import (  # noqa: E402
    MAX_OBSERVATIONS,
    MAX_PAYLOAD_BYTES,
    SCHEMA_VERSION,
    generate_correlation_id,
    is_in_flight,
    is_timestamp,
    is_valid_correlation_id,
    observation_identity,
    project_scoped_identity,
    run_name_for,
)

CAIRO_TZ = timezone(timedelta(hours=3))

GITHUB_REPO = "dfwconnect0-creator/Staff-agent-B"
OBSERVATION_WORKFLOW = "project-observation.yml"
DISPATCH_TIMEOUT_SECONDS = 60
GITHUB_QUERY_TIMEOUT_SECONDS = 30

# How far the search for one dispatch's exact run may reach. This is a bound on *work*,
# not on the answer. A search ends when it has crossed the dispatch's own timestamp; the
# page cap only decides when to admit that it could not get there.
GITHUB_QUERY_PAGE_SIZE = 100
GITHUB_QUERY_MAX_PAGES = 20

# How long a dispatch is trusted to still be invisible. GitHub can accept a request
# seconds before the run appears in any list, so "not listed yet" is not evidence that the
# request never landed. Until this grace expires, the receipt stays unresolved.
VISIBILITY_GRACE_SECONDS = 900

# Match outcomes. `query_unavailable` is deliberately not `no_match`: "I could not ask"
# and "it does not exist" lead to opposite actions, and collapsing them is how a
# dispatch that GitHub already accepted gets sent a second time.
MATCH = "match"
NO_MATCH = "no_match"
QUERY_UNAVAILABLE = "query_unavailable"

# A search that ran out of pages before it could prove either way. This is a third state,
# not a flavour of `no_match`: a bounded first page that happens not to contain the run
# proves nothing about the runs behind it, and reading it as `no_match` is what let an
# accepted dispatch be sent again.
SEARCH_INCOMPLETE = "search_incomplete"

AUTHORITATIVE_ABSENCE_OUTCOMES = (NO_MATCH,)

# Durable record states.
STATE_DISPATCHING = "dispatching"
STATE_UNCERTAIN = "uncertain"
STATE_ACCEPTED = "accepted"
STATE_PENDING = "pending"
UNRESOLVED_STATES = (STATE_DISPATCHING, STATE_UNCERTAIN, STATE_ACCEPTED)
ALLOWED_RECORD_STATES = (STATE_DISPATCHING, STATE_UNCERTAIN, STATE_ACCEPTED, STATE_PENDING)

# Returned outcomes of a dispatch attempt.
OUTCOME_ACKNOWLEDGED = "acknowledged"


class CorruptRecoveryStore(ValueError):
    """An existing recovery store that cannot be trusted to hold the whole truth.

    Distinct from an empty store on purpose. A missing file means "this machine has never
    dispatched anything", which is a complete and safe answer. A file that exists but
    carries no records is ambiguous: it is equally the shape left behind by a write that
    was interrupted, and treating that as an empty queue is how the same observations get
    dispatched twice.
    """


def now_cairo() -> str:
    return datetime.now(CAIRO_TZ).isoformat(timespec="seconds")


# --- durable locations ---------------------------------------------------------
#
# Resolved on every call, not at import, so `STAFF_AGENT_STATE_DIR` can point a *fresh
# process* at the state directory its parent used. That is the only honest way to prove
# recovery survives a restart rather than surviving an in-memory object.


def state_dir() -> Path:
    override = os.environ.get("STAFF_AGENT_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local" / "state" / "staff-agent"


def lock_dir() -> Path:
    if os.environ.get("STAFF_AGENT_STATE_DIR"):
        return state_dir()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "staff-agent"
    return state_dir()


def dispatch_records_path() -> Path:
    return state_dir() / "dispatch-records.json"


def lock_path() -> Path:
    return lock_dir() / "watcher.lock"


def ensure_dirs():
    lock_dir().mkdir(parents=True, exist_ok=True)
    state_dir().mkdir(parents=True, exist_ok=True)


# --- exclusive local lock ------------------------------------------------------


_lock_fd = None


def acquire_lock() -> bool:
    """Take the exclusive watcher lock, or report that another live process holds it.

    `flock` on a descriptor the caller keeps open is the whole mechanism. A marker file
    would only record a PID: two live processes would both read their own PID back as
    valid, and whichever released first would delete the other's claim.
    """
    ensure_dirs()
    global _lock_fd
    try:
        _lock_fd = os.open(str(lock_path()), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        if _lock_fd is not None:
            try:
                os.close(_lock_fd)
            except OSError:
                pass
            _lock_fd = None
        return False


def release_lock():
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None


# --- durable record store ------------------------------------------------------


def load_json(path: Path) -> list:
    """Read a JSON list, or raise.

    A missing file is an empty queue, because "this machine has never dispatched" is a
    complete and provable statement. An existing file is a different claim, and each way
    it can fail is corruption rather than emptiness:

    * zero bytes or whitespace only — the exact shape an interrupted write leaves behind,
      and indistinguishable from an intentional empty queue unless it is refused;
    * truncated or otherwise invalid JSON;
    * bytes that are not UTF-8.

    Returning ``[]`` for any of those would silently discard every dispatch this machine
    still owed, which is indistinguishable from never having dispatched at all — and the
    payload is then dispatched a second time.
    """
    if not path.exists():
        return []
    raw = path.read_bytes()
    if not raw.strip():
        raise CorruptRecoveryStore(
            f"{path} exists but holds no bytes. A store that exists and carries no "
            "records cannot be told apart from a truncated one, so it is treated as "
            "corruption rather than as an empty queue."
        )
    # `json.loads` raises JSONDecodeError and `decode` raises UnicodeDecodeError; both are
    # ValueError subclasses, so a malformed store stops the cycle wherever it is caught.
    return json.loads(raw.decode("utf-8"))


def save_json(path: Path, data) -> None:
    """Replace a JSON file atomically: temp file, fsync, rename, fsync directory.

    A partially written recovery file is worse than no file, because the next process
    reads it as authoritative and cannot tell a truncated record from a complete one.
    """
    ensure_dirs()
    tmp = path.with_name(path.name + ".tmp")
    content = json.dumps(data, indent=2, ensure_ascii=False)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(tmp), str(path))
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


REQUIRED_RECORD_FIELDS = ("correlation_id", "state", "payload", "payload_digest", "created_at")


def validate_record(record, index: int, path: Path) -> None:
    """Refuse one persisted receipt unless every field it depends on is sound.

    Being a JSON object is not enough. A record that lost a field, mistyped one, or whose
    payload no longer hashes to its stored digest cannot be sent, cannot be matched to its
    run, and cannot be aged against the visibility grace — so every rule that reads it
    would be reading a guess. Each check below corresponds to one such rule:

    * ``correlation_id`` names the remote run, so it must be well formed;
    * ``created_at`` orders the record against the grace window, so it must be a real
      timezone-aware timestamp and not merely a parseable string;
    * ``payload_digest`` is the only evidence that the payload is the one that was
      dispatched, so it is recomputed rather than trusted;
    * ``state`` decides whether the payload may be sent again, so an unrecognised value is
      refused instead of being treated as "not pending, therefore safe".

    A record that fails any of these is corruption. It is not dropped, repaired, or left in
    place to be rediscovered: the whole cycle stops, because the one thing this store is for
    is proving what has already been sent.
    """
    where = f"{path}[{index}]"
    if not isinstance(record, dict):
        raise ValueError(f"{where} must be an object, got {type(record).__name__}")
    for field in REQUIRED_RECORD_FIELDS:
        if field not in record:
            raise ValueError(f"{where} is missing required field {field!r}")

    correlation_id = record["correlation_id"]
    if not is_valid_correlation_id(correlation_id):
        raise ValueError(f"{where} correlation_id is not a valid correlation id: {correlation_id!r}")

    state = record["state"]
    if state not in ALLOWED_RECORD_STATES:
        raise ValueError(f"{where} state {state!r} is not one of {ALLOWED_RECORD_STATES}")

    for field in ("created_at", "updated_at"):
        value = record.get(field)
        if value is None and field == "updated_at":
            continue
        if not is_timestamp(value):
            raise ValueError(f"{where} {field} is not a timezone-aware ISO timestamp: {value!r}")

    payload = record["payload"]
    if not isinstance(payload, dict):
        raise ValueError(f"{where} payload must be an object, got {type(payload).__name__}")
    if not payload_is_usable(payload):
        raise ValueError(f"{where} payload carries no sendable observations")

    digest = record["payload_digest"]
    if not isinstance(digest, str):
        raise ValueError(f"{where} payload_digest must be a string, got {type(digest).__name__}")
    recomputed = payload_digest(payload)
    if digest != recomputed:
        raise ValueError(
            f"{where} payload_digest does not match its payload: stored {digest!r}, "
            f"computed {recomputed!r}. The stored payload is not the payload that was dispatched."
        )

    run_id = record.get("run_id")
    if run_id is not None and (isinstance(run_id, bool) or not isinstance(run_id, int)):
        raise ValueError(f"{where} run_id must be an integer or null, got {run_id!r}")
    for field in ("run_status", "run_conclusion"):
        value = record.get(field)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{where} {field} must be a string or null, got {value!r}")


def load_records() -> list[dict]:
    """Read the durable store, or raise. Nothing is silently dropped or repaired.

    A malformed store is not an empty queue. Treating "I cannot read my own receipts" as
    "I owe nothing" is precisely how the same observations get dispatched twice, so every
    shape problem surfaces here and stops the cycle instead.
    """
    path = dispatch_records_path()
    data = load_json(path)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON list")
    for index, record in enumerate(data):
        validate_record(record, index, path)
    return list(data)


def save_records(records: list[dict]) -> None:
    save_json(dispatch_records_path(), records)


def put_record(record: dict) -> None:
    """Insert or replace exactly one record, keyed by its correlation id."""
    records = load_records()
    kept = [r for r in records if r.get("correlation_id") != record["correlation_id"]]
    kept.append(record)
    save_records(kept)


def drop_record(correlation_id: str) -> None:
    """Remove exactly one record. Never the whole file.

    Clearing the entire store to acknowledge one batch would delete a *different*
    dispatch's recovery record, which is the same observation loss with extra steps.
    """
    records = load_records()
    kept = [r for r in records if r.get("correlation_id") != correlation_id]
    if len(kept) != len(records):
        save_records(kept)


def install_record_and_retire(record: dict, superseded: list[str]) -> None:
    """Atomically install a new dispatch receipt and retire the records it replaces.

    One write, because the window between "the new receipt exists" and "the old receipts
    are gone" is exactly the window in which a crash would let the next process send the
    same observations a second time.
    """
    retired = set(superseded) | {record["correlation_id"]}
    records = [r for r in load_records() if r.get("correlation_id") not in retired]
    records.append(record)
    save_records(records)


def records_in_state(records: list[dict], *states: str) -> list[dict]:
    return [r for r in records if r.get("state") in states]


def payload_is_usable(payload) -> bool:
    """A payload we could actually send: an object with a non-empty observations list."""
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("observations"), list)
        and len(payload["observations"]) > 0
    )


def payload_digest(payload) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def new_record(correlation_id: str, payload: dict) -> dict:
    stamp = now_cairo()
    return {
        "correlation_id": correlation_id,
        "payload_digest": payload_digest(payload),
        "payload": payload,
        "created_at": stamp,
        "updated_at": stamp,
        "state": STATE_DISPATCHING,
        "run_id": None,
        "run_status": None,
        "run_conclusion": None,
    }


# --- local repository refresh --------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    if result.returncode != 0:
        return False, ""
    return True, result.stdout.strip()


def refresh_local_repo_safely(read_only: bool = False) -> dict:
    """Bring the working copy level with origin, unless ``read_only`` forbids it.

    ``read_only`` is the dry-run contract: the repository is *inspected* with read-only
    commands and reported, but nothing is fetched, merged, or otherwise moved. A dry run
    that fast-forwards the checkout is not a dry run — it changes the code every later step
    is about to be evaluated against.
    """
    result = {
        "ok": False,
        "dirty": False,
        "was_behind": False,
        "updated": False,
        "read_only": bool(read_only),
        "head": "",
        "origin_head": "",
        "reason": "",
    }
    cwd = REPO_ROOT
    if not cwd.exists():
        result["reason"] = f"repo not found: {cwd}"
        return result
    ok, status = _run_git(["status", "--porcelain"], cwd)
    if not ok:
        result["reason"] = "git status failed"
        return result
    dirty = False
    for line in status.splitlines():
        if len(line) < 2 or line.startswith("??"):
            continue
        if line[0] != " " or line[1] != " ":
            dirty = True
            break
    result["dirty"] = dirty
    if dirty:
        result["reason"] = "worktree has local modifications"
        return result
    ok, head = _run_git(["rev-parse", "HEAD"], cwd)
    if ok:
        result["head"] = head
    ok, origin_head = _run_git(["rev-parse", "origin/main"], cwd)
    if ok:
        result["origin_head"] = origin_head
    if head and origin_head and head != origin_head:
        result["was_behind"] = True
        if read_only:
            result["reason"] = "dry run: repository is behind and was left untouched"
            result["ok"] = True
            return result
        ok, _ = _run_git(["fetch", "origin"], cwd)
        if not ok:
            result["reason"] = "git fetch failed"
            return result
        ok, origin_head = _run_git(["rev-parse", "origin/main"], cwd)
        if ok:
            result["origin_head"] = origin_head
        if head and origin_head and head != origin_head:
            ok, _ = _run_git(["merge", "--ff-only", "origin/main"], cwd)
            if not ok:
                result["reason"] = "ff-only failed"
                return result
            result["updated"] = True
            ok, head = _run_git(["rev-parse", "HEAD"], cwd)
            if ok:
                result["head"] = head
    result["ok"] = True
    return result


# --- observation identity ------------------------------------------------------


def make_observation(project_id: str, report: dict) -> dict:
    """Build one observation, carrying source evidence time separately from scan time.

    ``observed_at`` is when this process looked. ``newest_evidence_at`` is when the
    source says its newest thing happened. Collapsing the second into the first makes a
    2020 status file look like it was updated a moment ago, which would mark a stale
    project fresh and quietly remove it from portfolio attention.
    """
    facts = list(report.get("facts") or [])
    reason = report.get("reason") or ""
    summary = "; ".join(facts) if facts else (reason or "no facts")
    observation = {
        "project_id": project_id,
        "source_type": report["source"],
        "fresh": bool(report["fresh"]),
        "reason": reason,
        "facts": facts[:20],
        "evidence_summary": summary[:300],
        "observed_at": now_cairo(),
        "newest_evidence_at": report.get("newest_evidence_at") or "",
        "source_freshness": "",
    }
    observation["observation_digest"] = observation_identity(observation)
    return observation


# --- exact remote correlation --------------------------------------------------


def parse_remote_timestamp(value):
    """A GitHub or locally persisted ISO-8601 timestamp as an aware datetime, or None.

    Naive and unparseable values are both rejected, because ordering a run against a
    dispatch boundary is the only thing this is for and a bad reading cannot be ordered.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _gh_json(argv: list[str]):
    """Run one read-only ``gh`` query. Returns parsed JSON, or None when unusable.

    Every failure mode here — a non-zero exit, a timeout, a missing binary, output that is
    not JSON, JSON of the wrong shape — is reported the same way, because they are the same
    fact from the watcher's point of view: the question could not be answered.
    """
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=GITHUB_QUERY_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout or "[]")
    except (ValueError, TypeError):
        return None


def _exact_match_in(runs, expected: str) -> tuple[str, dict | None]:
    """The one run in ``runs`` whose published name is exactly ``expected``.

    The name is compared, never inferred. A neighbouring run carrying a different
    correlation id is a different dispatch, and accepting it would hand one payload's
    failure another payload's success.
    """
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("displayTitle") != expected:
            continue
        run_id = run.get("databaseId")
        if run_id in (None, ""):
            # The name matched but the answer cannot identify the run, so it is not a
            # usable answer. Reporting it unavailable keeps us from re-sending.
            return QUERY_UNAVAILABLE, None
        return MATCH, {
            "run_id": run_id,
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
        }
    return NO_MATCH, None


def view_run_by_id(correlation_id: str, run_id) -> tuple[str, dict | None]:
    """Resolve a run from the id already stored on the receipt, without listing anything.

    Once a dispatch has been seen, its run id is a complete lookup key, and the cheapest
    correct question is the direct one. Re-deriving the answer from a recent-runs list
    would be strictly worse: it can only ever answer "not in the newest N", which is
    exactly the failure this removes.

    The stored id is verified against the expected run name before it is trusted, so a
    receipt carrying a stale or wrong id falls through to the search rather than adopting
    somebody else's run.
    """
    if run_id in (None, ""):
        return NO_MATCH, None
    argv = [
        "gh",
        "run",
        "view",
        str(run_id),
        "-R",
        GITHUB_REPO,
        "--json",
        "databaseId,status,conclusion,displayTitle",
    ]
    answer = _gh_json(argv)
    if not isinstance(answer, dict):
        # Unknown run, or an answer we cannot read: fall back to the search, which is
        # slower but can still reach a run this direct query could not.
        return NO_MATCH, None
    return _exact_match_in([answer], run_name_for(correlation_id))


def list_runs_page(limit: int):
    """One page of the workflow's runs, newest first, or None when unqueryable."""
    answer = _gh_json(
        [
            "gh",
            "run",
            "list",
            "-R",
            GITHUB_REPO,
            "--workflow",
            OBSERVATION_WORKFLOW,
            "--limit",
            str(limit),
            "--json",
            "databaseId,status,conclusion,displayTitle,createdAt",
        ]
    )
    if not isinstance(answer, list):
        return None
    return answer


def find_run_by_correlation(correlation_id: str, created_at=None, run_id=None) -> tuple[str, dict | None]:
    """Resolve the ONE remote run whose identity proves it is this dispatch.

    Returns ``(MATCH, {"run_id", "status", "conclusion"})``, ``(NO_MATCH, None)``,
    ``(QUERY_UNAVAILABLE, None)`` or ``(SEARCH_INCOMPLETE, None)``.

    The workflow publishes ``run-name: project-observation-<correlation_id>``, so the
    decision is an exact comparison of the expected name against each run's remote display
    title. Run age, workflow, branch and "a run exists at all" take no part in it: a nearby
    run carrying a different correlation id is a different dispatch, and accepting it would
    hand one payload's failure another payload's success.

    ``NO_MATCH`` is returned only when the search was *authoritative* — when it saw the end
    of the workflow's history, or when every run it saw was already older than this
    dispatch was created. A search that ran out of pages first returns
    ``SEARCH_INCOMPLETE``, which callers must not read as permission to send again.
    """
    expected = run_name_for(correlation_id)

    if run_id not in (None, ""):
        outcome, run = view_run_by_id(correlation_id, run_id)
        if outcome != NO_MATCH:
            return outcome, run

    boundary = parse_remote_timestamp(created_at)

    limit = GITHUB_QUERY_PAGE_SIZE
    for _page in range(1, GITHUB_QUERY_MAX_PAGES + 1):
        runs = list_runs_page(limit)
        if runs is None:
            return QUERY_UNAVAILABLE, None

        outcome, run = _exact_match_in(runs, expected)
        if outcome != NO_MATCH:
            return outcome, run

        if len(runs) < limit:
            # Fewer runs came back than were asked for, so the workflow's whole history
            # has now been seen. There is nothing behind this page.
            return NO_MATCH, None

        if boundary is not None and _page_is_older_than(runs, boundary):
            # This page reached back past the moment the dispatch was created, and the
            # exact run was not in it. An older run cannot be this dispatch.
            return NO_MATCH, None

        limit += GITHUB_QUERY_PAGE_SIZE

    return SEARCH_INCOMPLETE, None


def _page_is_older_than(runs: list, boundary: datetime) -> bool:
    """True when every run on this page predates the dispatch being looked for.

    A run whose timestamp cannot be read is treated as *not* older, so an unreadable row
    keeps the search going instead of manufacturing an authoritative absence.
    """
    if not runs:
        return False
    unreadable = datetime.max.replace(tzinfo=timezone.utc)
    for run in runs:
        if not isinstance(run, dict):
            return False
        created = parse_remote_timestamp(run.get("createdAt")) or unreadable
        if not created < boundary:
            return False
    return True


def visibility_grace_remaining(record: dict, now: datetime | None = None) -> float:
    """Seconds of visibility grace this receipt has left, from its persisted timestamp.

    Stored on the record rather than held in memory, so a restart cannot silently reset the
    window and hand a still-invisible run back to the redispatch path.
    """
    created = parse_remote_timestamp(record.get("created_at"))
    if created is None:
        return 0.0
    reference = now if now is not None else datetime.now(created.tzinfo)
    return max(0.0, VISIBILITY_GRACE_SECONDS - (reference - created).total_seconds())


def _run_success(conclusion) -> bool:
    """Only an explicitly successful run counts as delivered.

    A failed or cancelled run carrying the exact correlation id *is* the exact run. It
    is a failure to recover from, not a different run to go looking for, and it must
    never be recorded as an accepted dispatch.
    """
    return conclusion == "success"


# --- durable dispatch state machine -------------------------------------------


def settled_state(status, conclusion) -> str:
    """The state an exact run implies. Pure: it reads the run and decides, nothing else.

    Kept separate from the write so a dry run can be told what *would* happen without
    performing any of it.
    """
    if is_in_flight(status):
        return STATE_ACCEPTED
    if _run_success(conclusion):
        return OUTCOME_ACKNOWLEDGED
    return STATE_PENDING


def settle_record(record: dict, status, conclusion, run_id) -> str:
    """Reduce an exact run to the record's next state, and persist it.

    Returns the new state, or `acknowledged` when the record was removed.
    """
    state = settled_state(status, conclusion)
    if state == OUTCOME_ACKNOWLEDGED:
        drop_record(record["correlation_id"])
        return OUTCOME_ACKNOWLEDGED
    record["run_status"] = status
    record["run_conclusion"] = conclusion
    record["run_id"] = run_id
    record["state"] = state
    record["updated_at"] = now_cairo()
    put_record(record)
    return state


def _mark_state(record: dict, state: str) -> None:
    record["state"] = state
    record["updated_at"] = now_cairo()
    put_record(record)


def dispatch_observations(payload: dict, superseded: list[str] | None = None) -> tuple[str, dict]:
    """Persist a dispatch receipt, dispatch it, then resolve the *exact* remote run.

    The receipt is written before the request leaves this process. That ordering is the
    whole recovery guarantee: a timeout, a crash or a reboot afterwards still leaves a
    correlation id on disk for the next process to resolve, instead of leaving only an
    intention that no longer exists anywhere.
    """
    correlation_id = generate_correlation_id()
    # The writer requires the correlation id inside the payload, and the digest below must
    # cover it, so the id is minted and injected here — not at the call site.
    payload = {**payload, "correlation_id": correlation_id}
    record = new_record(correlation_id, payload)
    install_record_and_retire(record, list(superseded or []))

    argv = [
        "gh",
        "workflow",
        "run",
        OBSERVATION_WORKFLOW,
        "-R",
        GITHUB_REPO,
        "-f",
        "payload={}".format(json.dumps(payload, ensure_ascii=False)),
        "-f",
        "correlation_id={}".format(correlation_id),
    ]
    dispatched = False
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=DISPATCH_TIMEOUT_SECONDS,
        )
        dispatched = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        dispatched = False

    outcome, run = find_run_by_correlation(correlation_id, created_at=record["created_at"])
    if outcome == MATCH:
        state = settle_record(record, run["status"], run["conclusion"], run["run_id"])
        return state, record
    if outcome in (QUERY_UNAVAILABLE, SEARCH_INCOMPLETE):
        _mark_state(record, STATE_UNCERTAIN)
        return STATE_UNCERTAIN, record
    if dispatched:
        # GitHub accepted the request but the run is not listed yet. Leave the record
        # unresolved so the next run resolves it by correlation id; re-sending now is the
        # one response guaranteed to duplicate.
        _mark_state(record, STATE_DISPATCHING)
        return STATE_DISPATCHING, record
    if visibility_grace_remaining(record) > 0:
        # The transport gave no usable answer either way, and the run may still be inside
        # GitHub's visibility delay. Holding the receipt beats re-sending on a guess.
        _mark_state(record, STATE_UNCERTAIN)
        return STATE_UNCERTAIN, record
    _mark_state(record, STATE_PENDING)
    return STATE_PENDING, record


def reconcile_dispatch(record: dict, dry_run: bool = False, now: datetime | None = None) -> tuple[str, dict]:
    """Resolve exactly one durable record, by its own exact correlation id.

    Returns ``(next_state, record)``. ``acknowledged`` means the record was removed because
    the exact run completed successfully; ``accepted`` means it is still in flight and the
    payload stays owned by the remote run; ``pending`` means an *authoritative* lookup
    proved the run does not exist and the visibility grace has expired, so the payload is
    safe to send again; ``uncertain`` means the answer is not proof of absence — GitHub
    could not be asked, the search could not be completed, or the run may still be inside
    the visibility window.

    With ``dry_run`` the same decision is reached and none of it is performed: no state is
    stored, no record is dropped, and what is on disk afterwards is byte-identical.
    """
    correlation_id = record["correlation_id"]
    # A stored run id is a complete lookup key, so it is used directly rather than
    # re-derived from a recent-runs list that may no longer include the run at all.
    outcome, run = find_run_by_correlation(
        correlation_id,
        created_at=record.get("created_at"),
        run_id=record.get("run_id"),
    )

    if outcome in (QUERY_UNAVAILABLE, SEARCH_INCOMPLETE):
        state = STATE_UNCERTAIN
    elif outcome == NO_MATCH:
        # An authoritative absence still waits out the visibility window: a run GitHub
        # already accepted can be missing from every list for a short while, and "not
        # listed yet" is not "never dispatched".
        state = STATE_UNCERTAIN if visibility_grace_remaining(record, now) > 0 else STATE_PENDING
    else:
        state = settled_state(run["status"], run["conclusion"])

    if dry_run:
        return state, record

    if outcome == MATCH:
        settle_record(record, run["status"], run["conclusion"], run["run_id"])
    elif state == STATE_UNCERTAIN:
        _mark_state(record, STATE_UNCERTAIN)
    elif state == STATE_PENDING:
        _mark_state(record, STATE_PENDING)
    return state, record


def reconcile_records(dry_run: bool = False) -> dict:
    """Resolve every durable record an earlier process left behind.

    Runs before any new project is inspected, so an unresolved dispatch is never re-sent.
    A record this process cannot resolve keeps its payload and stays unresolved — a
    question left open is not permission to send the same observations again.
    """
    summary = {
        STATE_ACCEPTED: 0,
        STATE_PENDING: 0,
        STATE_UNCERTAIN: 0,
        STATE_DISPATCHING: 0,
        OUTCOME_ACKNOWLEDGED: 0,
    }
    for record in load_records():
        state, _ = reconcile_dispatch(record, dry_run=dry_run)
        summary[state] = summary.get(state, 0) + 1
    return summary


# --- batching ------------------------------------------------------------------


def _unresolved_identities(records: list[dict]) -> set[str]:
    """Observation identities already owned by a dispatch that has not concluded.

    A queued run has the payload on GitHub's side. Discovering the same evidence again on
    a later scan is not new work, and treating it as new work would dispatch it twice.
    """
    keys: set[str] = set()
    for record in records_in_state(records, *UNRESOLVED_STATES):
        payload = record.get("payload")
        if not payload_is_usable(payload):
            continue
        for obs in payload["observations"]:
            if isinstance(obs, dict):
                keys.add(project_scoped_identity(obs))
    return keys


def _candidate_observations(records: list[dict], candidates: list[dict]):
    yield from observations_of(records_in_state(records, STATE_PENDING))
    owned = _unresolved_identities(records)
    for obs in candidates:
        if project_scoped_identity(obs) in owned:
            continue
        yield obs


def observations_of(records: list[dict]):
    for record in records:
        payload = record.get("payload")
        if not payload_is_usable(payload):
            continue
        for obs in payload["observations"]:
            if isinstance(obs, dict):
                yield obs


def build_batch(records: list[dict], candidates: list[dict]) -> tuple[list[dict], list[str]]:
    """Assemble the bounded batch and report which pending records it fully replaces.

    Records are retired only when every observation they carried is inside the batch. A
    pending record whose payload overflowed the bound stays pending, because dropping it
    would discard observations this run could not send.
    """
    keys: set[str] = set()
    ordered: list[dict] = []
    for obs in _candidate_observations(records, candidates):
        key = project_scoped_identity(obs)
        if key in keys:
            continue
        keys.add(key)
        ordered.append(obs)

    batch = ordered[:MAX_OBSERVATIONS]
    included = {project_scoped_identity(obs) for obs in batch}
    superseded = [
        record["correlation_id"]
        for record in records_in_state(records, STATE_PENDING)
        if payload_is_usable(record.get("payload"))
        and all(project_scoped_identity(obs) in included for obs in record["payload"]["observations"])
    ]
    return batch, superseded


def build_payload(observations: list[dict]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "observations": observations,
    }


# --- main ----------------------------------------------------------------------


def inspect_projects(catalog: dict, sources) -> list[dict]:
    """Read every registered, active, local-only project and return new observations."""
    import src.events as events_log

    observations: list[dict] = []
    for pid, entry in catalog.items():
        if entry.tracking != "active":
            continue
        if entry.evidence_source == "event_only":
            continue
        if entry.refreshable_in_actions != "no":
            # This watcher runs on the user's machine. A project Actions can refresh
            # itself is refreshed by its own workflow; reading it here too would only
            # produce a second, redundant dispatch of the same evidence.
            continue
        try:
            report = sources.inspect(pid, entry.evidence_source, entry.evidence_path)
        except (OSError, subprocess.SubprocessError):
            continue
        observation = make_observation(pid, report)
        if observation_identity(observation) in events_log.applied_observation_identities(pid):
            continue
        observations.append(observation)
    return observations


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-dispatch", action="store_true")
    args = parser.parse_args()

    # A dry run takes no lock, because creating the lock file is itself a write and a dry
    # run dispatches nothing that would need serialising.
    locked = False
    if not args.dry_run:
        if not acquire_lock():
            print("watcher already running, exiting", file=sys.stderr)
            return 0
        locked = True

    try:
        # Reconciliation comes first, before anything new is inspected: a dispatch this
        # machine already made and cannot yet explain outranks any newly seen evidence.
        try:
            summary = reconcile_records(dry_run=args.dry_run)
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            # Fail closed. The store is the only durable proof of what this machine may
            # already have sent; an unreadable one is not an empty one. Refuse to run
            # rather than silently dispatch again on top of a lost receipt.
            print(
                f"dispatch record store at {dispatch_records_path()} is unreadable: {exc}; "
                "refusing to dispatch until it is repaired",
                file=sys.stderr,
            )
            return 1
        if summary:
            print("reconciled: " + json.dumps(summary, sort_keys=True))

        repo_state = refresh_local_repo_safely(read_only=args.dry_run)
        if not repo_state["ok"]:
            if repo_state["dirty"]:
                print(f"repo not clean, skipping: {repo_state['reason']}", file=sys.stderr)
            else:
                print(f"repo refresh failed: {repo_state['reason']}", file=sys.stderr)
            return 0

        import src.project_sources as sources
        import src.projects as registry

        catalog = registry.load_registry()
        eligible = [
            (pid, entry)
            for pid, entry in catalog.items()
            if entry.tracking == "active"
            and entry.evidence_source != "event_only"
            and entry.refreshable_in_actions == "no"
        ]

        records = load_records()
        candidates = inspect_projects(catalog, sources)
        batch, superseded = build_batch(records, candidates)

        if args.dry_run:
            print(
                json.dumps(
                    {
                        "eligible": len(eligible),
                        "inspected": len(eligible),
                        "new_observations": len(candidates),
                        "pending_records": len(records_in_state(records, STATE_PENDING)),
                        "unresolved_records": len(records_in_state(records, *UNRESOLVED_STATES)),
                        "would_dispatch": len(batch),
                        "would_reconcile": {
                            key: value for key, value in summary.items() if value
                        },
                        "would_retire": len(superseded),
                        "repository": {
                            "read_only": True,
                            "behind": repo_state.get("was_behind", False),
                            "fetched": False,
                            "merged": False,
                        },
                    },
                    indent=2,
                )
            )
            return 0

        if not batch:
            print("nothing to dispatch")
            return 0

        payload = build_payload(batch)
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            print(
                f"payload of {len(encoded)} bytes exceeds the {MAX_PAYLOAD_BYTES} byte transport limit",
                file=sys.stderr,
            )
            return 1

        if args.force_dispatch:
            # Print a payload that is directly applicable, correlation id included.
            print(json.dumps({**payload, "correlation_id": generate_correlation_id()}, indent=2))
            return 0

        outcome, record = dispatch_observations(payload, superseded)
        correlation_id = record["correlation_id"]
        if outcome == OUTCOME_ACKNOWLEDGED:
            print(f"dispatch {correlation_id}: run {record['run_id']} completed successfully")
            return 0
        if outcome == STATE_ACCEPTED:
            print(f"dispatch {correlation_id}: run {record['run_id']} is {record['run_status']}")
            return 0
        if outcome == STATE_DISPATCHING:
            print(f"dispatch {correlation_id}: accepted by GitHub, not yet listed; will reconcile next run")
            return 0
        if outcome == STATE_PENDING:
            print(
                f"dispatch {correlation_id}: no exact run on GitHub; {len(batch)} observation(s) kept as pending",
                file=sys.stderr,
            )
            return 1
        print(
            f"dispatch {correlation_id}: GitHub could not be queried; {len(batch)} observation(s) kept as uncertain",
            file=sys.stderr,
        )
        return 1
    finally:
        if locked:
            release_lock()


if __name__ == "__main__":
    sys.exit(main())
