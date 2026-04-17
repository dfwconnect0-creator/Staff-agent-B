# chief-of-staff

A morning briefing agent that sends a daily Telegram message with what's stuck, the smallest unblocking action, and three sharp questions.

## What it does

Every morning at 8 AM Cairo (5 AM UTC), a GitHub Actions workflow:

1. Reads `context/soul.md`, `context/user.md`, and `context/heartbeat.md`
2. Checks yesterday's memory file (if it exists) to include prior context and any user replies
3. Calls Claude to generate a briefing + a structured JSON prediction
4. Sends the briefing text to Telegram
5. Writes a day file to `memory/episodic/YYYY-MM-DD.md`
6. Commits the memory file back to the repo

Every 2 hours, a second workflow ingests Telegram replies and appends them to the relevant day file.

## File layout

```
.
├── context/
│   ├── soul.md          # agent personality and rules
│   ├── user.md          # who Mohamed is, what he's working on
│   └── heartbeat.md     # current projects and blockers (update manually)
├── memory/
│   └── episodic/
│       ├── .gitkeep
│       ├── .last_update_id   # tracks last ingested Telegram update
│       ├── 2026-04-17.md
│       └── ...
├── src/
│   ├── briefing.py       # main entrypoint
│   ├── claude_client.py  # calls Claude, parses JSON block from response
│   ├── telegram_client.py
│   ├── ingest_replies.py # reads Telegram, appends to day files
│   └── memory/
│       ├── episodic.py   # read/write day files
│       └── schema.py     # JSON prediction schema + validation
└── tests/
```

## How memory works

Each day file has three sections:

```markdown
# 2026-04-17 (Friday)

## Briefing sent

[the briefing text sent to Telegram]

## Prediction (agent-generated)

```json
{
  "schema_version": 1,
  "stuck_item": "...",
  "smallest_action": "...",
  "confidence": "low|medium|high",
  "flags_raised": [...],
  "questions_asked": [...]
}
```

## User replies (ingested from Telegram)

- **2026-04-17 09:12 Cairo:** "energy 3"
- **2026-04-17 09:15 Cairo:** "topic finder is fine"
```

The next morning, the agent reads yesterday's file and includes the prediction + replies in the prompt. If the prediction was wrong (based on replies), the agent adjusts. If it was right but still unresolved, it flags it more firmly.

## Reading a day file

Each file is plain Markdown - readable directly in GitHub. The JSON prediction block is machine-parseable but also human-readable. Replies are timestamped in Cairo time.

## Feedback loop via Telegram replies

After each morning briefing, Mohamed can reply in Telegram. The `ingest-replies` workflow runs every 2 hours, fetches new messages, and appends them to the matching day file. The next morning's briefing includes those replies as context.

Replies that arrive on days without a briefing file (e.g., weekends if the briefing was skipped) go into `memory/episodic/.orphans.md`.

## Running locally

```bash
uv sync
export ANTHROPIC_API_KEY=...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
uv run python src/briefing.py
```

## Running tests

```bash
uv run pytest tests/ -v
```

## Updating context

Edit `context/heartbeat.md` directly to reflect what's currently in progress. The agent reads it fresh each run. No code changes needed.

## Secrets required

- `ANTHROPIC_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GITHUB_TOKEN` (auto-provided by Actions for the memory commit step)
