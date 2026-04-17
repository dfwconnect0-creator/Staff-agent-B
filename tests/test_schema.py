import pytest
from src.memory.schema import PREDICTION_SCHEMA, validate_prediction

VALID_PREDICTION = {
    "schema_version": 1,
    "stuck_item": "Topic finder GitHub Actions step",
    "smallest_action": "Open last workflow run in GitHub",
    "confidence": "medium",
    "flags_raised": ["last_10_percent"],
    "questions_asked": ["Did you check the workflow run?", "Energy level 1-5?"],
}


def test_valid_prediction_passes():
    valid, err = validate_prediction(VALID_PREDICTION)
    assert valid is True
    assert err is None


def test_missing_required_field_fails():
    data = {k: v for k, v in VALID_PREDICTION.items() if k != "stuck_item"}
    valid, err = validate_prediction(data)
    assert valid is False
    assert "stuck_item" in err


def test_wrong_confidence_value_fails():
    data = {**VALID_PREDICTION, "confidence": "super_high"}
    valid, err = validate_prediction(data)
    assert valid is False
    assert "confidence" in err


def test_extra_unknown_field_fails():
    data = {**VALID_PREDICTION, "llm_opinion": "great idea"}
    valid, err = validate_prediction(data)
    assert valid is False


def test_too_many_flags_fails():
    data = {**VALID_PREDICTION, "flags_raised": [f"flag_{i}" for i in range(15)]}
    valid, err = validate_prediction(data)
    assert valid is False


def test_empty_questions_asked_fails():
    data = {**VALID_PREDICTION, "questions_asked": []}
    valid, err = validate_prediction(data)
    assert valid is False
