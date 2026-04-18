# TEST PLAN: Stage 2 — Model-Agnostic LLM Layer

Acceptance tests that define "done." Each test is Given/When/Then so it maps directly to a pytest function. **Write tests first. They fail. Then implement. This is non-negotiable — it's the one thing Stage 1 got sloppy on.**

Test file layout:

```
tests/
├── test_llm_parsing.py                   # unit — shared JSON extraction
├── test_llm_retry.py                     # unit — one-shot retry behavior
├── test_llm_factory.py                   # unit — provider selection from env
├── test_provider_anthropic.py            # unit — Anthropic, replaces test_claude_client.py
├── test_provider_openai_compatible.py    # unit — shared base class behavior
├── test_provider_gemini.py               # smoke — correct URL + headers
├── test_provider_ollama.py               # smoke — correct URL + headers
├── test_provider_openrouter.py           # smoke — correct URL + headers
└── test_briefing_flow.py                 # integration — updated patches (existing file)
```

---

## Unit tests — parsing.py

### P1.1 Single JSON block extracted
- **Given** text `"some briefing\n\n```json\n{\"a\": 1}\n```"`
- **When** `extract_last_json_block` is called
- **Then** returns `({"a": 1}, "some briefing")`

### P1.2 Multiple JSON blocks — last wins
- **Given** text with two fenced json blocks
- **When** `extract_last_json_block` is called
- **Then** returns the SECOND block's parsed content and the text before it (not between them)

### P1.3 No JSON block
- **Given** text `"just briefing, no json"`
- **When** `extract_last_json_block` is called
- **Then** returns `(None, "just briefing, no json")`

### P1.4 Malformed JSON in last block
- **Given** text `"briefing\n\n```json\n{broken\n```"`
- **When** `extract_last_json_block` is called
- **Then** returns `(None, "briefing")` — text still trimmed to before the (malformed) block

### P1.5 JSON with multi-line string content
- **Given** a valid JSON block containing newlines inside string values
- **When** extracted
- **Then** parses correctly — regex uses `re.DOTALL`

---

## Unit tests — retry.py

These use a mock `complete_fn` that can be configured to return different responses on successive calls.

### R2.1 First call succeeds, no retry
- **Given** `complete_fn` that returns a valid prediction on first call
- **When** `with_json_retry` is called
- **Then** `complete_fn` is called exactly once and its result is returned

### R2.2 First call fails, retry succeeds
- **Given** `complete_fn` that returns `prediction=None` first, valid prediction second
- **When** `with_json_retry` is called
- **Then** `complete_fn` is called twice, the second result is returned

### R2.3 Retry call includes reminder as second message
- **Given** `complete_fn` that returns `prediction=None` first
- **When** `with_json_retry` is called
- **Then** on the retry, the `messages` list passed to `complete_fn` contains the original user message, the assistant's first raw reply, and the `RETRY_REMINDER` as a new user turn (3 messages total)

### R2.4 Both calls fail
- **Given** `complete_fn` that returns `prediction=None` both times
- **When** `with_json_retry` is called
- **Then** `complete_fn` is called exactly twice, the second `None` result is returned (does not raise)

### R2.5 First call raises exception
- **Given** `complete_fn` that raises `httpx.HTTPError` on first call
- **When** `with_json_retry` is called
- **Then** the exception propagates — retry is only for bad JSON, not for HTTP errors

---

## Unit tests — factory.py

Use `monkeypatch.setenv` / `monkeypatch.delenv` in every test.

### F3.1 Anthropic provider selected by env
- **Given** `LLM_PROVIDER=anthropic`, `ANTHROPIC_API_KEY=fake`
- **When** `get_provider()` is called
- **Then** returns `AnthropicProvider` instance with `model="claude-opus-4-6"`

### F3.2 Gemini provider selected by env
- **Given** `LLM_PROVIDER=gemini`, `GEMINI_API_KEY=fake`
- **When** `get_provider()` is called
- **Then** returns `GeminiProvider` with `model="gemini-2.5-flash-lite"`

### F3.3 Ollama provider selected by env
- **Given** `LLM_PROVIDER=ollama`, `OLLAMA_API_KEY=fake`
- **When** `get_provider()` is called
- **Then** returns `OllamaProvider` with `model="gpt-oss:120b-cloud"`

### F3.4 OpenRouter provider selected by env
- **Given** `LLM_PROVIDER=openrouter`, `OPENROUTER_API_KEY=fake`
- **When** `get_provider()` is called
- **Then** returns `OpenRouterProvider` with `model="google/gemini-2.5-flash-lite"`

### F3.5 LLM_MODEL overrides default
- **Given** `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-2.5-pro`, `GEMINI_API_KEY=fake`
- **When** `get_provider()` is called
- **Then** returned provider has `model="gemini-2.5-pro"`

### F3.6 Missing LLM_PROVIDER raises ValueError
- **Given** `LLM_PROVIDER` not set
- **When** `get_provider()` is called
- **Then** raises `ValueError` with a message listing the valid provider names

### F3.7 Invalid LLM_PROVIDER raises ValueError
- **Given** `LLM_PROVIDER=mistral`
- **When** `get_provider()` is called
- **Then** raises `ValueError` mentioning `mistral` and listing valid providers

### F3.8 Missing API key for selected provider raises ValueError
- **Given** `LLM_PROVIDER=gemini`, `GEMINI_API_KEY` not set
- **When** `get_provider()` is called
- **Then** raises `ValueError` mentioning `GEMINI_API_KEY`

### F3.9 Presence of unrelated API keys doesn't matter
- **Given** `LLM_PROVIDER=gemini`, `GEMINI_API_KEY=fake`, `ANTHROPIC_API_KEY` not set
- **When** `get_provider()` is called
- **Then** succeeds — only the selected provider's key is checked

---

## Unit tests — provider_anthropic.py (replaces old test_claude_client.py)

Mock `anthropic.Anthropic`. These are the Stage 1 T3.1–T3.4 tests, rewritten against `AnthropicProvider`.

### A4.1 Response with briefing text and JSON block parses correctly
- **Given** mocked Anthropic response containing briefing + valid JSON block
- **When** `AnthropicProvider("claude-opus-4-6", "fake-key").complete(...)` is called
- **Then** returned dict has: `text` = briefing portion, `prediction` = parsed dict, `raw` = full text, `provider="anthropic"`, `model="claude-opus-4-6"`

### A4.2 Response with no JSON block returns None prediction
- **Given** mocked response with no JSON block
- **When** `complete` is called
- **Then** `prediction=None`, `text` equals raw, provider retries once then gives up

### A4.3 Response with malformed JSON returns None prediction after retry
- **Given** mocked Anthropic client that returns malformed JSON on both calls
- **When** `complete` is called
- **Then** Anthropic SDK is called exactly twice (initial + retry), `prediction=None`

### A4.4 Multiple JSON blocks — last one wins
- **Given** mocked response with two fenced json blocks, the second valid
- **When** `complete` is called
- **Then** `prediction` = second block's content, `text` is everything before the last block

### A4.5 Successful retry after initial bad JSON
- **Given** mocked client that returns malformed JSON first, valid JSON second
- **When** `complete` is called
- **Then** Anthropic SDK is called twice, `prediction` is populated from second call

### A4.6 Model and provider fields populated
- **Given** any valid response
- **When** `complete` is called with `AnthropicProvider("some-model", "key")`
- **Then** returned dict has `provider="anthropic"` and `model="some-model"`

---

## Unit tests — provider_openai_compatible.py

These test the base class using a minimal concrete subclass defined in the test file:

```python
class _TestProvider(OpenAICompatibleProvider):
    name = "test"
    base_url = "https://example.com/v1/"
    extra_headers = {}
```

Mock `httpx.post` throughout.

### O5.1 Request shape matches OpenAI chat-completions format
- **Given** mocked `httpx.post` that records the request payload
- **When** `_TestProvider("m", "k").complete("sys prompt", "user msg")` is called with a response containing valid JSON
- **Then** the POST body has `model="m"`, `messages=[{"role":"system","content":"sys prompt"},{"role":"user","content":"user msg"}]`, `max_tokens=1024`

### O5.2 Authorization header uses Bearer token
- **Given** mocked `httpx.post`
- **When** `complete` is called with `api_key="my-key"`
- **Then** the request headers include `Authorization: Bearer my-key` and `Content-Type: application/json`

### O5.3 extra_headers are included
- **Given** a subclass with `extra_headers = {"X-Custom": "abc"}`
- **When** `complete` is called
- **Then** request headers include `X-Custom: abc` and also the standard Authorization/Content-Type

### O5.4 URL is `{base_url}/chat/completions` with no double slash
- **Given** `base_url = "https://example.com/v1/"` (trailing slash)
- **When** `complete` is called
- **Then** `httpx.post` is called with URL `https://example.com/v1/chat/completions` (single slash)

### O5.5 Response parsing — content extracted from choices[0].message.content
- **Given** mocked HTTP 200 response with body `{"choices":[{"message":{"content":"🎯 briefing\n\n```json\n{\"schema_version\":1,...}\n```"}}]}`
- **When** `complete` is called
- **Then** returned `raw` equals the content string, `prediction` is parsed, `text` is the briefing portion

### O5.6 HTTP error propagates
- **Given** mocked `httpx.post` that raises `httpx.HTTPStatusError`
- **When** `complete` is called
- **Then** the exception is raised — no swallowing (this test guards R2.5's semantics at the provider layer)

### O5.7 Retry on bad JSON triggers second POST
- **Given** mocked `httpx.post` that returns content with malformed JSON first, valid JSON second
- **When** `complete` is called
- **Then** `httpx.post` is called exactly twice, final result has valid `prediction`

---

## Smoke tests — provider_gemini.py, provider_ollama.py, provider_openrouter.py

Each file has 2 tests. These verify only the URL and headers — the behavior is already covered by `test_provider_openai_compatible.py`.

### G6.1 / OL6.1 / OR6.1 — Correct base_url
- **When** provider is instantiated and `complete` is called
- **Then** `httpx.post` is invoked with:
  - Gemini: `https://generativelanguage.googleapis.com/v1beta/openai/chat/completions`
  - Ollama: `https://ollama.com/v1/chat/completions`
  - OpenRouter: `https://openrouter.ai/api/v1/chat/completions`

### G6.2 / OL6.2 / OR6.2 — Correct headers
- **When** `complete` is called with api_key="k"
- **Then** request headers include `Authorization: Bearer k`, and for OpenRouter specifically also `HTTP-Referer` and `X-Title`

---

## Integration tests — briefing_flow.py (MODIFIED from Stage 1)

All seven Stage 1 integration tests remain. The only change is how the LLM is mocked — `src.briefing.ask_claude` no longer exists, so patches target `src.briefing.get_provider`.

### I7.1 All Stage 1 briefing tests still pass after refactor
- **Given** Stage 1 T5.1–T5.7 tests, with `patch("src.briefing.ask_claude", ...)` replaced by `patch("src.briefing.get_provider", return_value=<mock provider>)` where mock provider's `.complete()` returns the same dict shape Stage 1 used
- **When** the test suite runs
- **Then** all 7 tests pass with no other changes

### I7.2 Provider/model fields are logged (new)
- **Given** a mock provider that returns `{"text": "...", "prediction": {...}, "raw": "...", "provider": "gemini", "model": "gemini-2.5-flash-lite"}`
- **When** `briefing.main()` runs
- **Then** the captured log output contains a line mentioning `"gemini"` and `"gemini-2.5-flash-lite"` (log level INFO or above)

---

## Manual/smoke tests (run once after implementation)

### M1. Anthropic still works
```
LLM_PROVIDER=anthropic ANTHROPIC_API_KEY=... \
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
uv run python -m src.briefing
```
Expected: Telegram message arrives. `memory/episodic/YYYY-MM-DD.md` created. Behavior identical to Stage 1.

### M2. Gemini works on free tier
```
LLM_PROVIDER=gemini GEMINI_API_KEY=... \
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
uv run python -m src.briefing
```
Expected: Telegram message arrives. `memory/episodic/...md` file exists. Log line confirms `provider=gemini, model=gemini-2.5-flash-lite`. Delete this test file before the real daily cron runs.

### M3. Ollama Cloud works
```
LLM_PROVIDER=ollama OLLAMA_API_KEY=... \
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
uv run python -m src.briefing
```
Expected: same as above. Delete the test file.

### M4. OpenRouter works
```
LLM_PROVIDER=openrouter OPENROUTER_API_KEY=... \
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... \
uv run python -m src.briefing
```
Expected: same as above. Delete the test file.

### M5. Missing provider config fails fast
```
uv run python -m src.briefing
```
(No env vars set.) Expected: `ValueError: LLM_PROVIDER not set. Valid options: anthropic, gemini, ollama, openrouter`. Telegram is NOT called. No memory file written.

### M6. Invalid JSON from a real provider recovers via retry
Hardest to test manually. Best approach: run M2 with `LLM_MODEL=gemini-2.5-flash-lite` for several days. If you ever see a "skipping memory write" log line followed by a successful Telegram send, the graceful-degradation + retry path worked. Passive observation; not a blocking test.

---

## What "done" means

- [ ] All P, R, F, A, O, G, OL, OR, I tests are written and passing
- [ ] Stage 1 tests in `test_schema.py`, `test_episodic.py`, `test_ingest_replies.py` still pass unchanged
- [ ] `tests/test_claude_client.py` is deleted (its cases live on as A4.*)
- [ ] M1–M5 manual tests executed successfully
- [ ] `src/claude_client.py` is deleted
- [ ] Flipping `LLM_PROVIDER` env var in GitHub Actions variables switches the briefing provider with no code change on the next scheduled run
- [ ] README updated with the "choosing a provider" section
- [ ] Non-goals NOT implemented (no fallback chains, no YAML config, no native structured outputs)
