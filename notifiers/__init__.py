"""Notifier factory. Telegram is the sole transport; `get_notifier()` always
returns it. (Pushover was removed 2026-06-01 — it was non-functional.)"""
from notifiers.base import Notifier
from notifiers.telegram import TelegramNotifier

__all__ = ["Notifier", "TelegramNotifier", "get_notifier"]


def get_notifier(name: str | None = None) -> Notifier:
    """Return the notification transport. Single transport (Telegram); the
    optional `name` is accepted for call-site compatibility but ignored."""
    return TelegramNotifier()
