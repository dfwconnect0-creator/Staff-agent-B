"""Provider ABC and LLMResponse type."""
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
