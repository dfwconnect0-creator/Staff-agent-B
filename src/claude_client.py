import json
import os
import re

import anthropic


def _extract_last_json_block(text: str) -> tuple[dict | None, str]:
    """
    Extract the last ```json ... ``` block from text.
    Returns (parsed_dict_or_None, text_before_last_block).
    """
    blocks = list(re.finditer(r"```json\s*\n(.*?)\n```", text, re.DOTALL))
    if not blocks:
        return None, text
    last_block = blocks[-1]
    try:
        prediction = json.loads(last_block.group(1))
    except json.JSONDecodeError:
        prediction = None
    text_before = text[: last_block.start()].strip()
    return prediction, text_before


def ask_claude(system_prompt: str, user_message: str) -> dict:
    """
    Returns:
    {
        "text": str,           # the briefing for Telegram
        "prediction": dict | None,  # parsed JSON, None if parsing failed
        "raw": str,            # full model output, for debugging
    }
    """
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    message = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    raw = message.content[0].text
    prediction, briefing_text = _extract_last_json_block(raw)
    return {
        "text": briefing_text,
        "prediction": prediction,
        "raw": raw,
    }
