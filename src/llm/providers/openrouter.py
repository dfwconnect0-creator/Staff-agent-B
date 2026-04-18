"""OpenRouter provider."""
from src.llm.providers.openai_compatible import OpenAICompatibleProvider


class OpenRouterProvider(OpenAICompatibleProvider):
    name = "openrouter"
    base_url = "https://openrouter.ai/api/v1/"
    extra_headers = {
        "HTTP-Referer": "https://github.com/<user>/chief-of-staff",
        "X-Title": "chief-of-staff",
    }
