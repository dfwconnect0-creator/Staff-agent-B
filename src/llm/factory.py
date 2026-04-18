"""Provider factory — reads env vars, returns configured provider instance."""
import os

from src.llm.base import Provider

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
    provider_name = os.environ.get("LLM_PROVIDER")
    if not provider_name:
        valid = ", ".join(DEFAULT_MODELS.keys())
        raise ValueError(
            f"LLM_PROVIDER not set. Valid options: {valid}"
        )

    if provider_name not in DEFAULT_MODELS:
        valid = ", ".join(DEFAULT_MODELS.keys())
        raise ValueError(
            f"Unknown LLM_PROVIDER '{provider_name}'. Valid options: {valid}"
        )

    key_env = API_KEY_ENVS[provider_name]
    api_key = os.environ.get(key_env)
    if not api_key:
        raise ValueError(
            f"{key_env} is not set. Required for provider '{provider_name}'."
        )

    model = os.environ.get("LLM_MODEL") or DEFAULT_MODELS[provider_name]

    if provider_name == "anthropic":
        from src.llm.providers.anthropic import AnthropicProvider
        return AnthropicProvider(model, api_key)
    elif provider_name == "gemini":
        from src.llm.providers.gemini import GeminiProvider
        return GeminiProvider(model, api_key)
    elif provider_name == "ollama":
        from src.llm.providers.ollama import OllamaProvider
        return OllamaProvider(model, api_key)
    elif provider_name == "openrouter":
        from src.llm.providers.openrouter import OpenRouterProvider
        return OpenRouterProvider(model, api_key)
