"""Tests for AnthropicProvider — replaces test_claude_client.py."""
import pytest
from unittest.mock import MagicMock, patch


def make_mock_response(text: str):
    mock = MagicMock()
    mock.content = [MagicMock(text=text)]
    return mock


RAW_WITH_JSON = (
    '🎯 briefing here\n\n```json\n'
    '{"schema_version": 1, "stuck_item": "X", "smallest_action": "Y", '
    '"confidence": "medium", "flags_raised": [], "questions_asked": ["Q1"]}\n```'
)
RAW_NO_JSON = "🎯 briefing text only, no json"
RAW_MALFORMED = "🎯 briefing\n\n```json\n{bad json\n```"
RAW_TWO_BLOCKS = (
    'Here is an example:\n\n```json\n{"example": true}\n```\n\n'
    'Actual prediction:\n\n```json\n'
    '{"schema_version": 1, "stuck_item": "real", "smallest_action": "do it", '
    '"confidence": "high", "flags_raised": [], "questions_asked": ["Q?"]}\n```'
)


@patch("src.llm.providers.anthropic.Anthropic")
def test_a4_1_response_with_json_parses_correctly(mock_cls):
    mock_cls.return_value.messages.create.return_value = make_mock_response(RAW_WITH_JSON)

    from src.llm.providers.anthropic import AnthropicProvider
    result = AnthropicProvider("claude-opus-4-6", "fake-key").complete("sys", "user")

    assert "🎯 briefing here" in result["text"]
    assert result["prediction"] is not None
    assert result["prediction"]["stuck_item"] == "X"
    assert result["raw"] == RAW_WITH_JSON
    assert result["provider"] == "anthropic"
    assert result["model"] == "claude-opus-4-6"


@patch("src.llm.providers.anthropic.Anthropic")
def test_a4_2_no_json_block_returns_none_prediction(mock_cls):
    # Both calls return no JSON — provider retries once then gives up
    mock_cls.return_value.messages.create.return_value = make_mock_response(RAW_NO_JSON)

    from src.llm.providers.anthropic import AnthropicProvider
    result = AnthropicProvider("claude-opus-4-6", "fake-key").complete("sys", "user")

    assert result["prediction"] is None
    assert result["text"] == RAW_NO_JSON


@patch("src.llm.providers.anthropic.Anthropic")
def test_a4_3_malformed_json_retries_twice_returns_none(mock_cls):
    mock_cls.return_value.messages.create.return_value = make_mock_response(RAW_MALFORMED)

    from src.llm.providers.anthropic import AnthropicProvider
    result = AnthropicProvider("claude-opus-4-6", "fake-key").complete("sys", "user")

    # SDK called exactly twice (original + retry)
    assert mock_cls.return_value.messages.create.call_count == 2
    assert result["prediction"] is None


@patch("src.llm.providers.anthropic.Anthropic")
def test_a4_4_multiple_blocks_last_wins(mock_cls):
    mock_cls.return_value.messages.create.return_value = make_mock_response(RAW_TWO_BLOCKS)

    from src.llm.providers.anthropic import AnthropicProvider
    result = AnthropicProvider("claude-opus-4-6", "fake-key").complete("sys", "user")

    assert result["prediction"]["stuck_item"] == "real"
    assert "Actual prediction" in result["text"]


@patch("src.llm.providers.anthropic.Anthropic")
def test_a4_5_successful_retry_after_initial_bad_json(mock_cls):
    good_raw = (
        'retry briefing\n\n```json\n'
        '{"schema_version": 1, "stuck_item": "Z", "smallest_action": "do Z", '
        '"confidence": "low", "flags_raised": [], "questions_asked": ["Q?"]}\n```'
    )
    mock_cls.return_value.messages.create.side_effect = [
        make_mock_response(RAW_MALFORMED),
        make_mock_response(good_raw),
    ]

    from src.llm.providers.anthropic import AnthropicProvider
    result = AnthropicProvider("claude-opus-4-6", "fake-key").complete("sys", "user")

    assert mock_cls.return_value.messages.create.call_count == 2
    assert result["prediction"] is not None
    assert result["prediction"]["stuck_item"] == "Z"


@patch("src.llm.providers.anthropic.Anthropic")
def test_a4_6_model_and_provider_fields_populated(mock_cls):
    mock_cls.return_value.messages.create.return_value = make_mock_response(RAW_WITH_JSON)

    from src.llm.providers.anthropic import AnthropicProvider
    result = AnthropicProvider("some-model", "fake-key").complete("sys", "user")

    assert result["provider"] == "anthropic"
    assert result["model"] == "some-model"
