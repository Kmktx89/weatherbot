"""Notifier factory. `get_notifier()` selects the transport from the NOTIFIER
env switch (telegram | pushover). Unknown/unset -> pushover (safe rollback)."""
import wb_config
from notifiers.base import Notifier
from notifiers.pushover import PushoverNotifier
from notifiers.telegram import TelegramNotifier

__all__ = ["Notifier", "PushoverNotifier", "TelegramNotifier", "get_notifier"]


def get_notifier(name: str | None = None) -> Notifier:
    name = (name or wb_config.notifier_name()).lower()
    if name == "telegram":
        return TelegramNotifier()
    return PushoverNotifier()   # default + fallback for any unknown value
