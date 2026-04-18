"""Tests for OpenAICompatibleProvider base class."""
import json
import pytest
import httpx
from unittest.mock import MagicMock, patch


# Concrete subclass for testing (defined here in the test file)
def make_test_provider_class():
    from src.llm.providers.openai_compatible import OpenAICompatibleProvider

    class _TestProvider(OpenAICompatibleProvider):
        name = "test"
        base_url = "https://example.com/v1/"
        extra_headers = {}

    return _TestProvider


def make_http_response(content: str, status_code: int = 200):
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    mock.raise_for_status = MagicMock()
    return mock


VALID_CONTENT = (
    '🎯 briefing\n\n```json\n'
    '{"schema_version": 1, "stuck_item": "X", "smallest_action": "Y", '
    '"confidence": "medium", "flags_raised": [], "questions_asked": ["Q1"]}\n```'
)


@patch("src.llm.providers.openai_compatible.httpx.post")
def test_o5_1_request_shape(mock_post):
    mock_post.return_value = make_http_response(VALID_CONTENT)
    _TestProvider = make_test_provider_class()

    _TestProvider("m", "k").complete("sys prompt", "user msg")

    call_kwargs = mock_post.call_args
    payload = call_kwargs.kwargs["json"]
    assert payload["model"] == "m"
    assert payload["max_tokens"] == 1024
    assert payload["messages"][0] == {"role": "system", "content": "sys prompt"}
    assert payload["messages"][1] == {"role": "user", "content": "user msg"}


@patch("src.llm.providers.openai_compatible.httpx.post")
def test_o5_2_authorization_header(mock_post):
    mock_post.return_value = make_http_response(VALID_CONTENT)
    _TestProvider = make_test_provider_class()

    _TestProvider("m", "my-key").complete("sys", "user")

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer my-key"
    assert headers["Content-Type"] == "application/json"


@patch("src.llm.providers.openai_compatible.httpx.post")
def test_o5_3_extra_headers_included(mock_post):
    mock_post.return_value = make_http_response(VALID_CONTENT)

    from src.llm.providers.openai_compatible import OpenAICompatibleProvider

    class _CustomProvider(OpenAICompatibleProvider):
        name = "custom"
        base_url = "https://example.com/v1/"
        extra_headers = {"X-Custom": "abc"}

    _CustomProvider("m", "k").complete("sys", "user")

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["X-Custom"] == "abc"
    assert headers["Authorization"] == "Bearer k"
    assert headers["Content-Type"] == "application/json"


@patch("src.llm.providers.openai_compatible.httpx.post")
def test_o5_4_url_no_double_slash(mock_post):
    mock_post.return_value = make_http_response(VALID_CONTENT)
    _TestProvider = make_test_provider_class()

    _TestProvider("m", "k").complete("sys", "user")

    url = mock_post.call_args.args[0]
    assert url == "https://example.com/v1/chat/completions"


@patch("src.llm.providers.openai_compatible.httpx.post")
def test_o5_5_response_parsing(mock_post):
    mock_post.return_value = make_http_response(VALID_CONTENT)
    _TestProvider = make_test_provider_class()

    result = _TestProvider("m", "k").complete("sys", "user")

    assert result["raw"] == VALID_CONTENT
    assert result["prediction"] is not None
    assert "🎯 briefing" in result["text"]


@patch("src.llm.providers.openai_compatible.httpx.post")
def test_o5_6_http_error_propagates(mock_post):
    mock_resp = MagicMock(spec=httpx.Response)
    mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "404", request=MagicMock(), response=MagicMock()
    )
    mock_post.return_value = mock_resp
    _TestProvider = make_test_provider_class()

    with pytest.raises(httpx.HTTPStatusError):
        _TestProvider("m", "k").complete("sys", "user")


@patch("src.llm.providers.openai_compatible.httpx.post")
def test_o5_7_retry_on_bad_json_triggers_second_post(mock_post):
    bad_content = "no json here"
    good_content = VALID_CONTENT
    mock_post.side_effect = [
        make_http_response(bad_content),
        make_http_response(good_content),
    ]
    _TestProvider = make_test_provider_class()

    result = _TestProvider("m", "k").complete("sys", "user")

    assert mock_post.call_count == 2
    assert result["prediction"] is not None
