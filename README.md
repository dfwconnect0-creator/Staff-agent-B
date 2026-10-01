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
│   ├── user.md          # who Mohamed is, what he's working on (stable facts)
│   ├── heartbeat.md     # intervention policy
│   └── current_state.md # what is true right now + the next action (state lives here)
├── memory/
│   └── episodic/
│       ├── .gitkeep
│       ├── .last_update_id   # tracks last ingested Telegram update
│       ├── events.jsonl      # append-only log: briefing -> reply -> state_update
│       ├── 2026-04-17.md
│       └── ...
├── src/
│   ├── briefing.py       # main entrypoint
│   ├── telegram_client.py
│   ├── ingest_replies.py # reads Telegram, appends to day files + events.jsonl
│   ├── events.py         # append-only event log, briefing ids, provenance
│   ├── state.py          # read/write context/current_state.md, next-action rule
│   ├── state_update.py   # replies -> explicit checkpoint transitions
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
    ├── loop_support.py             # shared LLM + Telegram stubs
    ├── run_episodic_loop.py        # manual three-cycle test trigger
    ├── verify_artifacts.py         # re-derives acceptance criteria from artifacts
    └── artifacts/episodic-loop-*/  # saved evidence from a manual run
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

After each morning briefing, Mohamed can reply in Telegram. The `ingest-replies` workflow runs every 2 hours, fetches new messages, appends them to the matching day file, and records a `user_reply` event in `events.jsonl` with the briefing it answered (matched on Telegram's `reply_to_message_id`).

Replies that arrive on days without a briefing file (e.g., weekends if the briefing was skipped) go into `memory/episodic/.orphans.md`.

## The state loop

Three stores, three jobs:

| Store | Holds | Written by |
|---|---|---|
| `memory/episodic/YYYY-MM-DD.md` | human-readable day record | `briefing.py`, `ingest_replies.py` |
| `memory/episodic/events.jsonl` | append-only chain: `briefing_sent`, `briefing_delivered`, `user_reply`, `state_update` | `events.py` |
| `context/current_state.md` | current target, checkpoints, next action, transition log | `state_update.py` |

The day files hold one briefing per Cairo date, so they cannot represent several briefings in a day or mark an event as consumed. `events.jsonl` is therefore the authoritative store for the loop; the day files stay the readable mirror.

Every run of `python -m src.briefing` starts by applying any unconsumed `user_reply` events to `current_state.md`:

1. Split the reply into sentences. For each checkpoint, the **last** sentence mentioning one of its aliases decides.
2. Within that sentence a negative signal beats a positive one — "it worked, but the next briefing ignored it" must not read as verified.
3. Only transitions backed by a quoted signal are applied, and each one is appended to the state file's `## Transitions` log with the evidence event id and the exact matched phrase. Nothing is inferred from silence.
4. The next action is derived mechanically: the first checkpoint that is not `verified`, or `No evidence-backed intervention needed.` when all are verified.

The mandated next action is passed to the model, which must use it and may not re-test anything already marked verified. If the state has not changed since the last briefing delivered today, `briefing.py` skips the send entirely instead of sending a duplicate.

`current_state.md` is safe to edit by hand — the state-update step only applies transitions that new evidence supports. `target_output` is meant to be set by a human.

## Testing the loop end to end

`tests/run_episodic_loop.py` drives the real `src.briefing.main()`, `src.ingest_replies.ingest_update()` and `src.state_update.update_state_from_events()` for three consecutive cycles, stubbing only the two network seams (`get_provider`, `send_telegram_message`) with the same stubs the test suite uses. Saved artifacts from a run land in `tests/artifacts/episodic-loop-<date>/`.

```bash
uv run pytest tests/ -v                        # 143 tests
uv run python -m tests.run_episodic_loop       # manual three-cycle run, writes artifacts
uv run python -m tests.verify_artifacts        # re-check acceptance criteria from the artifacts
```

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

- `context/user.md` — stable facts about Mohamed and his work. The agent reads it fresh each run.
- `context/heartbeat.md` — intervention policy.
- `context/current_state.md` — what is true right now. Repoint `target_output` at the next real
  project and reset the checkpoint values; the agent fills in `next_action`, `last_evidence`,
  `confidence` and `## Transitions` from evidence.

## Secrets required

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `GITHUB_TOKEN` (auto-provided by Actions for the memory commit step)
- One of: `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OLLAMA_API_KEY`, `OPENROUTER_API_KEY` (depending on `LLM_PROVIDER`)
