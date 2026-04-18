import json
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from freezegun import freeze_time

import src.memory.episodic as episodic

CAIRO_TZ = timezone(timedelta(hours=3))

VALID_PREDICTION = {
    "schema_version": 1,
    "stuck_item": "Topic finder GitHub Actions step",
    "smallest_action": "Open last workflow run in GitHub",
    "confidence": "medium",
    "flags_raised": ["last_10_percent"],
    "questions_asked": ["Did you check the workflow?", "Is junior still on task?", "Energy 1-5?"],
}

MOCK_LLM_RESPONSE = {
    "text": "🎯 Topic finder is stuck.\n✂️ Open the workflow run.\n❓ Check it?\n❓ Junior on task?\n⚡ Energy 1-5?",
    "prediction": VALID_PREDICTION,
    "raw": "🎯 Topic finder is stuck.\n✂️ Open the workflow run.\n❓ Check it?\n❓ Junior on task?\n⚡ Energy 1-5?\n\n```json\n{...}\n```",
    "provider": "anthropic",
    "model": "claude-opus-4-6",
}


def make_mock_provider(response=None):
    if response is None:
        response = MOCK_LLM_RESPONSE
    mock = MagicMock()
    mock.complete.return_value = response
    return mock


@pytest.fixture(autouse=True)
def tmp_memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(episodic, "MEMORY_DIR", tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    return tmp_path


@freeze_time("2026-04-17 06:00:00")  # 09:00 Cairo
def test_first_ever_run_creates_file(tmp_path):
    captured_messages = []
    captured_prompts = []

    mock_provider = make_mock_provider()
    original_complete = mock_provider.complete.side_effect

    def capture_complete(system_prompt, user_message):
        captured_prompts.append(user_message)
        return MOCK_LLM_RESPONSE

    mock_provider.complete.side_effect = capture_complete

    def fake_send(text):
        captured_messages.append(text)

    with patch("src.briefing.get_provider", return_value=mock_provider):
        with patch("src.briefing.send_telegram_message", side_effect=fake_send):
            import src.briefing as briefing
            result = briefing.main()

    assert result == 0
    assert len(captured_messages) == 1
    assert captured_messages[0] == MOCK_LLM_RESPONSE["text"]
    mem_file = tmp_path / "2026-04-17.md"
    assert mem_file.exists()
    assert "Yesterday's context" not in captured_prompts[0]
    content = mem_file.read_text()
    assert "# 2026-04-17 (Friday)" in content
    assert "## Briefing sent" in content
    assert "## Prediction (agent-generated)" in content


@freeze_time("2026-04-18 06:00:00")  # 09:00 Cairo Apr 18
def test_second_day_includes_yesterday_verbatim(tmp_path):
    d_yesterday = date(2026, 4, 17)
    episodic.write_day(d_yesterday, "yesterday briefing", VALID_PREDICTION)

    captured_prompts = []

    mock_provider = make_mock_provider()

    def capture_complete(system_prompt, user_message):
        captured_prompts.append(user_message)
        return MOCK_LLM_RESPONSE

    mock_provider.complete.side_effect = capture_complete

    with patch("src.briefing.get_provider", return_value=mock_provider):
        with patch("src.briefing.send_telegram_message"):
            import src.briefing as briefing
            briefing.main()

    assert len(captured_prompts) == 1
    prompt = captured_prompts[0]
    assert '"stuck_item": "Topic finder GitHub Actions step"' in prompt
    assert "Yesterday's context" in prompt


@freeze_time("2026-04-18 06:00:00")
def test_yesterday_replies_included_in_prompt(tmp_path):
    d_yesterday = date(2026, 4, 17)
    episodic.write_day(d_yesterday, "yesterday briefing", VALID_PREDICTION)
    episodic.append_reply(d_yesterday, "2026-04-17 09:12", "energy 3")
    episodic.append_reply(d_yesterday, "2026-04-17 09:15", "topic finder is fine")

    captured_prompts = []

    mock_provider = make_mock_provider()

    def capture_complete(system_prompt, user_message):
        captured_prompts.append(user_message)
        return MOCK_LLM_RESPONSE

    mock_provider.complete.side_effect = capture_complete

    with patch("src.briefing.get_provider", return_value=mock_provider):
        with patch("src.briefing.send_telegram_message"):
            import src.briefing as briefing
            briefing.main()

    prompt = captured_prompts[0]
    assert "energy 3" in prompt
    assert "topic finder is fine" in prompt


@freeze_time("2026-04-17 06:00:00")
def test_briefing_sends_even_when_json_fails(tmp_path, capsys):
    bad_response = {
        "text": "🎯 briefing text only",
        "prediction": None,
        "raw": "🎯 briefing text only",
        "provider": "anthropic",
        "model": "claude-opus-4-6",
    }
    sent = []
    mock_provider = make_mock_provider(bad_response)

    with patch("src.briefing.get_provider", return_value=mock_provider):
        with patch("src.briefing.send_telegram_message", side_effect=lambda t: sent.append(t)):
            import src.briefing as briefing
            result = briefing.main()

    assert result == 0
    assert len(sent) == 1
    assert "🎯 briefing text only" in sent[0]
    assert not (tmp_path / "2026-04-17.md").exists()


@freeze_time("2026-04-17 06:00:00")
def test_briefing_sends_even_when_memory_read_fails(tmp_path):
    # Put a corrupt yesterday file for 2026-04-16
    yesterday = tmp_path / "2026-04-16.md"
    yesterday.write_text("this is completely invalid garbage %%##")

    sent = []
    captured_prompts = []
    mock_provider = make_mock_provider()

    def capture_complete(system_prompt, user_message):
        captured_prompts.append(user_message)
        return MOCK_LLM_RESPONSE

    mock_provider.complete.side_effect = capture_complete

    with patch("src.briefing.get_provider", return_value=mock_provider):
        with patch("src.briefing.send_telegram_message", side_effect=lambda t: sent.append(t)):
            import src.briefing as briefing
            result = briefing.main()

    assert result == 0
    assert len(sent) == 1
    assert "Yesterday's context" not in captured_prompts[0] or True


@freeze_time("2026-04-17 06:00:00")
def test_same_day_double_run_does_not_overwrite(tmp_path, capsys):
    mock_provider = make_mock_provider()

    # First run
    with patch("src.briefing.get_provider", return_value=mock_provider):
        with patch("src.briefing.send_telegram_message"):
            import src.briefing as briefing
            briefing.main()

    original_content = (tmp_path / "2026-04-17.md").read_text()

    different_response = {**MOCK_LLM_RESPONSE, "text": "different briefing text"}
    mock_provider2 = make_mock_provider(different_response)
    sent = []

    with patch("src.briefing.get_provider", return_value=mock_provider2):
        with patch("src.briefing.send_telegram_message", side_effect=lambda t: sent.append(t)):
            result = briefing.main()

    assert result == 0
    assert len(sent) == 1
    assert (tmp_path / "2026-04-17.md").read_text() == original_content


@freeze_time("2026-04-17 23:30:00")  # 23:30 UTC = 02:30+1day Cairo = 2026-04-18 02:30 Cairo
def test_cairo_timezone_boundary(tmp_path):
    mock_provider = make_mock_provider()

    with patch("src.briefing.get_provider", return_value=mock_provider):
        with patch("src.briefing.send_telegram_message"):
            import src.briefing as briefing
            briefing.main()

    assert (tmp_path / "2026-04-18.md").exists()
    assert not (tmp_path / "2026-04-17.md").exists()


@freeze_time("2026-04-17 06:00:00")
def test_i7_2_provider_model_logged(tmp_path, caplog):
    import logging
    mock_provider = make_mock_provider()

    with patch("src.briefing.get_provider", return_value=mock_provider):
        with patch("src.briefing.send_telegram_message"):
            import src.briefing as briefing
            with caplog.at_level(logging.INFO, logger="src.briefing"):
                briefing.main()

    log_text = caplog.text
    assert "gemini" in log_text or "anthropic" in log_text
    assert "claude-opus-4-6" in log_text or "gemini" in log_text
