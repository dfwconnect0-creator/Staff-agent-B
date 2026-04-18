"""Anthropic provider."""
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
