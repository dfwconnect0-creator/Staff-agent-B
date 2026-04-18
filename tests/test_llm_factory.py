"""Tests for src/llm/factory.py — provider selection from env."""
import pytest
from src.llm.factory import get_provider


def test_f3_1_anthropic_provider_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    from src.llm.providers.anthropic import AnthropicProvider
    p = get_provider()
    assert isinstance(p, AnthropicProvider)
    assert p.model == "claude-opus-4-6"


def test_f3_2_gemini_provider_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    from src.llm.providers.gemini import GeminiProvider
    p = get_provider()
    assert isinstance(p, GeminiProvider)
    assert p.model == "gemini-2.5-flash-lite"


def test_f3_3_ollama_provider_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("OLLAMA_API_KEY", "fake")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    from src.llm.providers.ollama import OllamaProvider
    p = get_provider()
    assert isinstance(p, OllamaProvider)
    assert p.model == "gpt-oss:120b-cloud"


def test_f3_4_openrouter_provider_selected(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")
    monkeypatch.delenv("LLM_MODEL", raising=False)

    from src.llm.providers.openrouter import OpenRouterProvider
    p = get_provider()
    assert isinstance(p, OpenRouterProvider)
    assert p.model == "google/gemini-2.5-flash-lite"


def test_f3_5_llm_model_overrides_default(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.setenv("LLM_MODEL", "gemini-2.5-pro")

    p = get_provider()
    assert p.model == "gemini-2.5-pro"


def test_f3_6_missing_llm_provider_raises(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)

    with pytest.raises(ValueError) as exc_info:
        get_provider()
    msg = str(exc_info.value)
    assert "LLM_PROVIDER" in msg
    # should list valid options
    assert "anthropic" in msg


def test_f3_7_invalid_llm_provider_raises(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mistral")

    with pytest.raises(ValueError) as exc_info:
        get_provider()
    msg = str(exc_info.value)
    assert "mistral" in msg
    assert "anthropic" in msg


def test_f3_8_missing_api_key_raises(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(ValueError) as exc_info:
        get_provider()
    msg = str(exc_info.value)
    assert "GEMINI_API_KEY" in msg


def test_f3_9_unrelated_api_keys_ignored(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    # Should not raise
    p = get_provider()
    assert p is not None
