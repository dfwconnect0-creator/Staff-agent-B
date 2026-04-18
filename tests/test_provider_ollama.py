"""Smoke tests for OllamaProvider — correct URL and headers."""
import pytest
from unittest.mock import MagicMock, patch
import httpx


VALID_CONTENT = (
    'briefing\n\n```json\n{"schema_version": 1, "stuck_item": "X", '
    '"smallest_action": "Y", "confidence": "medium", "flags_raised": [], '
    '"questions_asked": ["Q1"]}\n```'
)


def make_http_response(content: str):
    mock = MagicMock(spec=httpx.Response)
    mock.json.return_value = {"choices": [{"message": {"content": content}}]}
    mock.raise_for_status = MagicMock()
    return mock


@patch("src.llm.providers.openai_compatible.httpx.post")
def test_ol6_1_correct_base_url(mock_post):
    mock_post.return_value = make_http_response(VALID_CONTENT)
    from src.llm.providers.ollama import OllamaProvider

    OllamaProvider("gpt-oss:120b-cloud", "k").complete("sys", "user")

    url = mock_post.call_args.args[0]
    assert url == "https://ollama.com/v1/chat/completions"


@patch("src.llm.providers.openai_compatible.httpx.post")
def test_ol6_2_correct_headers(mock_post):
    mock_post.return_value = make_http_response(VALID_CONTENT)
    from src.llm.providers.ollama import OllamaProvider

    OllamaProvider("gpt-oss:120b-cloud", "mykey").complete("sys", "user")

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer mykey"
