import pytest
from unittest.mock import MagicMock, patch


def make_mock_response(text: str):
    mock = MagicMock()
    mock.content = [MagicMock(text=text)]
    return mock


@patch("src.claude_client.anthropic.Anthropic")
def test_response_with_json_block_parses_correctly(mock_anthropic_cls):
    raw = '🎯 briefing here\n\n```json\n{"schema_version": 1, "stuck_item": "X", "smallest_action": "Y", "confidence": "medium", "flags_raised": [], "questions_asked": ["Q1"]}\n```'
    mock_anthropic_cls.return_value.messages.create.return_value = make_mock_response(raw)

    from src.claude_client import ask_claude
    result = ask_claude("system", "user")

    assert "🎯 briefing here" in result["text"]
    assert result["prediction"] is not None
    assert result["prediction"]["stuck_item"] == "X"
    assert result["raw"] == raw


@patch("src.claude_client.anthropic.Anthropic")
def test_response_with_no_json_block_returns_none_prediction(mock_anthropic_cls):
    raw = "🎯 briefing text only, no json"
    mock_anthropic_cls.return_value.messages.create.return_value = make_mock_response(raw)

    from src.claude_client import ask_claude
    result = ask_claude("system", "user")

    assert result["text"] == raw
    assert result["prediction"] is None


@patch("src.claude_client.anthropic.Anthropic")
def test_response_with_malformed_json_returns_none_prediction(mock_anthropic_cls):
    raw = "🎯 briefing\n\n```json\n{bad json\n```"
    mock_anthropic_cls.return_value.messages.create.return_value = make_mock_response(raw)

    from src.claude_client import ask_claude
    result = ask_claude("system", "user")

    assert result["prediction"] is None
    assert "🎯 briefing" in result["text"]


@patch("src.claude_client.anthropic.Anthropic")
def test_multiple_json_blocks_uses_last_one(mock_anthropic_cls):
    raw = 'Here is an example:\n\n```json\n{"example": true}\n```\n\nActual prediction:\n\n```json\n{"schema_version": 1, "stuck_item": "real", "smallest_action": "do it", "confidence": "high", "flags_raised": [], "questions_asked": ["Q?"]}\n```'
    mock_anthropic_cls.return_value.messages.create.return_value = make_mock_response(raw)

    from src.claude_client import ask_claude
    result = ask_claude("system", "user")

    assert result["prediction"]["stuck_item"] == "real"
    assert "Actual prediction" in result["text"]
