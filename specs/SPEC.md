# SPEC: Stage 2 — Model-Agnostic LLM Layer

**Status:** Ready to implement
**Target repo:** `chief-of-staff` (post-Stage 1, has episodic memory)
**Scope:** Introduce a provider abstraction so the agent can run against Anthropic, Gemini, Ollama Cloud, or OpenRouter without code changes to `briefing.py` or `ingest_replies.py`. Keep Anthropic working exactly as it does today.
**Out of scope:** Fallback chains, A/B routing per task, cost tracking, model benchmarking, local Ollama. These come later.

---

## 1. Problem statement

v2 is Anthropic-only. `src/claude_client.py` imports the `anthropic` SDK directly and is imported by name in `briefing.py`. This blocks three things you want:

1. **Cost** — use cheap Gemini 2.5 Flash-Lite ($0.10/$0.40 per 1M tokens, free tier available) for dev iterations instead of burning Anthropic credits on every test run.
2. **Resilience** — if one provider has an outage or a bad day, switch providers by flipping an env var instead of waiting it out.
3. **Experimentation** — compare what different models produce for the same briefing prompt without forking the codebase.

The insight that makes this easy: **three of the four providers speak the same protocol**. Ollama Cloud, Gemini (via its OpenAI-compat endpoint), and OpenRouter all expose OpenAI-compatible `/v1/chat/completions`. Anthropic is the odd one out with its `/v1/messages` API.

So we need two protocol adapters, not four, and a thin interface that both satisfy.

---

## 2. Architectural rules (non-negotiable)

1. **One `Provider` interface, one method.** Every provider implements `complete(system_prompt, user_message) -> {text, prediction, raw, provider, model}`. That's it. No streaming, no tool use, no images in Stage 2.
2. **JSON extraction lives in one place.** The `_extract_last_json_block` regex logic currently in `claude_client.py` moves into a shared helper in `src/llm/parsing.py`. All providers use it. Duplicating this across providers is how drift starts.
3. **Provider selection happens at one point.** A `get_provider()` factory function reads env vars and returns a configured provider instance. `briefing.py` calls this once; it never knows which provider it got.
4. **Env vars are the only configuration surface for Stage 2.** No YAML, no TOML sections, no runtime switches. Reason: Stage 1 patterns. Stage 1 used env vars (`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`) and nothing else, the cron runs them through GitHub Actions secrets, and it works. Adding a config file now means a new parsing module, a new precedence rule, and a new failure mode — pay that cost later if per-task routing becomes real.
5. **Anthropic stays working byte-for-byte.** The existing Stage 1 tests for `claude_client.py` must pass unchanged after the refactor. Behavior of the Anthropic path is regression-locked.
6. **The JSON-prediction contract is unchanged from Stage 1.** Same schema (`PREDICTION_SCHEMA`), same validation, same "LLM never writes to disk" rule, same `briefing.py` flow. We're replacing the inside of `ask_claude`, not the contract around it.
7. **Lowest-friction JSON handling:** prompt every provider the same way Anthropic is prompted — "output a fenced ```json block last." Parse the last block with the shared regex. If parsing fails, retry once with a terse reminder ("The JSON block was missing or malformed. Return only a fenced ```json block containing the required fields.") and append it to the prior messages. If the retry fails, return `prediction=None` and let Stage 1's existing skip-memory-write path handle it. No provider-specific structured-output features in Stage 2 — they diverge (Gemini wants `responseSchema`, OpenAI wants `json_schema`, Ollama does its own thing) and the prompt-based approach works on all three with one code path.

---

## 3. File layout after implementation

```
chief-of-staff/
├── src/
│   ├── briefing.py                      [MODIFIED — one import change, one call change]
│   ├── ingest_replies.py                [UNCHANGED]
│   ├── telegram_client.py               [UNCHANGED]
│   ├── claude_client.py                 [DELETED — logic moves into llm/providers/anthropic.py]
│   ├── llm/                             [NEW package]
│   │   ├── __init__.py
│   │   ├── base.py                      [NEW — Provider ABC + LLMResponse typed dict]
│   │   ├── factory.py                   [NEW — get_provider() reads env, returns configured instance]
│   │   ├── parsing.py                   [NEW — extract_last_json_block, shared across providers]
│   │   ├── retry.py                     [NEW — one-shot retry with reminder on bad JSON]
│   │   └── providers/
│   │       ├── __init__.py
│   │       ├── anthropic.py             [NEW — wraps Anthropic SDK, same behavior as old claude_client]
│   │       ├── openai_compatible.py     [NEW — base class for OpenAI-format providers]
│   │       ├── gemini.py                [NEW — thin subclass, points at Gemini's OpenAI-compat endpoint]
│   │       ├── ollama.py                [NEW — thin subclass, points at Ollama Cloud]
│   │       └── openrouter.py            [NEW — thin subclass, points at OpenRouter]
│   └── memory/                          [UNCHANGED]
├── tests/
│   ├── test_llm_parsing.py              [NEW — the JSON extraction tests migrate here]
│   ├── test_llm_factory.py              [NEW]
│   ├── test_llm_retry.py                [NEW]
│   ├── test_provider_anthropic.py       [NEW — replaces test_claude_client.py]
│   ├── test_provider_openai_compatible.py  [NEW]
│   ├── test_provider_gemini.py          [NEW — smoke: correct base_url + headers]
│   ├── test_provider_ollama.py          [NEW — smoke: correct base_url + headers]
│   └── test_provider_openrouter.py      [NEW — smoke: correct base_url + headers]
├── pyproject.toml                       [MODIFIED — remove anthropic-only deps, add openai]
└── README.md                            [MODIFIED — document provider selection]
```

The `tests/test_claude_client.py` file from Stage 1 is **deleted** as part of this refactor — its tests are reimplemented against the new structure in `test_provider_anthropic.py`.

---

## 4. Data contract — the `LLMResponse` shape

Every provider's `complete()` method returns the same dict:

```python
{
    "text": str,                # briefing text for Telegram (everything before the last ```json block)
    "prediction": dict | None,  # parsed JSON from the last ```json block, None if missing/malformed
    "raw": str,                 # full model output, for debugging
    "provider": str,            # "anthropic" | "gemini" | "ollama" | "openrouter"
    "model": str,               # the model string actually sent ("claude-opus-4-6", "gemini-2.5-flash-lite", etc.)
}
```

This is a superset of what `ask_claude` returned in Stage 1 — the addition is `provider` and `model` so that `briefing.py` can log which provider answered, and so future stages can record it in `memory/episodic/` if we want to.

Stage 1's `briefing.py` consumed `result["text"]` and `result["prediction"]`. That stays the same. The two new fields are additive.

---

## 5. Environment variables

One required variable and a set of provider-specific ones. Only the vars for the chosen provider need to be set.

```
# Required
LLM_PROVIDER=anthropic      # anthropic | gemini | ollama | openrouter

# Optional — overrides the default model for whichever provider is selected
LLM_MODEL=                   # e.g. "claude-opus-4-6", "gemini-2.5-flash-lite", "gpt-oss:120b-cloud"

# Provider-specific keys (set only the one you need)
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
OLLAMA_API_KEY=...
OPENROUTER_API_KEY=...
```

**Default models per provider** (used when `LLM_MODEL` is not set):

| Provider    | Default model              | Base URL                                    |
|-------------|----------------------------|---------------------------------------------|
| anthropic   | `claude-opus-4-6`          | (SDK-managed)                               |
| gemini      | `gemini-2.5-flash-lite`    | `https://generativelanguage.googleapis.com/v1beta/openai/` |
| ollama      | `gpt-oss:120b-cloud`       | `https://ollama.com/v1/`                    |
| openrouter  | `google/gemini-2.5-flash-lite` | `https://openrouter.ai/api/v1/`         |

If `LLM_PROVIDER` is unset, the factory raises `ValueError` with a clear message listing the valid options. No silent default — we want the deployment to be explicit.

If the API key for the chosen provider is missing, the factory raises `ValueError` before any HTTP call is made.

---

## 6. Module specifications

### 6.1 `src/llm/base.py`

```python
from abc import ABC, abstractmethod
from typing import TypedDict

class LLMResponse(TypedDict):
    text: str
    prediction: dict | None
    raw: str
    provider: str
    model: str

class Provider(ABC):
    name: str  # class attribute, e.g. "anthropic"

    def __init__(self, model: str, api_key: str):
        self.model = model
        self.api_key = api_key

    @abstractmethod
    def complete(self, system_prompt: str, user_message: str) -> LLMResponse:
        ...
```

### 6.2 `src/llm/parsing.py`

Exports exactly one function:

```python
def extract_last_json_block(text: str) -> tuple[dict | None, str]:
    """
    Find the LAST ```json ... ``` fenced block in text.
    Returns (parsed_dict_or_None, text_before_last_block).
    Regex: r"```json\s*\n(.*?)\n```" with re.DOTALL, take last match.
    If no block found: returns (None, text).
    If block found but invalid JSON: returns (None, text_before_last_block).
    """
```

This is the same logic that currently lives in `claude_client._extract_last_json_block`. It's moved here so the three OpenAI-compatible providers can share it with the Anthropic provider.

### 6.3 `src/llm/retry.py`

```python
RETRY_REMINDER = (
    "The JSON block was missing or malformed. "
    "Return only a single fenced ```json block at the end of your reply, "
    "containing exactly these fields: schema_version, stuck_item, smallest_action, "
    "confidence, flags_raised, questions_asked."
)

def with_json_retry(complete_fn, system_prompt, user_message) -> LLMResponse:
    """
    Call complete_fn once. If prediction is None, call it a second time
    with an appended reminder message. Return whichever response is better —
    the retry if it succeeded, otherwise the original.
    """
```

`complete_fn` is the provider's low-level single-shot call (not the public `complete` method, which delegates through `with_json_retry`). Providers are written as:

```python
def complete(self, system_prompt, user_message):
    return with_json_retry(self._single_shot, system_prompt, user_message)
```

One retry maximum. No exponential backoff, no infinite loops. If both attempts fail, `prediction` is `None` and Stage 1's graceful-degradation path (skip memory write, still send briefing) takes over.

### 6.4 `src/llm/factory.py`

```python
DEFAULT_MODELS = {
    "anthropic": "claude-opus-4-6",
    "gemini": "gemini-2.5-flash-lite",
    "ollama": "gpt-oss:120b-cloud",
    "openrouter": "google/gemini-2.5-flash-lite",
}

API_KEY_ENVS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

def get_provider() -> Provider:
    """
    Read LLM_PROVIDER and LLM_MODEL from env.
    Validate provider name is in DEFAULT_MODELS.
    Validate the provider's API key env var is set.
    Construct and return the matching provider instance.
    Raises ValueError on any missing/invalid config.
    """
```

### 6.5 `src/llm/providers/anthropic.py`

Wraps the `anthropic` SDK. Same model default (`claude-opus-4-6`), same API call shape, same max_tokens (1024), same JSON extraction — but now using the shared `extract_last_json_block` instead of a local copy.

```python
from anthropic import Anthropic
from src.llm.base import Provider, LLMResponse
from src.llm.parsing import extract_last_json_block
from src.llm.retry import with_json_retry

class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, api_key: str):
        super().__init__(model, api_key)
        self._client = Anthropic(api_key=api_key)

    def complete(self, system_prompt: str, user_message: str) -> LLMResponse:
        return with_json_retry(self._single_shot, system_prompt, user_message)

    def _single_shot(self, system_prompt: str, messages: list[dict]) -> LLMResponse:
        msg = self._client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
        )
        raw = msg.content[0].text
        prediction, text_before = extract_last_json_block(raw)
        return {
            "text": text_before if prediction is not None else raw,
            "prediction": prediction,
            "raw": raw,
            "provider": self.name,
            "model": self.model,
        }
```

Note the `messages: list[dict]` shape — that's so the retry can append the reminder as a second turn. The retry helper is the only thing that knows about multi-turn; the provider just accepts a list.

### 6.6 `src/llm/providers/openai_compatible.py`

A base class for Gemini, Ollama, and OpenRouter. All three accept the same request shape at different URLs with different keys.

```python
import httpx
from src.llm.base import Provider, LLMResponse
from src.llm.parsing import extract_last_json_block
from src.llm.retry import with_json_retry

class OpenAICompatibleProvider(Provider):
    base_url: str  # subclasses override
    extra_headers: dict  # subclasses can override

    def complete(self, system_prompt, user_message):
        return with_json_retry(self._single_shot, system_prompt, user_message)

    def _single_shot(self, system_prompt, messages):
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": system_prompt},
                *messages,  # list of {"role": "user"|"assistant", "content": "..."}
            ],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        resp = httpx.post(url, json=payload, headers=headers, timeout=60.0)
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"]
        prediction, text_before = extract_last_json_block(raw)
        return {
            "text": text_before if prediction is not None else raw,
            "prediction": prediction,
            "raw": raw,
            "provider": self.name,
            "model": self.model,
        }
```

### 6.7 `src/llm/providers/gemini.py`, `ollama.py`, `openrouter.py`

Each is ~10 lines:

```python
# gemini.py
from src.llm.providers.openai_compatible import OpenAICompatibleProvider

class GeminiProvider(OpenAICompatibleProvider):
    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    extra_headers = {}
```

```python
# ollama.py
class OllamaProvider(OpenAICompatibleProvider):
    name = "ollama"
    base_url = "https://ollama.com/v1/"
    extra_headers = {}
```

```python
# openrouter.py
class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1/"
    extra_headers = {
        "HTTP-Referer": "https://github.com/<user>/chief-of-staff",
        "X-Title": "chief-of-staff",
    }
```

OpenRouter's `HTTP-Referer` and `X-Title` headers are optional but recommended for their analytics. Constants are fine; no env-var override needed in Stage 2.

### 6.8 `src/briefing.py` changes

Only two lines change:

```python
# BEFORE (Stage 1):
from src.claude_client import ask_claude
...
result = ask_claude(system_prompt, user_message)

# AFTER (Stage 2):
from src.llm.factory import get_provider
...
result = get_provider().complete(system_prompt, user_message)
```

`result["text"]` and `result["prediction"]` work the same way. The additional `result["provider"]` and `result["model"]` are logged at INFO level for observability but not written to memory in Stage 2 (keeping memory schema stable).

---

## 7. Dependencies

`pyproject.toml` changes:

```toml
dependencies = [
    "anthropic>=0.40.0",      # still needed — Anthropic SDK has no OpenAI-compat endpoint we want to use
    "httpx>=0.27.0",          # already present, used by openai_compatible.py
    "jsonschema>=4.0.0",
]
```

**No new runtime dependency.** We're using `httpx` (already present) to hit the OpenAI-compatible endpoints directly. Adding the `openai` SDK would be a 30+ MB install for one HTTP call we can make in 20 lines. Not worth it.

---

## 8. GitHub Actions changes

Both workflows need `LLM_PROVIDER` and the matching key passed in. Example for `daily-briefing.yml`:

```yaml
- run: uv run python -m src.briefing
  env:
    LLM_PROVIDER: ${{ vars.LLM_PROVIDER || 'anthropic' }}
    LLM_MODEL: ${{ vars.LLM_MODEL }}
    ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
    GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
    OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}
    OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
    TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
    TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
```

`vars.LLM_PROVIDER` is a GitHub repo-level variable (not a secret — it's not sensitive) so you can flip between providers from the GitHub UI without editing workflow YAML. `secrets.*_API_KEY` entries are set for whichever providers you actually use; unused ones can be absent and the factory's validation will still catch the specific missing one.

`ingest_replies.yml` doesn't use the LLM (it's Telegram-only) so it stays unchanged.

---

## 9. Migration path (how Stage 1 tests survive)

Stage 1 has `tests/test_claude_client.py` with 4 tests. They test `ask_claude` directly. In Stage 2:

1. Delete `tests/test_claude_client.py` (the module it tests no longer exists).
2. Create `tests/test_provider_anthropic.py` with the same 4 test cases rewritten against `AnthropicProvider.complete()`. Behavior must be identical.
3. `tests/test_briefing_flow.py` patches `src.briefing.ask_claude`. In Stage 2 it patches `src.briefing.get_provider` to return a mock provider whose `.complete()` returns the mock response. One-line change per test.

This keeps Stage 1's test coverage intact while switching what's being tested.

---

## 10. Non-goals (do NOT implement in this stage)

- **No fallback chains.** If Gemini fails, the run fails. Retry-across-providers is stage 3 at earliest.
- **No per-task routing.** `briefing.py` and any future tasks all use the same provider. A/B testing comes later.
- **No provider-native structured-output APIs.** No Gemini `responseSchema`, no OpenAI `json_schema`. The prompt-based fenced-JSON approach is uniform and works everywhere.
- **No cost tracking or token accounting.** Logging `provider` and `model` is enough; real cost dashboards are a separate project.
- **No local Ollama support.** Cloud-only in Stage 2. Adding local means adding base-URL config and handling no-auth, which doubles the surface area.
- **No streaming.** Briefings are short, latency isn't the bottleneck, and streaming complicates JSON extraction.
- **No config file.** Env vars only. If per-task routing becomes real in stage 3, revisit.

---

## 11. Success criteria

- All Stage 1 tests still pass (either renamed as per section 9, or unchanged).
- New tests from `TEST_PLAN.md` pass.
- `LLM_PROVIDER=anthropic` produces byte-identical behavior to pre-refactor.
- `LLM_PROVIDER=gemini GEMINI_API_KEY=...` produces a valid briefing when run live.
- Flipping providers requires only an env var change. No code edits.
