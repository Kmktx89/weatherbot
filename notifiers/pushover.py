"""Pushover transport — thin wrapper over the existing alerts.py functions so
the legacy path is byte-for-byte unchanged when NOTIFIER=pushover (rollback).

`alerts` is imported lazily inside the methods to avoid an import cycle
(alerts.py imports notifiers.get_notifier)."""
from notifiers.base import Notifier


class PushoverNotifier(Notifier):
    name = "pushover"

    def available(self) -> bool:
        import alerts
        return alerts.load_config() is not None

    def send(self, title: str, message: str) -> tuple[bool, str]:
        import alerts
        cfg = alerts.load_config()
        if cfg is None:
            return False, "no pushover_config.json"
        try:
            return alerts.send_pushover(title, message, cfg)
        except Exception as e:   # transport failure -> readable, never raise
            return False, str(e)
