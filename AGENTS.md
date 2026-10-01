# AGENTS.md

## Commands

- Sync deps: `uv sync` (CI uses `uv sync --frozen` — don't hand-edit `uv.lock`)
- Run daily briefing: `uv run python -m src.briefing`
- Run reply ingestion: `uv run python -m src.ingest_replies`
- Tests: `uv run pytest tests/ -v`; single test: `uv run pytest tests/test_episodic.py::test_name -v`
- Manual three-cycle loop test: `uv run python -m tests.run_episodic_loop` (add `--tmp-memory` to keep the repo's `memory/episodic/` clean)
- Re-check a saved run against the acceptance criteria: `uv run python -m tests.verify_artifacts`

## Verification

- No linter or typechecker is configured. pytest is the only gate — don't invent lint commands.
- Tests need no secrets: root `conftest.py` sets fake `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` automatically.
- Test stack: `pytest-mock` + `freezegun`. Memory tests monkeypatch `episodic.MEMORY_DIR` to `tmp_path` — keep that pattern when adding tests.
- An autouse `tmp_state_path` fixture redirects `state.DEFAULT_STATE_PATH` to `tmp_path` too, so tests never rewrite the repo's `context/current_state.md`. Adding a test that needs a state file should not patch it again.
- `tests/loop_support.py` holds the only LLM/Telegram stubs. `tests/run_episodic_loop.py` imports them rather than defining its own, so the manual run and the gate can't drift.

## Architecture

- Two cron entrypoints: `src/briefing.py` (daily briefing) and `src/ingest_replies.py` (Telegram reply ingestion). Both are run as `python -m src.<module>`.
- LLM access goes through `src/llm/`: `Provider` ABC (`base.py`), `get_provider()` factory (`factory.py`) driven by the `LLM_PROVIDER` env var. Gemini/Ollama/OpenRouter share `OpenAICompatibleProvider` (`providers/openai_compatible.py`); JSON extraction (`parsing.py`) and one-shot retry (`retry.py`) are shared by all providers.
- `specs/SPEC.md` contains the non-negotiable rules for the provider layer — read it before changing `src/llm/`.
- The loop is three modules: `src/events.py` (append-only `events.jsonl`, briefing ids, reply→briefing provenance), `src/state.py` (parse/render `context/current_state.md`, derive the next action), `src/state_update.py` (replies → checkpoint transitions). `src/briefing.py` calls the state-update step before generating anything, and skips the send when `state_version` is unchanged from a briefing already delivered today.
- `state_update.py` is deliberately deterministic, not model-generated: alias matching plus a positive/negative signal table. To teach it a new phrase, add an alias in `current_state.md` or a signal in the tables — don't add a model call.
- The scheduled cron and the manual three-cycle trigger call the same three functions; only `get_provider` and `send_telegram_message` are substituted in tests.

## Memory format (easy to break)

- Day files live in `memory/episodic/YYYY-MM-DD.md` and are parsed by regex in `src/memory/episodic.py`. Section headers must match exactly: `## Briefing sent`, `## Prediction (agent-generated)`, `## User replies`. A wrong header silently parses as empty.
- Day files hold **one** briefing per Cairo date. Multiple briefings in a day live only in `events.jsonl`; `briefing.py` no longer overwrites a day file.
- `memory/episodic/events.jsonl` is the authoritative store for the loop and is committed to git. It is append-only: a reply is written once with `processed: false` and consumption is recorded by a later `state_update` event's `consumes` list. Don't edit existing lines.
- Event ids are positional (`evt_000001`, …). Never truncate or rewrite the log, or ids will collide.
- `context/current_state.md` is regex-parsed the same way. `## Checkpoints` and `## Transitions` are the only sections; `_none_` is the empty marker.
- The prediction JSON block is validated against `src/memory/schema.py`; `schema_version` must be `1`.
- `memory/episodic/.last_update_id` is the Telegram ingestion cursor and is committed to git — never delete it.
- Replies arriving for days without a briefing file go to `memory/episodic/.orphans.md`.

## Conventions

- All dates/timestamps use Cairo time (UTC+3), including day-file boundaries and reply timestamps.
- `LLM_PROVIDER` and `LLM_MODEL` are GitHub repo *variables* (not secrets) so the provider can be switched from the GitHub UI; only the chosen provider's API key secret is required.
- Both workflows commit memory changes back to the repo (`contents: write`). If you add a file the pipeline writes, add its path to the workflow's `git add` line — currently `git add memory/episodic/ context/current_state.md`.
- `context/soul.md`, `context/user.md`, `context/heartbeat.md`, `context/current_state.md` are the canonical context files. The `context/* copy.md` files are untracked duplicates — edit the originals, not the copies.
