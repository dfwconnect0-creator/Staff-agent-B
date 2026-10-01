import os
import sys
from pathlib import Path

import pytest

# Add src to path for src/briefing.py's internal imports
sys.path.insert(0, str(Path(__file__).parent / "src"))

import src.state as state  # noqa: E402


@pytest.fixture(autouse=True)
def set_test_env(monkeypatch):
    """Set required env vars for all tests unless already set."""
    if "ANTHROPIC_API_KEY" not in os.environ:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-fake-key")
    if "TELEGRAM_BOT_TOKEN" not in os.environ:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-fake-token")
    if "TELEGRAM_CHAT_ID" not in os.environ:
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")


@pytest.fixture(autouse=True)
def tmp_state_path(tmp_path, monkeypatch):
    """Redirect current_state.md writes into tmp_path, seeded from the real file.

    Same pattern as the memory tests' episodic.MEMORY_DIR patch: tests must never
    mutate the repo's context/current_state.md.
    """
    seed = Path(__file__).parent / "context" / "current_state.md"
    target = tmp_path / "current_state.md"
    if seed.exists():
        target.write_text(seed.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(state, "DEFAULT_STATE_PATH", target)
    return target
