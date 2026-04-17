import os
import httpx


def send_telegram_message(text: str) -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = httpx.post(url, json={"chat_id": chat_id, "text": text})
    response.raise_for_status()
