"""Gemini provider (OpenAI-compat endpoint)."""
from src.llm.providers.openai_compatible import OpenAICompatibleProvider


class GeminiProvider(OpenAICompatibleProvider):
    name = "gemini"
    base_url = "https://generativelanguage.googleapis.com/v1beta/openai/"
    extra_headers = {}
