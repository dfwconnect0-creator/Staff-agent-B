import os
from datetime import datetime, timezone, timedelta

import httpx

CAIRO_TZ = timezone(timedelta(hours=3))


def send_telegram_message(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = httpx.post(url, json={"chat_id": chat_id, "text": text})
    response.raise_for_status()


def get_updates(since_update_id: int = 0) -> list[dict]:
    """
    Call Telegram getUpdates API. Return list of message dicts:
    [{"update_id": int, "timestamp_cairo": str, "text": str, "chat_id": int}, ...]
    Filters to only messages from TELEGRAM_CHAT_ID.
    """
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = int(os.environ["TELEGRAM_CHAT_ID"])
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"offset": since_update_id + 1} if since_update_id > 0 else {}
    response = httpx.get(url, params=params)
    response.raise_for_status()
    data = response.json()
    updates = []
    for item in data.get("result", []):
        msg = item.get("message") or item.get("edited_message")
        if not msg:
            continue
        if msg.get("chat", {}).get("id") != chat_id:
            continue
        if "text" not in msg:
            continue
        ts_unix = msg["date"]
        dt_cairo = datetime.fromtimestamp(ts_unix, tz=CAIRO_TZ)
        timestamp_cairo = dt_cairo.strftime("%Y-%m-%d %H:%M")
        updates.append({
            "update_id": item["update_id"],
            "timestamp_cairo": timestamp_cairo,
            "text": msg["text"],
            "chat_id": msg["chat"]["id"],
        })
    return updates
