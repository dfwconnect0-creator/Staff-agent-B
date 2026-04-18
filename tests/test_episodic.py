import json
import pytest
from datetime import date
from freezegun import freeze_time

import src.memory.episodic as episodic

VALID_PREDICTION = {
    "schema_version": 1,
    "stuck_item": "Topic finder GitHub Actions step",
    "smallest_action": "Open last workflow run in GitHub",
    "confidence": "medium",
    "flags_raised": ["last_10_percent"],
    "questions_asked": ["Did you check the workflow run?", "Energy level 1-5?"],
}


@pytest.fixture(autouse=True)
def tmp_memory_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(episodic, "MEMORY_DIR", tmp_path)
    return tmp_path


def test_path_for_generates_correct_filename(tmp_path):
    d = date(2026, 4, 17)
    result = episodic.path_for(d)
    assert result == tmp_path / "2026-04-17.md"


def test_read_day_returns_none_for_missing_file():
    result = episodic.read_day(date(2026, 4, 17))
    assert result is None


def test_write_day_creates_file_with_correct_structure(tmp_path):
    d = date(2026, 4, 17)
    episodic.write_day(d, "🎯 briefing text here", VALID_PREDICTION)
    p = tmp_path / "2026-04-17.md"
    assert p.exists()
    content = p.read_text()
    assert "# 2026-04-17 (Friday)" in content
    assert "## Briefing sent" in content
    assert "🎯 briefing text here" in content
    assert "## Prediction (agent-generated)" in content
    assert '"stuck_item"' in content
    parsed_json = json.loads(content.split("```json")[1].split("```")[0].strip())
    assert parsed_json == VALID_PREDICTION


def test_write_day_on_sunday_writes_sunday_in_header(tmp_path):
    d = date(2026, 4, 19)
    episodic.write_day(d, "briefing", VALID_PREDICTION)
    content = (tmp_path / "2026-04-19.md").read_text()
    assert "# 2026-04-19 (Sunday)" in content


def test_write_day_refuses_to_overwrite(tmp_path):
    d = date(2026, 4, 17)
    episodic.write_day(d, "first", VALID_PREDICTION)
    original = (tmp_path / "2026-04-17.md").read_text()
    with pytest.raises(FileExistsError):
        episodic.write_day(d, "second", VALID_PREDICTION)
    assert (tmp_path / "2026-04-17.md").read_text() == original


def test_read_day_round_trips_write_day(tmp_path):
    d = date(2026, 4, 17)
    episodic.write_day(d, "hello world", VALID_PREDICTION)
    result = episodic.read_day(d)
    assert result is not None
    assert result["briefing_text"] == "hello world"
    assert result["prediction"] == VALID_PREDICTION


def test_read_day_handles_missing_prediction_json(tmp_path):
    p = tmp_path / "2026-04-17.md"
    p.write_text("# 2026-04-17 (Friday)\n\n## Briefing sent\n\nsome text\n")
    result = episodic.read_day(date(2026, 4, 17))
    assert result is not None
    assert result["briefing_text"] == "some text"
    assert result["prediction"] is None


def test_read_day_handles_malformed_json(tmp_path):
    p = tmp_path / "2026-04-17.md"
    p.write_text("# 2026-04-17\n\n## Briefing sent\n\ntext\n\n## Prediction (agent-generated)\n\n```json\n{broken json\n```\n")
    result = episodic.read_day(date(2026, 4, 17))
    assert result is not None
    assert result["prediction"] is None


def test_append_reply_adds_to_existing_file(tmp_path):
    d = date(2026, 4, 17)
    episodic.write_day(d, "briefing", VALID_PREDICTION)
    episodic.append_reply(d, "2026-04-17 09:12", "yo")
    content = (tmp_path / "2026-04-17.md").read_text()
    assert "## User replies" in content
    assert '- **2026-04-17 09:12 Cairo:** "yo"' in content


def test_append_reply_is_idempotent(tmp_path):
    d = date(2026, 4, 17)
    episodic.write_day(d, "briefing", VALID_PREDICTION)
    episodic.append_reply(d, "2026-04-17 09:12", "yo")
    content_after_first = (tmp_path / "2026-04-17.md").read_text()
    episodic.append_reply(d, "2026-04-17 09:12", "yo")
    content_after_second = (tmp_path / "2026-04-17.md").read_text()
    assert content_after_first == content_after_second


def test_append_reply_preserves_other_sections(tmp_path):
    d = date(2026, 4, 17)
    episodic.write_day(d, "briefing text", VALID_PREDICTION)
    outcome_section = "\n## Outcome (optional, user-editable)\n\n_Good prediction._\n"
    p = tmp_path / "2026-04-17.md"
    p.write_text(p.read_text() + outcome_section)
    episodic.append_reply(d, "2026-04-17 09:12", "reply text")
    content = p.read_text()
    assert "briefing text" in content
    assert '"stuck_item"' in content
    assert "_Good prediction._" in content
    assert '- **2026-04-17 09:12 Cairo:** "reply text"' in content


def test_append_reply_raises_on_missing_file():
    with pytest.raises(FileNotFoundError):
        episodic.append_reply(date(2026, 4, 17), "2026-04-17 09:12", "yo")


def test_write_day_file_has_closing_json_fence(tmp_path):
    d = date(2026, 4, 17)
    episodic.write_day(d, "briefing", VALID_PREDICTION)
    content = (tmp_path / "2026-04-17.md").read_text()
    # The prediction block must be a valid closed markdown code fence
    assert content.count("```") == 2  # one open, one close
    assert "```\n" in content.split("```json")[1]  # closing fence exists
