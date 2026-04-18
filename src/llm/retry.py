"""One-shot retry on malformed JSON."""
from __future__ import annotations
from typing import Callable

RETRY_REMINDER = (
    "The JSON block was missing or malformed. "
    "Return only a single fenced ```json block at the end of your reply, "
    "containing exactly these fields: schema_version, stuck_item, smallest_action, "
    "confidence, flags_raised, questions_asked."
)


def with_json_retry(complete_fn: Callable, system_prompt: str, user_message: str) -> dict:
    """
    Call complete_fn once. If prediction is None, call it a second time
    with an appended reminder message. Return whichever response is better —
    the retry if it succeeded, otherwise the original.

    complete_fn signature: (system_prompt: str, messages: list[dict]) -> LLMResponse
    """
    messages = [{"role": "user", "content": user_message}]
    result = complete_fn(system_prompt, messages)

    if result["prediction"] is not None:
        return result

    # Retry with reminder appended to conversation
    retry_messages = [
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": result["raw"]},
        {"role": "user", "content": RETRY_REMINDER},
    ]
    return complete_fn(system_prompt, retry_messages)
