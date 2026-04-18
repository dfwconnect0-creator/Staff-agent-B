"""Tests for src/llm/parsing.py — JSON block extraction."""
import pytest
from src.llm.parsing import extract_last_json_block


def test_p1_1_single_json_block_extracted():
    text = 'some briefing\n\n```json\n{"a": 1}\n```'
    parsed, before = extract_last_json_block(text)
    assert parsed == {"a": 1}
    assert before == "some briefing"


def test_p1_2_multiple_blocks_last_wins():
    text = (
        'first block\n\n```json\n{"x": 1}\n```\n\n'
        'second block\n\n```json\n{"y": 2}\n```'
    )
    parsed, before = extract_last_json_block(text)
    assert parsed == {"y": 2}
    # text before last block includes everything up to the last ```json
    assert "first block" in before
    assert "second block" in before
    assert '{"y": 2}' not in before


def test_p1_3_no_json_block():
    text = "just briefing, no json"
    parsed, before = extract_last_json_block(text)
    assert parsed is None
    assert before == "just briefing, no json"


def test_p1_4_malformed_json_in_last_block():
    text = "briefing\n\n```json\n{broken\n```"
    parsed, before = extract_last_json_block(text)
    assert parsed is None
    assert before == "briefing"


def test_p1_5_multiline_string_content():
    text = '```json\n{"key": "line one\\nline two"}\n```'
    parsed, before = extract_last_json_block(text)
    assert parsed == {"key": "line one\nline two"}
