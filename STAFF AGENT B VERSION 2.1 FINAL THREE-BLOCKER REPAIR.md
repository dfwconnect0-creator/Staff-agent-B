# STAFF AGENT B — VERSION 2.1 FINAL THREE-BLOCKER REPAIR

Repair of the three activation blockers reproduced by the second independent Codex re-audit
of commit `e0a80ca7af48aec89e7068f2dd45e6c9ef19e5e4`. Scope was held to those three
blockers and the tests that prove them. No live canary was performed. The production timer
was not activated.

## 1. Starting State

| | |
|---|---|
| Audited commit | `e0a80ca7af48aec89e7068f2dd45e6c9ef19e5e4` |
| `origin/main` at start | `e0a80ca7af48aec89e7068f2dd45e6c9ef19e5e4` (identical — nothing to reconcile) |
| Branch | `main`, tracking `origin/main`, fast-forward only, never reset |
| Baseline suite | **448 passed**, 0 failed, 0 skipped |
| Historical artifact verification | **19/19 checks passed** |
| Implementation under repair | `src/local_watcher.py` (816 lines at `e0a80ca`) |

All three blockers were confirmed in the source before any edit was made. Each was a real
defect, not a misreading by the auditor:

| Blocker | Confirmed at `e0a80ca` | Defect |
|---|---|---|
| A | `load_json` line 163 | `if not text.strip(): return []` — a zero-byte or whitespace-only store read as an empty queue |
| A | `load_records` line 206 | Only `isinstance(record, dict)`; no field, type, or digest validation |
| B | `find_run_by_correlation` line 411 | Hardcoded `--limit 50`, returning `NO_MATCH` at line 447 |
| B | `reconcile_dispatch` line 568 | `record["run_id"]` never read; no direct lookup existed |
| C | `reconcile_dispatch` line 577 | `settle_record` called unguarded by `dry_run`, and it calls `put_record`/`drop_record` |
| C | `main` line 731 | `refresh_local_repo_safely()` ran unconditionally, performing `git fetch` and `git merge --ff-only` |

## 2. Blank/Corrupt Recovery Repair

`load_json` no longer converts an existing file into an empty list. A new exception,
`CorruptRecoveryStore(ValueError)`, distinguishes "this store cannot be trusted to hold the
whole truth" from "there is nothing here", and `main` already failed closed on `ValueError`,
so the corrupt case stops the cycle before any project is inspected.

`load_records` now runs `validate_record` over every entry. Being a JSON object is no longer
sufficient; each check corresponds to one rule that would otherwise read a guess:

| Input | Before | After |
|---|---|---|
| missing file | `[]` | `[]` — a complete, provable statement |
| valid JSON `[]` | `[]` | `[]` — an intentional empty store |
| **zero-byte existing file** | `[]` | **`CorruptRecoveryStore`** — exactly the shape an interrupted write leaves |
| **whitespace-only existing file** | `[]` | **`CorruptRecoveryStore`** |
| truncated JSON | raises | raises (`JSONDecodeError`) |
| invalid JSON | raises | raises |
| non-UTF-8 bytes | raises | raises (`UnicodeDecodeError`) |
| wrong top-level type | raises | raises (`ValueError`) |
| **invalid record shape** | kept and re-discovered | **`ValueError`, cycle stops** |
| **payload digest mismatch** | not checked | **`ValueError`, cycle stops** |

Validated per record: `correlation_id` format, `state` membership, `payload` object with
sendable observations, `payload_digest` **recomputed and compared**, `created_at` as a
timezone-aware timestamp, and `run_id`/`run_status`/`run_conclusion` typed when present.

The digest check is a recomputation, not a format test. A test that rewrites a stored
payload *and reseals its digest* passes, which is what shows the check is integrity rather
than ceremony.

Two consequences, both intended and both changes of prior behaviour:

- A malformed record no longer leaves the cycle running on the entries that happen to parse.
  The store is the only proof of what was sent, so acting on part of an untrustworthy store
  is not safe. The refused store is left byte-identical on disk.
- `validate_record` requires a usable payload, which makes the old "unusable payload →
  uncertain" branch in `reconcile_dispatch` unreachable. Fail-closed at load is strictly
  earlier than fail-closed at reconcile.

## 3. GitHub Lookup / Duplicate Dispatch Repair

A new fourth outcome, `search_incomplete`, sits alongside `match`, `no_match` and
`query_unavailable`. The rule it encodes: **only an authoritative absence may lead to a
resend.**

### Stored run id, when there is one

`view_run_by_id` issues `gh run view <run_id>` for the id already on the receipt. No listing
happens at all. The stored id is verified against the expected run name before it is trusted,
so a stale or wrong id falls through to the search rather than adopting another run's result.
An unknown id also falls through to the search.

### Before the run id is known

`find_run_by_correlation` paginates with a growing `--limit`, `GITHUB_QUERY_PAGE_SIZE` (100)
per step up to `GITHUB_QUERY_MAX_PAGES` (20). It returns `NO_MATCH` **only** when one of
these holds:

- **the whole history was seen** — fewer runs came back than were requested; or
- **the dispatch-time boundary was crossed** — every run on the page predates the receipt's
  persisted `created_at`, using each run's `createdAt`.

If the page cap is reached first, the result is `search_incomplete`, never `no_match`. A row
whose timestamp cannot be parsed counts as *not* older, so an unreadable row keeps the search
going rather than manufacturing an absence. The bound is therefore a bound on work that
corresponds to the dispatch's own time, not "the first 50 runs".

### Visibility grace

`visibility_grace_remaining` is computed from the receipt's **persisted** `created_at` and
`VISIBILITY_GRACE_SECONDS` (900s). It survives restart because it is never held in memory.
An authoritative negative inside the grace yields `uncertain`, not `pending`. Past the grace,
an authoritative negative yields `pending`. There is no busy loop: the grace is a fixed
window, after which one complete search settles the question.

## 4. Dry-Run Purity

`settled_state` is now a pure function of the run; `settle_record` performs the write.
`reconcile_dispatch` decides first and writes only when `dry_run` is false, so a dry run
reaches exactly the same decision and performs none of it.

`refresh_local_repo_safely(read_only=...)` uses `git status --porcelain` and
`git rev-parse` only. `git fetch` and `git merge --ff-only` are unreachable on that path.
A dry run takes no lock, because creating the lock file is itself a write. `load_json` no
longer calls `ensure_dirs`, so reading cannot create a state directory.

Byte-for-byte proof, from `tests/test_watcher_dry_run_purity.py`. Before and after a dry run
the test compares a SHA-256 of the recovery store, a SHA-256 per **git-tracked** file,
`git rev-parse HEAD`, `git rev-parse origin/main`, and `git status --porcelain`:

| Case | Reconciliation would have | After dry run |
|---|---|---|
| queued exact run | `uncertain → accepted` | store digest unchanged, state still `dispatching` |
| failed exact run | `uncertain → pending` | store digest unchanged, state still `dispatching` |
| **successful exact run** | **deletes the receipt and its payload** | store digest unchanged, receipt and payload intact |
| no store existed | creates one | still does not exist |
| no state directory | creates it | still does not exist |

Git command tracing, in a fresh child interpreter, asserts the issued subcommands contain no
`fetch`, `merge`, `push` or `commit`. A separate test builds a real clone left genuinely
behind its origin and asserts the read-only path issues exactly `["status", "rev-parse",
"rev-parse"]` and leaves `HEAD` unchanged.

The report gained `would_reconcile`, `would_retire`, and a `repository` block stating
`read_only`/`behind`/`fetched`/`merged`. Two existing dry-run tests were updated because the
report gained keys; neither was weakened.

## 5. Codex Failure Reproductions

### Zero-byte duplicate dispatch

Process 1 and process 2 are separate interpreters sharing only the state directory on disk,
with GitHub stubbed inside the child.

```
process 1  -> persists an accepted/in-flight receipt, exits        dispatches: 1
             store truncated to zero bytes
process 2  -> exits 1, stderr names the store as unreadable        dispatches: 0
                                                             TOTAL: 1
```

Before the repair, process 2 read the zero-byte file as `[]`, rediscovered the same
observations and dispatched them again. The test also asserts the corrupt store is left
byte-identical, so refusing to act is not the same as destroying the evidence. A
whitespace-only variant is reproduced identically.

### More-than-50-run duplicate dispatch

```
process 1  -> dispatches; run accepted remotely; run id not yet known   dispatches: 1
             60 newer unrelated runs appear ahead of it
process 2  -> paginates past the newer runs, finds the exact run        dispatches: 0
             summary: accepted=1, record retained with run_id
                                                             TOTAL: 1
```

The correlation id is taken from process 1's actual output, so the remote run's name cannot
be rigged to match.

### Dry-run mutation

`uncertain → accepted`, `uncertain → pending`, and `successful receipt → deleted` were all
observed under the previous code path. All three are now covered by byte-for-byte tests that
fail against `e0a80ca`.

## 6. Focused Tests

66 new tests across three files, plus one added to an existing file.

| File | Tests | Covers spec items |
|---|---|---|
| `tests/test_watcher_store_integrity.py` | 37 | 1–10, 22 |
| `tests/test_watcher_duplicate_dispatch.py` | 18 | 11–16, 23 |
| `tests/test_watcher_dry_run_purity.py` | 11 | 17–21 |

- Restart tests use **real fresh child interpreters** via `python -c`, sharing only the state
  directory. No in-memory object is carried across a simulated restart.
- The GitHub simulator enforces **actual pagination semantics**: it honours `--limit` by
  slicing `runs[:limit]`, in both the in-process double and the child stub, so a test cannot
  accidentally prove pagination with a transport that has none.
- Both doubles record every argv, and an `atexit` handler emits the trace so it survives a
  non-zero exit. `dispatch_count(calls)` and `git_subcommands(calls)` are asserted directly.
- Dispatch counting spans both processes, so "one dispatch total" is a real measurement.
- The child stub raises on any `gh workflow run` during reconciliation, so an accidental
  dispatch fails loudly rather than passing unnoticed.

**Non-vacuity was verified, not assumed.** With `src/local_watcher.py` reverted to its state
at `e0a80ca` and the new tests unchanged, **50 of the 66 fail**. The 16 that still pass are
the cases the old code already handled, such as a genuinely missing store reading as empty.
A test suite that passed against the broken implementation would prove nothing.

Four existing tests changed expectation, each because it encoded one of the three blockers.
No other test was altered:

| Test | Was | Now | Why |
|---|---|---|---|
| `test_a_timeout_with_a_reliable_no_match_moves_the_record_to_pending` | `pending` | split into an in-grace test and an after-grace test | "one empty list ⇒ resend" is Blocker B |
| `test_a_fresh_process_that_finds_a_reliable_no_run_moves_it_to_pending` | `pending` | holds `uncertain` | a restart must not reset the grace |
| `test_a_pending_record_is_retried_...` | immediate `pending` | ages past the grace first | the resend must be earned |
| `test_an_unreadable_record_is_never_silently_dropped` | kept and carried on | whole cycle refuses, store byte-identical | Blocker A fails closed |

Nine `refresh_local_repo_safely` test doubles gained `**_kwargs` to match the new signature.
A latent bug in both transport doubles was fixed: `argv[:2] == ("git",)` can never match,
since a git command's first two words are `("git", "<subcommand>")`.

## 7. Full Regression

```
collected   515
passed      515
failed        0
skipped       0
deselected    0
```

Historical artifact verification, run separately:

```
19/19 checks passed
```

Per the repair instruction, safety is not claimed from the count alone. Preserved repairs
re-verified by name, all passing:

| §6 requirement | Test |
|---|---|
| callable watcher main | `test_the_module_has_a_callable_main_that_returns_an_exit_code` |
| exact positive correlation | `test_exact_correlation_matches_the_expected_run` |
| QUERY_UNAVAILABLE ≠ authoritative absence | `test_a_query_failure_is_reported_as_unavailable_not_as_no_run`, `test_an_unavailable_query_is_uncertain_not_pending` |
| historical A → B → old A rejection | `test_a_delayed_replay_of_a_after_b_is_rejected` |
| legitimate new later A acceptance | `test_a_genuinely_newer_a_is_accepted_after_b` |
| evidence-time freshness | `test_the_writer_uses_evidence_time_for_freshness_not_the_scan_clock`, `test_identity_ignores_the_scan_clock` |
| whole-payload validation | `test_an_unknown_project_rejects_the_whole_batch`, `test_the_malformed_payload_itself_is_rejected` |
| shell-safe workflow transport | `test_a_payload_with_shell_metacharacters_stays_one_argument` |
| atomic write | `test_the_outbox_is_replaced_atomically_and_never_truncated_in_place` |
| exclusive flock | `test_the_lock_is_a_real_flock_not_a_marker_file` |
| no LLM | `test_the_whole_cycle_makes_no_llm_call` |
| no Telegram | `test_the_whole_cycle_makes_no_telegram_call` |

## 8. Git State

Minimal commits, no force push, pushed normally. `HEAD == origin/main` verified at the end.

Modified: `src/local_watcher.py`, `tests/watcher_support.py`,
`tests/test_watcher_boundaries.py`, `tests/test_watcher_recovery.py`.
Added: the three test files above and this report.

`git diff --name-only HEAD -- ops/ context/` returned nothing, so
`ops/systemd/staff-agent-local-watcher.{service,timer}` and
`context/heartbeat copy.md`, `context/soul copy.md`, `context/user copy.md` are untouched.

## 9. Timer State

**The production timer was not inspected and could not be inspected from this machine, and it
was not activated.**

This work was performed on macOS (`uname -s` → `Darwin`). `systemctl` is not present on this
host. The Linux machine's timer enablement is therefore unknown from here, and is not
inferred from the unit files in `ops/systemd/` — a `WantedBy=timers.target` stanza is a
statement of how a timer *may* be installed, not evidence of what is installed on any host.
Verifying it requires `systemctl is-enabled staff-agent-local-watcher.timer` and
`systemctl is-active staff-agent-local-watcher.timer` on the Linux box.

## 10. Remaining Issues

1. **Live canary not performed**, by instruction. The real Telegram/provider path and a real
   `gh workflow run` remain unexercised. Every GitHub behaviour here is proven against a
   simulator.
2. **Timer state unknown.** See §9. Must be checked on the Linux host.
3. **`gh run list` has no page cursor.** Pagination is emulated by a growing `--limit`, so a
   search re-reads earlier pages. The search stays bounded at 20 requests, but a very busy
   workflow could in principle exhaust that bound and return `search_incomplete`. That is
   fail-safe: it withholds a resend rather than permitting one. `gh api --paginate` would
   remove the re-reading and is the natural follow-up if a real workflow ever approaches the
   bound.
4. **Grace is a fixed 900s window** while the timer fires every 30 minutes. A dispatch that
   lands just after its cycle will be reconciled on the next one, comfortably inside grace.
   The two are not derived from each other, so changing the timer interval alone would not
   automatically change the grace.
5. **`payload.correlation_id` is not cross-checked against `record.correlation_id`.** Both are
   validated for format; equality is not enforced. Two existing tests construct receipts with
   differing inner ids. Closing this needs a decision about those tests, so it was left out
   of a bounded repair.
6. **`--force-dispatch` still prints a payload with a freshly generated correlation id** that
   differs from any stored receipt. Pre-existing behaviour, untouched, but worth noting before
   the flag is used in anger.

## 11. Verdict

VERSION 2.1 THREE BLOCKERS REPAIRED — READY FOR FINAL CODEX AUDIT