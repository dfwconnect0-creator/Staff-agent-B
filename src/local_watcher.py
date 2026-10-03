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
    is_valid_correlation_id,
    observation_identity,
    project_scoped_identity,
    run_name_for,
)

CAIRO_TZ = timezone(timedelta(hours=3))

GITHUB_REPO = "dfwconnect0-creator/Staff-agent-B"
OBSERVATION_WORKFLOW = "project-observation.yml"
GITHUB_QUERY_LIMIT = 50
DISPATCH_TIMEOUT_SECONDS = 60
GITHUB_QUERY_TIMEOUT_SECONDS = 30

# Match outcomes. `query_unavailable` is deliberately not `no_match`: "I could not ask"
# and "it does not exist" lead to opposite actions, and collapsing them is how a
# dispatch that GitHub already accepted gets sent a second time.
MATCH = "match"
NO_MATCH = "no_match"
QUERY_UNAVAILABLE = "query_unavailable"

# Durable record states.
STATE_DISPATCHING = "dispatching"
STATE_UNCERTAIN = "uncertain"
STATE_ACCEPTED = "accepted"
STATE_PENDING = "pending"
UNRESOLVED_STATES = (STATE_DISPATCHING, STATE_UNCERTAIN, STATE_ACCEPTED)

# Returned outcomes of a dispatch attempt.
OUTCOME_ACKNOWLEDGED = "acknowledged"


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

    A missing file is an empty queue. An unreadable or malformed one is an error, because
    returning `[]` for a corrupt recovery file would silently discard every dispatch the
    watcher still owed, which is indistinguishable from never having dispatched at all.
    """
    ensure_dirs()
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return []
    return json.loads(text)


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


def load_records() -> list[dict]:
    """Read the durable store, or raise. Nothing is silently dropped or repaired.

    A malformed store is not an empty queue. Treating "I cannot read my own receipts"
    as "I owe nothing" is precisely how the same observations get dispatched twice, so
    every shape problem surfaces here and stops the cycle instead.
    """
    data = load_json(dispatch_records_path())
    if not isinstance(data, list):
        raise ValueError(f"{dispatch_records_path()} must contain a JSON list")
    for index, record in enumerate(data):
        if not isinstance(record, dict):
            raise ValueError(
                f"{dispatch_records_path()}[{index}] must be an object, "
                f"got {type(record).__name__}"
            )
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


def refresh_local_repo_safely() -> dict:
    result = {
        "ok": False,
        "dirty": False,
        "was_behind": False,
        "updated": False,
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
    ok, _ = _run_git(["fetch", "origin"], cwd)
    if not ok:
        result["reason"] = "git fetch failed"
        return result
    ok, origin_head = _run_git(["rev-parse", "origin/main"], cwd)
    if ok:
        result["origin_head"] = origin_head
    if head and origin_head and head != origin_head:
        result["was_behind"] = True
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


def find_run_by_correlation(correlation_id: str) -> tuple[str, dict | None]:
    """Resolve the ONE remote run whose identity proves it is this dispatch.

    Returns ``(MATCH, {"run_id", "status", "conclusion"})``, ``(NO_MATCH, None)`` or
    ``(QUERY_UNAVAILABLE, None)``.

    The workflow publishes ``run-name: project-observation-<correlation_id>``, so the
    decision is an exact comparison of the expected name against each run's remote
    display title. Run age, workflow, branch and "a run exists at all" take no part in
    it: a nearby run carrying a different correlation id is a different dispatch, and
    accepting it would hand one payload's failure another payload's success.
    """
    expected = run_name_for(correlation_id)
    argv = [
        "gh",
        "run",
        "list",
        "-R",
        GITHUB_REPO,
        "--workflow",
        OBSERVATION_WORKFLOW,
        "--limit",
        str(GITHUB_QUERY_LIMIT),
        "--json",
        "databaseId,status,conclusion,displayTitle",
    ]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=GITHUB_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return QUERY_UNAVAILABLE, None
    if result.returncode != 0:
        return QUERY_UNAVAILABLE, None
    try:
        runs = json.loads(result.stdout or "[]")
    except (ValueError, TypeError):
        return QUERY_UNAVAILABLE, None
    if not isinstance(runs, list):
        return QUERY_UNAVAILABLE, None
    for run in runs:
        if not isinstance(run, dict):
            continue
        if run.get("displayTitle") != expected:
            continue
        run_id = run.get("databaseId")
        if run_id in (None, ""):
            # The name matched but the answer cannot identify the run, so it is not a
            # usable answer. Reporting it as unavailable keeps us from re-sending.
            return QUERY_UNAVAILABLE, None
        return MATCH, {
            "run_id": run_id,
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
        }
    return NO_MATCH, None


def _run_success(conclusion) -> bool:
    """Only an explicitly successful run counts as delivered.

    A failed or cancelled run carrying the exact correlation id *is* the exact run. It
    is a failure to recover from, not a different run to go looking for, and it must
    never be recorded as an accepted dispatch.
    """
    return conclusion == "success"


# --- durable dispatch state machine -------------------------------------------


def settle_record(record: dict, status, conclusion, run_id) -> str:
    """Reduce an exact run to the record's next state, and persist it.

    Returns the new state, or `acknowledged` when the record was removed.
    """
    if is_in_flight(status):
        record["run_status"] = status
        record["run_conclusion"] = conclusion
        record["run_id"] = run_id
        record["state"] = STATE_ACCEPTED
        record["updated_at"] = now_cairo()
        put_record(record)
        return STATE_ACCEPTED
    if _run_success(conclusion):
        drop_record(record["correlation_id"])
        return OUTCOME_ACKNOWLEDGED
    record["run_status"] = status
    record["run_conclusion"] = conclusion
    record["run_id"] = run_id
    record["state"] = STATE_PENDING
    record["updated_at"] = now_cairo()
    put_record(record)
    return STATE_PENDING


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

    outcome, run = find_run_by_correlation(correlation_id)
    if outcome == MATCH:
        state = settle_record(record, run["status"], run["conclusion"], run["run_id"])
        return state, record
    if outcome == QUERY_UNAVAILABLE:
        _mark_state(record, STATE_UNCERTAIN)
        return STATE_UNCERTAIN, record
    if dispatched:
        # GitHub accepted the request but the run is not listed yet. Leave the record
        # unresolved so the next run resolves it by correlation id; re-sending now is
        # the one response guaranteed to duplicate.
        _mark_state(record, STATE_DISPATCHING)
        return STATE_DISPATCHING, record
    _mark_state(record, STATE_PENDING)
    return STATE_PENDING, record


def reconcile_dispatch(record: dict, dry_run: bool = False) -> tuple[str, dict]:
    """Resolve exactly one durable record, by its own exact correlation id.

    Returns ``(next_state, record)``. ``acknowledged`` means the record was removed because
    the exact run completed successfully; ``accepted`` means it is still in flight and the
    payload stays owned by the remote run; ``pending`` means a *reliable* lookup proved the
    run does not exist, so the payload is safe to send again; ``uncertain`` means GitHub
    could not be asked, which is not permission to send it again.
    """
    correlation_id = record.get("correlation_id")
    if not is_valid_correlation_id(correlation_id) or not payload_is_usable(record.get("payload")):
        # Fail closed: a receipt that cannot be sent or cannot be named is never
        # dispatched and never deleted, because deleting it would lose the observations
        # it stands for.
        if not dry_run and record.get("state") != STATE_UNCERTAIN:
            _mark_state(record, STATE_UNCERTAIN)
        return STATE_UNCERTAIN, record

    outcome, run = find_run_by_correlation(correlation_id)
    if outcome == QUERY_UNAVAILABLE:
        if not dry_run:
            _mark_state(record, STATE_UNCERTAIN)
        return STATE_UNCERTAIN, record
    if outcome == NO_MATCH:
        if not dry_run:
            _mark_state(record, STATE_PENDING)
        return STATE_PENDING, record
    state = settle_record(record, run["status"], run["conclusion"], run["run_id"])
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

    if not acquire_lock():
        print("watcher already running, exiting", file=sys.stderr)
        return 0

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

        repo_state = refresh_local_repo_safely()
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
        release_lock()


if __name__ == "__main__":
    sys.exit(main())
