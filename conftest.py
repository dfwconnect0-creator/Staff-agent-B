import os
import sys
from pathlib import Path

import pytest

# Add src to path for src/briefing.py's internal imports
sys.path.insert(0, str(Path(__file__).parent / "src"))


@pytest.fixture(autouse=True)
def set_test_env(monkeypatch):
    """Set required env vars for all tests unless already set."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key")
    if "TELEGRAM_BOT_TOKEN" not in os.environ:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-fake-token")
    if "TELEGRAM_CHAT_ID" not in os.environ:
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
