# BASELINE AUDIT — Staff Agent B

**Date:** 2026-10-01
**Commit:** `6f0c2fc3433041c9c47d41998ddaef12092c2509` (branch `main`, clean tree)
**Auditor phase:** 2 (read-only, no fixes applied)

---

## 2A. Existing system run first

| Command | Result |
|---|---|
| `uv sync --frozen` | OK — anthropic 0.x, httpx 0.28.1, jsonschema 4.26.0, pytest 9.0.3 |
| `uv run pytest tests/ -v` | **72 passed in 1.00s** (full output: `baseline_pytest.txt`) |
| `uv run python /tmp/.../smoke_baseline.py` | exit 0 (full output: `baseline_smoke.txt`) |

Live Telegram / live LLM were **not** testable: `LLM_PROVIDER`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`, `OLLAMA_API_KEY`, `OPENROUTER_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID` are all unset on this machine. No secret value was printed.

### Smoke test — what it proved

The smoke test drove the **real** `src.briefing.main()` and `src.ingest_replies.main()`,
faking only the two network boundaries (provider + `sendMessage`) and redirecting
`episodic.MEMORY_DIR` to a temp dir.

```
STEP 1  briefing for 2026-10-01          -> rc=0, memory/episodic/2026-10-01.md written
STEP 2  ingest a Telegram reply          -> cursor -> 9001, "## User replies" section written
STEP 3  briefing for 2026-10-02          -> rc=0
        reply text present in prompt: True
        "Yesterday" section present : True
STEP 4  replay the SAME update twice     -> day file unchanged on replay: True
STEP 5  current_state.md exists          : False
        events.jsonl exists              : False
        context files loaded             : ['heartbeat', 'soul', 'user']
```

**Conclusion:** the transport half of the loop works. Replies are stored and *do* reach the
next prompt. What does **not** exist is any state transition: the reply is handed to the
model as prose and the model is asked to "adjust". Nothing writes back.

---

## 2C. Data-flow trace — one briefing, end to end

**Scheduled run (`src/briefing.py`, cron `0 5 * * *` = 08:00 Cairo):**

```
cron trigger
  -> load_context()                    reads context/{soul,user,heartbeat}.md   [src/briefing.py:19]
  -> today = now(UTC+3).date()
  -> episodic.read_day(yesterday)      reads memory/episodic/<yesterday>.md    [src/briefing.py:104]
       └─ on ANY exception -> log.warning + yesterday_memory=None           [src/briefing.py:105]
  -> build_prompt()                    inlines user.md, heartbeat.md,
                                       yesterday's prediction JSON,
                                       yesterday's reply bullets              [src/briefing.py:31]
  -> get_provider().complete()         real network call (anthropic|gemini|ollama|openrouter)
  -> result["text"] / result["prediction"]
       └─ extract_last_json_block()    last ```json fence                     [src/llm/parsing.py:6]
       └─ with_json_retry()            ONE retry, bad-JSON only                [src/llm/retry.py:13]
  -> send_telegram_message(text)       POST sendMessage                        [src/briefing.py:120]
  -> validate_prediction(prediction)   jsonschema, schema_version==1          [src/briefing.py:126]
  -> episodic.write_day(today, ...)    FileExistsError -> logged, NOT retried  [src/briefing.py:131]
  -> GH Actions: git add memory/episodic/ && commit && push
```

**After the user replies (`src/ingest_replies.py`, cron `0 */2 * * *`):**

```
cron trigger
  -> read_last_update_id()             memory/episodic/.last_update_id         [src/ingest_replies.py:25]
  -> telegram.get_updates(since_id)    filters chat_id, requires "text"        [src/telegram_client.py:17]
  -> parse_cairo_date(timestamp)       -> day
  -> episodic.append_reply(day, ts, text)
       ├─ file missing -> .orphans.md                                       [src/ingest_replies.py:46]
       └─ dedup by exact bullet string                                     [src/memory/episodic.py:102]
  -> write_last_update_id(max update_id)
  -> GH Actions: commit + push
```

### Where the chain stops — stated explicitly

**The chain stops after Telegram send + markdown append. There is no state mutation and no
explicit link between a reply and the briefing it answered.**

Concrete proof from the repository itself, not from reading code:

- `memory/episodic/` holds **145 day files**. **1** of them (2026-04-22) has a
  `## User replies` section. `.orphans.md` does not exist. `.last_update_id` = `695435085`.
- Parsing all 145 prediction blocks yields **13 distinct `stuck_item` values**, and the top
  six are cosmetic variants of one item:

  | days | stuck_item |
  |---|---|
  | 48 | `Marketing-agent pipeline real-world test run` |
  | 41 | `marketing-agent pipeline real-world test run` |
  | 12 | `marketing-agent: needs real-world test run` |
  | 11 | `marketing-agent real-world test run` |
  | 7 | `Marketing agent pipeline - needs real-world t…` |
  | 6 | `Marketing-agent real-world test run initiatio…` |

- The single ingested reply (2026-04-22) reads:
  `"Same prediction as today — marketing agent test run. Agent repeating without memory. Energy: [4]."`

So the loop has been running for ~5.5 months and the *decision* has never changed. The user's
complaint is a measured property of the system, not a perception.

---

## 2B. Static review — findings

### 1. Silent exception handling
| Where | What | Verdict |
|---|---|---|
| `src/memory/episodic.py:63` | `except Exception:` in `read_day` returns a dict with empty briefing / `None` prediction — indistinguishable from "a briefing that had no prediction" | **Real defect.** A corrupt file silently degrades context. Not logged at all. |
| `src/briefing.py:105` | `except Exception as e: log.warning(...)` then continue | Acceptable — logged. |
| `src/memory/parsing.py:21`, `episodic.py:26` | narrow `json.JSONDecodeError` | Fine. |
| `src/ingest_replies.py:31` | `except ValueError: return 0` on cursor parse | Acceptable — a corrupt cursor replays from 0, and `append_reply` is idempotent. |

No `contextlib.suppress`. No bare `except:`.

### 2. State corruption risks
- **No atomic writes anywhere.** Every `write_text` truncates in place
  (`episodic.py:91`, `episodic.py:105`, `ingest_replies.py:37`). A crash mid-write loses the
  day file. Severity LOW for the MVP (files are small, git holds history).
- **Send-before-record ordering.** `send_telegram_message` runs at `briefing.py:120`,
  `write_day` at `briefing.py:132`. If the write fails, the briefing was delivered but is
  not on record, so the next run has no `## Briefing sent` for that day.
- **No duplicate-event processing.** `append_reply` dedups on the exact bullet string and
  `.last_update_id` is the real guard. Verified in smoke STEP 4.

### 3. Telegram reply-linking risks — **the core gap**
- `telegram_client.get_updates()` builds a **new dict** with only
  `update_id, timestamp_cairo, text, chat_id` (`telegram_client.py:42`).
  **`message_id` and `reply_to_message` are read from the API response and thrown away.**
- Consequence: a reply cannot be tied to the briefing it answers. The only link is the
  Cairo **date**, which breaks when: a reply arrives after midnight for the previous day's
  briefing; a run is missed so day N-1 has no file; two briefings exist on one day.
- **Unrelated messages are accepted as briefing replies.** Only `chat_id` and presence of
  `text` are checked. `/start`, "ok", or a message about a different project all become
  `## User replies` bullets and are then fed to the model as calibration input.
- **No message_id persisted**, so there is no natural idempotency key for a reply. Dedup
  relies on the bullet string matching exactly — two genuinely distinct replies with
  identical text on the same day collapse to one.

### 4. Idempotency
| Scenario | Behaviour | Verdict |
|---|---|---|
| Same Telegram update delivered twice | `append_reply` dedups; cursor already advanced | **Safe** (smoke STEP 4) |
| `briefing.main()` twice on the same Cairo day | **Sends a SECOND Telegram briefing**, `write_day` raises `FileExistsError` and is swallowed with a log line | **Defect.** Duplicate delivery, no record of the second one. Existing test `test_same_day_double_run_does_not_overwrite` encodes this behaviour (2 sends, 1 file). |
| Two runs racing | No lock. Both read the same state, both send. | Accepted risk — cron is 2h/24h apart; the two crons can overlap only at 00:00/05:00 boundaries. |

### 5. Race conditions
`briefing` (05:00 UTC daily) and `ingest_replies` (every 2h) can overlap. Both `git push`
to the same branch with no retry. A push rejection fails the workflow; the next run
`git pull`s via `actions/checkout` and recovers, but the failed run's work is lost.
Severity MEDIUM, not blocking the MVP. Not adding locking.

### 6. Scheduling problems
- Timezone: consistent UTC+3 (`CAIRO_TZ` in both `briefing.py` and `telegram_client.py`).
  Verified by `test_cairo_timezone_boundary`. **No timezone defect.**
- The commit message in `daily-briefing.yml:34` uses `date -u`, so a briefing written at
  05:00 UTC logs the right UTC date — but `write_day` uses Cairo date. During the
  22:00–24:00 UTC window these disagree. At 05:00 UTC they do not. LOW.
- **Manual vs scheduled**: both workflows have `workflow_dispatch`. There is **no local
  entry point** and no way to drive the pipeline today without waiting for cron. This is a
  gap for the 3-cycle requirement.
- `timeout-minutes: 5` / `3` bound the runs in CI. Locally there is nothing.

### 7. Configuration problems
- **No timeouts on Telegram.** `httpx.post(url, json=...)` (`telegram_client.py:13`) and
  `httpx.get(url, params=...)` (`telegram_client.py:27`) have **no `timeout=`**. A hung
  connection blocks the thread indefinitely outside CI. One `httpx.post` does set
  `timeout=60.0` (`openai_compatible.py:31`).
- `os.environ["TELEGRAM_BOT_TOKEN"]` — `KeyError` with no friendly message if unset. Fails
  loudly, which is acceptable, but the traceback does not name the workflow step.
- No secrets in the repo. `.gitignore` covers `.env`. `.DS_Store` is tracked at repo root and
  in `specs/` — noise only.
- Paths are `Path(__file__).parent`-relative — **no machine-specific paths, no CWD
  dependency.** Good.
- `OpenRouterProvider.extra_headers` contains a literal `https://github.com/<user>/chief-of-staff`
  placeholder. Cosmetic.

### 8. Persistence
Day files and `.last_update_id` are committed to git and survive restart and separate CI
invocations. **Persistence itself is sound.** The defect is that nothing persisted is
*consulted as state*.

### 9. Prompt/state mismatch — **the root cause**
The prompt at `briefing.py:56-60` says:

> "Use this to calibrate today's briefing. If yesterday's prediction was wrong based on the
> replies, adjust. If it was right but still unresolved, flag it again more firmly."

That instruction is **actively harmful** in the second clause: it tells the model that an
unresolved item *should be re-flagged*. Given a reply like "I tested it, it works", the
model has no persisted fact saying "storage = verified", so the most defensible thing it can
do — and the one it demonstrably did for 145 days — is re-assert the same blocker. The agent
is relying purely on conversation context. There is no operational state file to check
against, so a claim can be made that is contradicted by, or absent from, everything persisted.

### 10. "Done" assumptions
`briefing.py` returns `0` on the success path, including when the briefing sent was a
`⚠️ Empty briefing` warning path (`briefing.py:116-118` returns `1` there, fine) and when the
prediction was dropped (`:123`, `:128` → `return 0`). So "process succeeded" and "memory
recorded" are conflated in the exit code, though the log distinguishes them.

### 11. Missing validation
`validate_prediction` checks shape only. Nothing validates that the briefing's claim is
supported by any persisted evidence. `confidence` is free text from the model, unverified.

### 12. Test quality
72 tests, all passing. Strong coverage of: JSON extraction, retry, factory, provider shapes,
day-file parse/write, reply append idempotency, chat-id filtering, Cairo boundary.
**What is missing:**
- **No test asserts that a reply changes the next briefing.** The closest is
  `test_yesterday_replies_included_in_prompt`, which asserts the reply string is *present in
  the prompt text*. That tests plumbing, not behaviour — it would pass identically if the
  model ignored the reply, which is exactly what happened for 145 days.
- `test_briefing_sends_even_when_memory_read_fails` ends with
  `assert "Yesterday's context" not in captured_prompts[0] or True` — the `or True`
  **disables the assertion**. It is a no-op test.
- `test_i7_2_provider_model_logged` asserts `"claude-opus-6" in log_text or "gemini" in
  log_text` against a mock that returns `provider="anthropic"` — satisfied incidentally, not
  by what it claims to check.
- Every `send_telegram_message` is mocked. **Nothing has ever exercised real Telegram delivery
  from this test suite.**
- No test for duplicate briefing delivery on a same-day re-run.
- No test for a multi-line or quote-containing reply breaking the bullet regex.

---

## 2D. Baseline risk report

| # | Issue | Evidence | Severity | Current impact | Must fix for 3-cycle? | Proposed minimal fix |
|---|---|---|---|---|---|---|
| 1 | No explicit state transition; reply reaches prompt as prose only | `briefing.py:56-60`; smoke STEP 3; 145 day-files / 13 distinct stuck_items | **BLOCKER** | Decision never changes. Fails criteria 4, 5, 6, 8 | **YES** | Add `current_state.md` + a state-update step in `briefing.main()` |
| 2 | Prompt instructs the model to *re-flag* unresolved items | `briefing.py:59` | **BLOCKER** | Actively drives the 145-day repeat | **YES** | Replace with state-mandated next action; verified checkpoints are off the table |
| 3 | `message_id` / `reply_to_message_id` discarded | `telegram_client.py:42` | **HIGH** | No reply→briefing link. Fails criteria 2, 3 | **YES** | Preserve both in `get_updates()` output; store on the event |
| 4 | No briefing identifier | `episodic.write_day` keys on date only | **HIGH** | Cannot chain briefing→reply→next briefing. Fails criteria 1, 3 | **YES** | `briefing_YYYYMMDD_NNN`, derived from the event log |
| 5 | No event log | no `events.jsonl` anywhere | **HIGH** | `processed` flag has nowhere to live. Fails criteria 2, 4, 5 | **YES** | Append-only `memory/episodic/events.jsonl` |
| 6 | Same-day re-run sends a duplicate briefing, unrecorded | `briefing.py:120` before `:132`; `test_same_day_double_run_does_not_overwrite` | **HIGH** | Manual test trigger would spam; violates idempotency | **YES** | Record briefing event before send; refuse duplicate send for the same id |
| 7 | Any chat text accepted as a briefing reply | `telegram_client.py:33-38` | MEDIUM | Noise becomes state input | No (out of scope for MVP) | Leave as-is; note it |
| 8 | No timeout on Telegram HTTP calls | `telegram_client.py:13,27` | MEDIUM | Can hang outside CI | Recommended — 1-line each | Add `timeout=30.0` |
| 9 | `read_day` swallows all exceptions silently | `episodic.py:63` | MEDIUM | Corruption indistinguishable from empty | No | Leave; note it |
| 10 | Non-atomic writes | `episodic.py:91,105` | LOW | Crash mid-write loses a day file | No | Leave; note it |
| 11 | `## Outcome` parsed but never written | `episodic.py:42` vs `write_day:79` | LOW | Dead code | No | Leave; note it |
| 12 | No local/manual pipeline trigger | workflows are cron/`workflow_dispatch` only | MEDIUM | Cannot run 3 cycles today | **YES** | Test harness reusing `briefing.main()` unchanged |
| 13 | Two crons can `git push` concurrently, no retry | `daily-briefing.yml:37`, `ingest-replies.yml:29` | MEDIUM | Occasional lost run | No | Leave; note it |
| 14 | `assert ... or True` no-op test | `test_briefing_flow.py:181` | LOW | False confidence | No | Leave; report it |
| 15 | Multi-line or quoted reply breaks bullet parse | `episodic.py:36` regex | LOW | Reply silently dropped on read-back | No | Leave; report it |

---

## 2F. Decision gate

**Classification: B — NEEDS MINIMAL REPAIR FIRST**, then extend.

Rationale:
- The provider layer, day-file store, Telegram client and scheduler are sound and well
  tested (72 green). **Nothing here needs repair.** The three BLOCKER/HIGH items are all
  *missing* capabilities, not broken working code.
- The single genuine pre-existing **defect** that would invalidate the experiment is #6:
  a second `briefing.main()` on the same Cairo day sends a duplicate Telegram message and
  records nothing. Any test that runs more than one briefing per day hits it immediately.
- So: fix #6 as part of the feature (it falls out of adding briefing IDs naturally — no
  separate repair commit needed), then add items #1–#5 and #12.
- Not **C**: the baseline is green and extensible. Not **A**: without #6 the manual trigger
  is not trustworthy, so "SAFE TO EXTEND" would be overclaiming.

**Scope boundary accepted today:** items 7, 9, 10, 11, 13, 14, 15 are recorded and left
alone.
