"""OpenAI-compatible provider base class."""
import httpx

from src.llm.base import Provider, LLMResponse
from src.llm.parsing import extract_last_json_block
from src.llm.retry import with_json_retry


class OpenAICompatibleProvider(Provider):
    base_url: str  # subclasses override
    extra_headers: dict  # subclasses can override

    def complete(self, system_prompt: str, user_message: str) -> LLMResponse:
        return with_json_retry(self._single_shot, system_prompt, user_message)

    def _single_shot(self, system_prompt: str, messages: list[dict]) -> LLMResponse:
        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": [
                {"role": "system", "content": system_prompt},
                *messages,
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
