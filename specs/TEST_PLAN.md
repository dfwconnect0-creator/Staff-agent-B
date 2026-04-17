# TEST PLAN: Stage 1 — Episodic Memory

Acceptance tests that define "done." Each test is written as Given/When/Then so it translates directly to pytest functions. Claude Code should implement these tests BEFORE writing the feature code (true SDD). Tests should fail initially, then pass once the feature is implemented.

Test file layout:
```
tests/
├── test_schema.py           # unit — JSON validation
├── test_episodic.py         # unit — file read/write
├── test_claude_client.py    # unit — response parsing
├── test_ingest_replies.py   # unit — reply ingestion logic
└── test_briefing_flow.py    # integration — full briefing flow with mocked LLM/Telegram
```

---

## Unit tests — schema.py

### T1.1 Valid prediction passes
- **Given** a dict matching PREDICTION_SCHEMA exactly
- **When** validate_prediction is called
- **Then** returns (True, None)

### T1.2 Missing required field fails
- **Given** a prediction dict missing `stuck_item`
- **When** validate_prediction is called
- **Then** returns (False, error_message) and error mentions `stuck_item`

### T1.3 Wrong confidence value fails
- **Given** prediction with `"confidence": "super_high"`
- **When** validate_prediction is called
- **Then** returns (False, error_message) and error mentions `confidence`

### T1.4 Extra unknown field fails
- **Given** valid prediction plus extra `"llm_opinion": "great idea"` field
- **When** validate_prediction is called
- **Then** returns (False, error_message) — strict schema, no additional properties

### T1.5 Too many flags fails
- **Given** prediction with 15 items in flags_raised
- **When** validate_prediction is called
- **Then** returns (False, error_message)

### T1.6 Empty questions_asked fails
- **Given** prediction with `"questions_asked": []`
- **When** validate_prediction is called
- **Then** returns (False, error_message)

---

## Unit tests — episodic.py

Use a pytest fixture `tmp_memory_dir` (based on `tmp_path`) that sets MEMORY_DIR to a temp location for each test.

### T2.1 path_for generates correct filename
- **Given** date 2026-04-17
- **When** path_for is called
- **Then** returns `<MEMORY_DIR>/2026-04-17.md`

### T2.2 read_day returns None for missing file
- **Given** empty memory directory
- **When** read_day is called for any date
- **Then** returns None (not raises)

### T2.3 write_day creates file with correct structure
- **Given** valid briefing_text and prediction dict
- **When** write_day is called for 2026-04-17
- **Then** file exists, contains `# 2026-04-17 (Friday)` header, contains `## Briefing sent` section with exact briefing_text, contains `## Prediction (agent-generated)` with JSON block parseable back to original prediction

### T2.4 write_day on Sunday writes (Sunday) in header
- **Given** date 2026-04-19 which is a Sunday
- **When** write_day is called
- **Then** header is `# 2026-04-19 (Sunday)`

### T2.5 write_day refuses to overwrite
- **Given** a file already exists for 2026-04-17
- **When** write_day is called for 2026-04-17 again
- **Then** raises FileExistsError, original file unchanged

### T2.6 read_day round-trips what write_day wrote
- **Given** write_day called with (text="hello world", prediction={...valid...})
- **When** read_day is called for same date
- **Then** returned dict has briefing_text == "hello world" and prediction == the original dict

### T2.7 read_day handles missing prediction JSON
- **Given** a manually-written file with `## Briefing sent` but no JSON block
- **When** read_day is called
- **Then** returns dict with briefing_text populated and prediction == None

### T2.8 read_day handles malformed JSON
- **Given** a file with a `json` fenced block containing `{broken json`
- **When** read_day is called
- **Then** returns dict with prediction == None (does not raise)

### T2.9 append_reply adds to existing file
- **Given** an existing day file without `## User replies` section
- **When** append_reply is called with timestamp "2026-04-17 09:12" and text "yo"
- **Then** file now contains `## User replies` section with bullet `- **2026-04-17 09:12 Cairo:** "yo"`

### T2.10 append_reply is idempotent
- **Given** a day file already containing reply "yo" at timestamp "2026-04-17 09:12"
- **When** append_reply is called with same timestamp and text
- **Then** file unchanged (no duplicate bullet)

### T2.11 append_reply preserves other sections
- **Given** existing file with briefing, prediction, outcome
- **When** append_reply is called
- **Then** briefing, prediction, outcome sections all still present and unchanged

### T2.12 append_reply raises on missing file
- **Given** no file exists for 2026-04-17
- **When** append_reply is called for 2026-04-17
- **Then** raises FileNotFoundError

---

## Unit tests — claude_client.py

Use `pytest-mock` to mock the Anthropic client.

### T3.1 Response with briefing text and JSON block parses correctly
- **Given** mocked Claude response "🎯 briefing here\n\n```json\n{...valid...}\n```"
- **When** ask_claude is called
- **Then** returns `{"text": "🎯 briefing here", "prediction": {...parsed dict...}, "raw": "...full..."}`

### T3.2 Response with no JSON block returns None prediction
- **Given** mocked response "🎯 briefing text only, no json"
- **When** ask_claude is called
- **Then** returns `{"text": "🎯 briefing text only, no json", "prediction": None, "raw": "..."}`

### T3.3 Response with malformed JSON returns None prediction
- **Given** mocked response "🎯 briefing\n\n```json\n{bad json\n```"
- **When** ask_claude is called
- **Then** returns dict with prediction == None, briefing text preserved

### T3.4 Multiple JSON blocks: uses the last one
- **Given** response with two ```json blocks
- **When** ask_claude is called
- **Then** the second (last) JSON is returned as prediction, text is everything before the last JSON block

---

## Unit tests — ingest_replies.py

### T4.1 Maps message timestamp to correct Cairo date
- **Given** Telegram message with timestamp equivalent to "2026-04-17 23:30 UTC" (which is 2026-04-18 02:30 Cairo)
- **When** parse_cairo_date is called
- **Then** returns date(2026, 4, 18)

### T4.2 Filters out messages from other chat_ids
- **Given** getUpdates returns 3 messages, 2 from TELEGRAM_CHAT_ID and 1 from other
- **When** get_updates is called
- **Then** returns only the 2 matching messages

### T4.3 Appends reply to correct day's file
- **Given** existing file `memory/episodic/2026-04-17.md` and a reply mapped to that day
- **When** ingest_replies main runs
- **Then** append_reply is called with correct date and message text

### T4.4 Orphan replies go to .orphans.md
- **Given** a reply mapped to 2026-04-17 but no file exists for that day
- **When** ingest_replies main runs
- **Then** `memory/episodic/.orphans.md` contains a line with the reply

### T4.5 Updates last_update_id after successful ingestion
- **Given** starts with last_update_id=100, ingests 3 messages with max update_id=250
- **When** ingest_replies main completes
- **Then** `.last_update_id` file contains "250"

### T4.6 No updates → no file changes
- **Given** getUpdates returns empty list
- **When** ingest_replies main runs
- **Then** no files are created or modified, .last_update_id unchanged

---

## Integration tests — briefing_flow.py

These test the full path with mocked external services but real file I/O.

### T5.1 First-ever run creates file, no yesterday context
- **Given** empty memory/episodic/, mocked Claude that returns valid briefing+JSON, mocked Telegram
- **When** briefing.main runs with today=2026-04-17
- **Then**:
  - Telegram send was called once with the briefing text
  - File `memory/episodic/2026-04-17.md` exists
  - The Claude prompt did NOT contain "Yesterday's context" section
  - File contains expected header, briefing, prediction JSON

### T5.2 Second day run includes yesterday verbatim
- **Given** existing file for 2026-04-17 with prediction `{"stuck_item": "X", ...}`
- **When** briefing.main runs with today=2026-04-18, captures prompt sent to Claude
- **Then** the user_message sent to Claude contains the exact yesterday JSON including `"stuck_item": "X"` — verify with substring match on the full JSON, not paraphrase

### T5.3 Yesterday's replies are included in today's prompt
- **Given** existing file for 2026-04-17 with prediction AND user_replies ["energy 3", "topic finder is fine"]
- **When** briefing.main runs with today=2026-04-18
- **Then** prompt to Claude contains both reply texts

### T5.4 Briefing sends even when JSON parsing fails
- **Given** Claude returns briefing text but malformed JSON
- **When** briefing.main runs
- **Then**:
  - Telegram send WAS called with briefing text
  - No file was written for today
  - Log contains "skipping memory write" message
  - Exit code is 0 (not a failure)

### T5.5 Briefing sends even when memory read fails
- **Given** yesterday's file exists but is corrupt (invalid structure)
- **When** briefing.main runs
- **Then** briefing is sent successfully, prompt falls back to "no prior context", no crash

### T5.6 Same-day double-run does not overwrite
- **Given** briefing already ran successfully for today, file exists
- **When** briefing.main runs again same day
- **Then**:
  - Telegram send IS called (user gets briefing either way)
  - File is NOT overwritten
  - Log contains "already exists" message
  - Exit code is 0

### T5.7 Cairo timezone boundary
- **Given** system UTC time is 2026-04-17 23:30 UTC (which is 2026-04-18 02:30 Cairo)
- **When** briefing.main runs
- **Then** today's file is written as `2026-04-18.md`, not `2026-04-17.md`

---

## Manual/smoke tests (run once after implementation)

Not pytest — these you run yourself end-to-end.

### M1. Real first run
Delete any test files. Run `uv run python src/briefing.py` with real env vars. Verify: Telegram message arrives, `memory/episodic/YYYY-MM-DD.md` appears with real content, JSON block is valid.

### M2. Real second-day run
Edit the file's date to yesterday. Run briefing again. Verify: the prompt (log it) contained yesterday's JSON verbatim, today's file is created, second message arrives.

### M3. Reply ingestion
Send a Telegram reply manually. Run `uv run python src/ingest_replies.py`. Verify: reply appears in today's file under `## User replies`. Run again — verify idempotency, no duplicate.

### M4. GitHub Actions commit-back
Push code, trigger workflow manually. Verify: after run completes, a commit appears in the repo containing the new memory file, committed by `chief-of-staff-bot`.

---

## What "done" means

- [ ] All T1–T5 tests are written and passing
- [ ] M1–M4 manual tests executed successfully
- [ ] No code paths where a Claude-generated string is written to disk without validation
- [ ] `memory/episodic/.gitkeep` committed, directory survives fresh clone
- [ ] README updated to document the new flow
- [ ] Non-goals NOT implemented (no llm/ directory, no semantic memory, no LanceDB)
