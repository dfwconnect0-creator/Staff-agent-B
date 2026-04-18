"""Tests for src/llm/retry.py — one-shot retry behavior."""
import pytest
import httpx
from unittest.mock import MagicMock, call
from src.llm.retry import with_json_retry, RETRY_REMINDER


def make_response(prediction=None, raw="some text"):
    return {
        "text": raw if prediction is None else "briefing",
        "prediction": prediction,
        "raw": raw,
        "provider": "test",
        "model": "test-model",
    }


def test_r2_1_first_call_succeeds_no_retry():
    valid_response = make_response(prediction={"key": "val"})
    fn = MagicMock(return_value=valid_response)

    result = with_json_retry(fn, "sys", "user msg")

    fn.assert_called_once()
    assert result == valid_response


def test_r2_2_first_fails_retry_succeeds():
    bad = make_response(prediction=None, raw="bad")
    good = make_response(prediction={"key": "val"}, raw="good")
    fn = MagicMock(side_effect=[bad, good])

    result = with_json_retry(fn, "sys", "user msg")

    assert fn.call_count == 2
    assert result == good


def test_r2_3_retry_messages_include_reminder():
    bad = make_response(prediction=None, raw="assistant bad reply")
    good = make_response(prediction={"key": "val"})
    fn = MagicMock(side_effect=[bad, good])

    with_json_retry(fn, "sys", "user msg")

    # first call: messages=[{"role":"user","content":"user msg"}]
    first_call_messages = fn.call_args_list[0][0][1]
    assert first_call_messages == [{"role": "user", "content": "user msg"}]

    # second call: 3 messages — original user, assistant reply, RETRY_REMINDER
    second_call_messages = fn.call_args_list[1][0][1]
    assert len(second_call_messages) == 3
    assert second_call_messages[0] == {"role": "user", "content": "user msg"}
    assert second_call_messages[1] == {"role": "assistant", "content": "assistant bad reply"}
    assert second_call_messages[2] == {"role": "user", "content": RETRY_REMINDER}


def test_r2_4_both_calls_fail():
    bad = make_response(prediction=None, raw="bad")
    fn = MagicMock(side_effect=[bad, bad])

    result = with_json_retry(fn, "sys", "user msg")

    assert fn.call_count == 2
    assert result["prediction"] is None


def test_r2_5_exception_propagates():
    fn = MagicMock(side_effect=httpx.HTTPError("connection failed"))

    with pytest.raises(httpx.HTTPError):
        with_json_retry(fn, "sys", "user msg")

    fn.assert_called_once()
