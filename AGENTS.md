# AGENTS.md

## Commands

- Sync deps: `uv sync` (CI uses `uv sync --frozen` — don't hand-edit `uv.lock`)
- Run daily briefing: `uv run python -m src.briefing`
- Run reply ingestion: `uv run python -m src.ingest_replies`
- Tests: `uv run pytest tests/ -v`; single test: `uv run pytest tests/test_episodic.py::test_name -v`

## Verification

- No linter or typechecker is configured. pytest is the only gate — don't invent lint commands.
- Tests need no secrets: root `conftest.py` sets fake `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` automatically.
- Test stack: `pytest-mock` + `freezegun`. Memory tests monkeypatch `episodic.MEMORY_DIR` to `tmp_path` — keep that pattern when adding tests.

## Architecture

- Two cron entrypoints: `src/briefing.py` (daily briefing) and `src/ingest_replies.py` (Telegram reply ingestion). Both are run as `python -m src.<module>`.
- LLM access goes through `src/llm/`: `Provider` ABC (`base.py`), `get_provider()` factory (`factory.py`) driven by the `LLM_PROVIDER` env var. Gemini/Ollama/OpenRouter share `OpenAICompatibleProvider` (`providers/openai_compatible.py`); JSON extraction (`parsing.py`) and one-shot retry (`retry.py`) are shared by all providers.
- `specs/SPEC.md` contains the non-negotiable rules for the provider layer — read it before changing `src/llm/`.

## Memory format (easy to break)

- Day files live in `memory/episodic/YYYY-MM-DD.md` and are parsed by regex in `src/memory/episodic.py`. Section headers must match exactly: `## Briefing sent`, `## Prediction (agent-generated)`, `## User replies`. A wrong header silently parses as empty.
- The prediction JSON block is validated against `src/memory/schema.py`; `schema_version` must be `1`.
- `memory/episodic/.last_update_id` is the Telegram ingestion cursor and is committed to git — never delete it.
- Replies arriving for days without a briefing file go to `memory/episodic/.orphans.md`.

## Conventions

- All dates/timestamps use Cairo time (UTC+3), including day-file boundaries and reply timestamps.
- `LLM_PROVIDER` and `LLM_MODEL` are GitHub repo *variables* (not secrets) so the provider can be switched from the GitHub UI; only the chosen provider's API key secret is required.
- Both workflows commit memory changes back to the repo (`contents: write`).
- `context/soul.md`, `context/user.md`, `context/heartbeat.md` are the canonical context files. The `context/* copy.md` files are untracked duplicates — edit the originals, not the copies.
