"""Telegram transport for scheduled pushes.

Uses a plain HTTPS POST to the Bot API sendMessage endpoint — deliberately NOT
python-telegram-bot. The push path runs inside synchronous scheduled tasks
(snapshot.py, generate_t24_card.py); a bare requests.post avoids dragging an
asyncio event loop into that context. python-telegram-bot is used only by the
long-polling command server (telegram_bot.py).

Credentials come from .env via wb_config: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""
import requests

import wb_config
from notifiers.base import Notifier

SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(self):
        self.token = wb_config.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = wb_config.get("TELEGRAM_CHAT_ID")

    def available(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, title: str, message: str, *, timeout: float = 10.0) -> tuple[bool, str]:
        if not self.available():
            return False, "missing TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
        text = f"{title}\n\n{message}" if title else message
        try:
            r = requests.post(
                SEND_URL.format(token=self.token),
                data={"chat_id": self.chat_id, "text": text,
                      "disable_web_page_preview": True},
                timeout=timeout,
            )
            return r.status_code == 200, r.text
        except Exception as e:   # transport failure -> readable, never raise
            return False, str(e)
