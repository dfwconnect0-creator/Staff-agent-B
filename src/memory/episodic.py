import json
import re
from datetime import date
from pathlib import Path

MEMORY_DIR: Path = Path(__file__).parent.parent.parent / "memory" / "episodic"


def path_for(day: date) -> Path:
    return MEMORY_DIR / f"{day.isoformat()}.md"


def _parse_briefing_text(content: str) -> str:
    match = re.search(r"## Briefing sent\n\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not match:
        return ""
    return match.group(1).strip()


def _parse_prediction(content: str) -> dict | None:
    blocks = re.findall(r"```json\s*\n(.*?)\n```", content, re.DOTALL)
    if not blocks:
        return None
    try:
        return json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None


def _parse_user_replies(content: str) -> list[dict]:
    match = re.search(r"## User replies.*?\n\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not match:
        return []
    replies = []
    for line in match.group(1).strip().splitlines():
        m = re.match(r'- \*\*(.+?) Cairo:\*\* "(.*)"', line)
        if m:
            replies.append({"timestamp": m.group(1), "text": m.group(2)})
    return replies


def _parse_outcome(content: str) -> str | None:
    match = re.search(r"## Outcome.*?\n\n(.*?)(?=\n## |\Z)", content, re.DOTALL)
    if not match:
        return None
    text = match.group(1).strip()
    return text if text and text != "_" else None


def read_day(day: date) -> dict | None:
    p = path_for(day)
    if not p.exists():
        return None
    try:
        content = p.read_text(encoding="utf-8")
        return {
            "date": day.isoformat(),
            "briefing_text": _parse_briefing_text(content),
            "prediction": _parse_prediction(content),
            "user_replies": _parse_user_replies(content),
            "outcome": _parse_outcome(content),
        }
    except Exception:
        return {
            "date": day.isoformat(),
            "briefing_text": "",
            "prediction": None,
            "user_replies": [],
            "outcome": None,
        }


def write_day(day: date, briefing_text: str, prediction: dict) -> Path:
    p = path_for(day)
    if p.exists():
        raise FileExistsError(f"{p} already exists")
    day_name = day.strftime("%A")
    prediction_json = json.dumps(prediction, indent=2, ensure_ascii=False)
    content = f"""# {day.isoformat()} ({day_name})

## Briefing sent

{briefing_text}

## Prediction (agent-generated)

```json
{prediction_json}
```
"""
    p.write_text(content, encoding="utf-8")
    return p


def append_reply(day: date, timestamp_cairo: str, text: str) -> None:
    p = path_for(day)
    if not p.exists():
        raise FileNotFoundError(f"No day file for {day.isoformat()}")
    bullet = f'- **{timestamp_cairo} Cairo:** "{text}"'
    content = p.read_text(encoding="utf-8")
    if bullet in content:
        return
    if "## User replies" not in content:
        content = content.rstrip("\n") + "\n\n## User replies\n\n"
    content = content.rstrip("\n") + f"\n{bullet}\n"
    p.write_text(content, encoding="utf-8")
