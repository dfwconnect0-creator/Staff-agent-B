"""Ollama Cloud provider."""
from src.llm.providers.openai_compatible import OpenAICompatibleProvider


class OllamaProvider(OpenAICompatibleProvider):
    name = "ollama"
    base_url = "https://ollama.com/v1/"
    extra_headers = {}
