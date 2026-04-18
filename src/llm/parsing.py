"""Shared JSON block extraction for all providers."""
import json
import re


def extract_last_json_block(text: str) -> tuple[dict | None, str]:
    """
    Find the LAST ```json ... ``` fenced block in text.
    Returns (parsed_dict_or_None, text_before_last_block).
    Regex: backtick-backtick-backtick json \\s*\\n(.*?)\\n backtick-backtick-backtick with re.DOTALL, take last match.
    If no block found: returns (None, text).
    If block found but invalid JSON: returns (None, text_before_last_block).
    """
    blocks = list(re.finditer(r"```json\s*\n(.*?)\n```", text, re.DOTALL))
    if not blocks:
        return None, text
    last_block = blocks[-1]
    text_before = text[: last_block.start()].strip()
    try:
        prediction = json.loads(last_block.group(1))
    except json.JSONDecodeError:
        return None, text_before
    return prediction, text_before
