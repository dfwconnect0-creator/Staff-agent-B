# SPEC: Stage 1 — Episodic Memory

**Status:** Ready to implement
**Target repo:** `chief-of-staff` (existing v1, stateless briefing agent)
**Scope:** Add episodic memory — daily markdown files that give the agent day-over-day continuity. Also wire a feedback loop via Telegram replies.
**Out of scope:** LLM switching, semantic memory, vector search, graph memory. These come in later stages — do NOT add them.

---

## 1. Problem statement

v1 is stateless. Every morning, Claude sees `soul.md`, `user.md`, `heartbeat.md` and predicts what's stuck with no knowledge of yesterday. This stage gives the agent memory of its own predictions and a way to learn whether those predictions were right, by reading the user's reply to yesterday's Telegram message.

The source of truth is plain markdown files in `memory/episodic/`. The LLM never writes to these files directly — it proposes structured JSON, Python validates and writes. This pattern is load-bearing for every future stage.

---

## 2. Architectural rules (non-negotiable)

1. **LLM never writes to disk.** Claude returns text + structured JSON. Python code validates the JSON against a schema and writes the markdown file. If validation fails, the briefing still sends but the memory write is skipped and logged.
2. **Markdown is the source of truth.** Not JSON, not SQLite. Plain files in `memory/episodic/`. Filename is `YYYY-MM-DD.md` in Cairo time.
3. **Append-only.** Today's file is written once per run. Never modify past days' files programmatically. If a past file needs correction, user edits by hand.
4. **Yesterday's context passes through verbatim.** When today's run reads yesterday's file, it embeds yesterday's prediction JSON in today's prompt unchanged. No summarization, no rewording. Summarization is where drift hides.
5. **Telegram reply ingestion is a separate concern from the briefing run.** The briefing cron runs once per morning. Reply ingestion runs independently (also on a cron) and writes into the relevant day's file. Never combine these into one workflow.
6. **If memory read fails, briefing still runs.** A missing or malformed yesterday file degrades gracefully to "no prior context available." Never let memory errors block the briefing.

---

## 3. File layout after implementation

```
chief-of-staff/
├── .github/workflows/
│   ├── daily-briefing.yml          [MODIFIED — now passes memory path]
│   └── ingest-replies.yml          [NEW — reads Telegram replies, updates files]
├── context/                         [UNCHANGED]
│   ├── soul.md
│   ├── user.md
│   └── heartbeat.md
├── memory/                          [NEW]
│   └── episodic/
│       └── .gitkeep                 [NEW — keep dir in git even when empty]
├── src/
│   ├── briefing.py                  [MODIFIED — reads memory, writes memory]
│   ├── claude_client.py             [MODIFIED — ask_claude returns (text, json)]
│   ├── telegram_client.py           [MODIFIED — add get_updates() for replies]
│   ├── memory/                      [NEW package]
│   │   ├── __init__.py
│   │   ├── episodic.py              [NEW — read/write daily files]
│   │   └── schema.py                [NEW — JSON validation]
│   └── ingest_replies.py            [NEW — reads Telegram replies]
├── tests/                           [NEW]
│   ├── __init__.py
│   ├── test_episodic.py
│   ├── test_schema.py
│   └── fixtures/
│       └── sample_day.md
├── pyproject.toml                   [MODIFIED — add jsonschema, pytest]
└── README.md                        [MODIFIED — document new flow]
```

---

## 4. Data contract — the daily file format

Every file `memory/episodic/YYYY-MM-DD.md` has this exact structure. No deviations. This is what the agent reads and writes.

```markdown
# 2026-04-17 (Friday)

## Briefing sent

[Raw briefing text that was sent to Telegram, verbatim.]

## Prediction (agent-generated)

```json
{
  "schema_version": 1,
  "stuck_item": "Topic finder GitHub Actions step — workflow not completing",
  "smallest_action": "Open last workflow run in GitHub, read the error",
  "confidence": "medium",
  "flags_raised": ["last_10_percent", "external_dependency"],
  "questions_asked": [
    "Did you check the workflow run today?",
    "Is the junior developer still on NotebookLM infographics?",
    "Energy level 1-5?"
  ]
}
```

## User replies (ingested from Telegram)

- **2026-04-17 09:12 Cairo:** "yeah topic finder is actually fine, the real stuck one is course conversion — 3 leads ghosted"
- **2026-04-17 09:15 Cairo:** "energy 3"

## Outcome (optional, user-editable)

_Prediction was partially wrong. Correct stuck item: course conversion follow-up, not topic finder._
```

**Required sections:** `# header`, `## Briefing sent`, `## Prediction (agent-generated)`.
**Optional sections:** `## User replies`, `## Outcome`. Omitted if no data.

The JSON block uses fenced code with `json` language tag. This is what the parser looks for.

---

## 5. JSON schema for predictions

File: `src/memory/schema.py`. Use `jsonschema` library.

```python
PREDICTION_SCHEMA = {
    "type": "object",
    "required": ["schema_version", "stuck_item", "smallest_action", "confidence", "flags_raised", "questions_asked"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "integer", "const": 1},
        "stuck_item": {"type": "string", "minLength": 5, "maxLength": 300},
        "smallest_action": {"type": "string", "minLength": 5, "maxLength": 300},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        "flags_raised": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": 10,
        },
        "questions_asked": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 5,
        },
    },
}
```

Validation rule: if JSON fails validation, log the failure, send the briefing anyway, skip the memory write. Do NOT crash.

---

## 6. Module specifications

### 6.1 `src/memory/episodic.py`

```python
from pathlib import Path
from datetime import date

MEMORY_DIR: Path  # Resolved from repo root / memory / episodic

def path_for(day: date) -> Path:
    """Return memory/episodic/YYYY-MM-DD.md for given date."""

def read_day(day: date) -> dict | None:
    """
    Read a day's file and return:
    {
        "date": "2026-04-17",
        "briefing_text": str,
        "prediction": dict | None,     # parsed JSON block, None if missing/invalid
        "user_replies": list[dict],    # [{"timestamp": str, "text": str}, ...]
        "outcome": str | None,
    }
    Returns None if file does not exist.
    Never raises on malformed content — returns best-effort dict with None fields.
    """

def write_day(day: date, briefing_text: str, prediction: dict) -> Path:
    """
    Create a new day file. Raises FileExistsError if file already exists
    (same-day double-run is a bug we want to know about, not silently overwrite).
    Returns path to written file.
    """

def append_reply(day: date, timestamp_cairo: str, text: str) -> None:
    """
    Append a user reply to an existing day file under '## User replies'.
    If '## User replies' section doesn't exist, create it.
    Idempotent: if (timestamp, text) already present, do nothing.
    Raises FileNotFoundError if the day file doesn't exist yet.
    """
```

### 6.2 `src/memory/schema.py`

Exports `PREDICTION_SCHEMA` and `validate_prediction(data: dict) -> tuple[bool, str | None]`. Returns `(True, None)` on valid, `(False, error_message)` on invalid.

### 6.3 `src/claude_client.py` (modified)

Change `ask_claude` signature to return a structured result:

```python
def ask_claude(system_prompt: str, user_message: str) -> dict:
    """
    Returns:
    {
        "text": str,           # the briefing for Telegram
        "prediction": dict | None,  # parsed JSON, None if parsing failed
        "raw": str,            # full model output, for debugging
    }
    """
```

How to split: the prompt (see section 7) instructs Claude to produce the briefing text, then a fenced `json` block at the end. The client extracts the last `json` fenced block as the prediction. Everything before it is the briefing text.

If no JSON block found, `prediction = None` and `text = raw`.

### 6.4 `src/briefing.py` (modified)

New flow:

```python
def main():
    context = load_context()
    today = datetime.now(CAIRO_TZ).date()
    yesterday = today - timedelta(days=1)

    yesterday_memory = episodic.read_day(yesterday)  # may be None

    system_prompt, user_message = build_prompt(context, yesterday_memory, today)

    result = ask_claude(system_prompt, user_message)
    briefing_text = result["text"]
    prediction = result["prediction"]

    if not briefing_text.strip():
        send_telegram_message("⚠️ Empty briefing. Check logs.")
        sys.exit(1)

    send_telegram_message(briefing_text)

    # Memory write — only if prediction is valid
    if prediction is None:
        log("No valid prediction JSON in response, skipping memory write")
        return 0

    valid, err = validate_prediction(prediction)
    if not valid:
        log(f"Prediction failed schema: {err}, skipping memory write")
        return 0

    try:
        episodic.write_day(today, briefing_text, prediction)
    except FileExistsError:
        log(f"File for {today} already exists, not overwriting")
    return 0
```

### 6.5 `src/telegram_client.py` (modified — add one function)

```python
def get_updates(since_update_id: int = 0) -> list[dict]:
    """
    Call Telegram getUpdates API. Return list of message dicts:
    [{"update_id": int, "timestamp_cairo": str, "text": str, "chat_id": int}, ...]
    Filters to only messages from TELEGRAM_CHAT_ID.
    """
```

### 6.6 `src/ingest_replies.py` (new, standalone entry point)

Runs on its own cron (every 2 hours, say). Reads Telegram replies since last ingestion, maps each to a day (based on Cairo date of message timestamp), appends to that day's file.

Last-ingested update_id is tracked in `memory/episodic/.last_update_id`. This is the only state file outside per-day files. It's gitignored (written by Actions, not committed).

```python
def main():
    last_id = read_last_update_id()  # from memory/episodic/.last_update_id, default 0
    updates = telegram.get_updates(since_update_id=last_id)
    for update in updates:
        day = parse_cairo_date(update["timestamp_cairo"])
        try:
            episodic.append_reply(day, update["timestamp_cairo"], update["text"])
        except FileNotFoundError:
            # Reply to a day we never briefed on — save to orphans
            append_to_orphans(update)
    if updates:
        write_last_update_id(max(u["update_id"] for u in updates))
```

Orphan replies go to `memory/episodic/.orphans.md` — a catch-all for messages that don't map to a briefed day. Keeps the signal, doesn't lose it.

---

## 7. Prompt changes

The system prompt stays `soul.md`. The user message changes:

```
Today is {day_name}, {date_str} (Cairo time).

{IF yesterday_memory exists and has prediction:}
## Yesterday's context

Yesterday ({yesterday_date}) you predicted:

```json
{yesterday_prediction_json_verbatim}
```

{IF user_replies present:}
The user replied:
{formatted_user_replies}
{END IF}

Use this to calibrate today's briefing. If yesterday's prediction was wrong based on the replies, adjust. If it was right but still unresolved, flag it again more firmly.

{END IF}

--- USER PROFILE (user.md) ---
{user.md contents}

--- DAILY CHECKLIST (heartbeat.md) ---
{heartbeat.md contents}

--- YOUR TASK ---

[same briefing structure as v1: 🎯 stuck item, ✂️ smallest action, ❓ three questions, ⚡ energy check]

AFTER the briefing text, output a JSON block with this exact structure:

```json
{
  "schema_version": 1,
  "stuck_item": "one-line name of the stuck item",
  "smallest_action": "the single smallest action to unblock it",
  "confidence": "low|medium|high",
  "flags_raised": ["flag1", "flag2"],
  "questions_asked": ["question 1", "question 2", "question 3"]
}
```

The JSON must come last. The briefing text (above the JSON) is what I'll read on my phone.
```

---

## 8. Dependencies to add

In `pyproject.toml`:

```toml
dependencies = [
    "anthropic>=0.40.0",
    "httpx>=0.27.0",
    "jsonschema>=4.0.0",
]

[dependency-groups]
dev = [
    "pytest>=8.0.0",
    "pytest-mock>=3.12.0",
    "freezegun>=1.4.0",    # for testing date-dependent code
]
```

---

## 9. GitHub Actions changes

### 9.1 `daily-briefing.yml` — add write-back commit

After the briefing runs, if `memory/episodic/` has new files, commit and push them back to the repo. This is the mechanism that makes memory persistent across runs.

```yaml
- name: Commit new memory
  run: |
    git config user.name "chief-of-staff-bot"
    git config user.email "bot@users.noreply.github.com"
    git add memory/episodic/
    git diff --staged --quiet || git commit -m "memory: briefing for $(date -u +%Y-%m-%d)"
    git push
  env:
    GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

Workflow needs `permissions: contents: write` at the top.

### 9.2 `ingest-replies.yml` — new workflow

```yaml
name: Ingest Telegram Replies

on:
  schedule:
    - cron: "0 */2 * * *"   # every 2 hours
  workflow_dispatch:

permissions:
  contents: write

jobs:
  ingest:
    runs-on: ubuntu-latest
    timeout-minutes: 3
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync --frozen
      - run: uv run python src/ingest_replies.py
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
      - name: Commit ingested replies
        run: |
          git config user.name "chief-of-staff-bot"
          git config user.email "bot@users.noreply.github.com"
          git add memory/episodic/
          git diff --staged --quiet || git commit -m "memory: ingest replies $(date -u +%Y-%m-%dT%H:%M)"
          git push
```

---

## 10. Non-goals (do NOT implement in this stage)

- No `llm/` directory. Do not refactor `claude_client.py` into an abstract interface. That's stage 2.
- No `memory/semantic/` directory. No projects.md extraction. That's stage 3.
- No LanceDB, no vector search, no embeddings. That's stage 4.
- No Neo4j. Maybe never.
- No pattern/accuracy tracking JSON. That's stage 3.
- No summarization of yesterday's prediction. Pass through verbatim.
- No backfill of historical data. Starts empty from first run.

---

## 11. Success criteria

All acceptance tests in `TEST_PLAN.md` pass. On first real run, a `memory/episodic/YYYY-MM-DD.md` file appears in git. On second run 24h later, the briefing prompt includes yesterday's JSON verbatim. When user replies to the Telegram message, within 2 hours the reply shows up in that day's markdown file.
