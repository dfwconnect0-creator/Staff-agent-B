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
│   ├── telegram_client.py
│   ├── ingest_replies.py # reads Telegram, appends to day files
│   ├── llm/
│   │   ├── base.py           # Provider ABC + LLMResponse type
│   │   ├── factory.py        # get_provider() — reads env, returns provider
│   │   ├── parsing.py        # shared JSON block extraction
│   │   ├── retry.py          # one-shot retry on malformed JSON
│   │   └── providers/
│   │       ├── anthropic.py
│   │       ├── openai_compatible.py
│   │       ├── gemini.py
│   │       ├── ollama.py
│   │       └── openrouter.py
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

## Choosing a provider

The agent supports four LLM providers. Set `LLM_PROVIDER` to select one:

| Provider | Env var | Default model |
|---|---|---|
| `anthropic` | `ANTHROPIC_API_KEY` | `claude-opus-4-6` |
| `gemini` | `GEMINI_API_KEY` | `gemini-2.5-flash-lite` |
| `ollama` | `OLLAMA_API_KEY` | `gpt-oss:120b-cloud` |
| `openrouter` | `OPENROUTER_API_KEY` | `google/gemini-2.5-flash-lite` |

Set `LLM_MODEL` to override the default model for the chosen provider.

`LLM_PROVIDER` can be set as a GitHub Actions repository variable (Settings > Variables > Actions) so you can switch providers from the GitHub UI without editing workflow YAML. Only the API key for the chosen provider needs to be set — unused secrets can be absent.

If `LLM_PROVIDER` is not set, the run fails immediately with a clear error listing valid options.

### Troubleshooting

**What do I do if provider X returns malformed JSON?**

The retry handles it automatically: the agent sends one follow-up message asking the model to return only a fenced JSON block, then parses again. If both attempts fail, the briefing still sends to Telegram but the memory file is skipped for that day (logged as "skipping memory write").

If it happens persistently, check that the model is capable of following formatting instructions about fenced JSON blocks. Consider switching to a stronger default model by setting `LLM_MODEL`.

## Running locally

```bash
uv sync
export LLM_PROVIDER=anthropic
export ANTHROPIC_API_KEY=...
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
uv run python -m src.briefing
```

## Running tests

```bash
uv run pytest tests/ -v
```

## Updating context

Edit `context/heartbeat.md` directly to reflect what's currently in progress. The agent reads it fresh each run. No code changes needed.

## Secrets required

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GITHUB_TOKEN` (auto-provided by Actions for the memory commit step)
- One of: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OLLAMA_API_KEY`, `OPENROUTER_API_KEY` (depending on `LLM_PROVIDER`)
