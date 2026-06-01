"""Notifier interface — the single contract scheduled pushes go through, so the
transport (Pushover vs Telegram) is swappable via the NOTIFIER env switch."""
from abc import ABC, abstractmethod


class Notifier(ABC):
    name = "base"

    @abstractmethod
    def available(self) -> bool:
        """True if this transport is configured (creds present). Callers skip
        sending — rather than raising — when a transport is unavailable."""

    @abstractmethod
    def send(self, title: str, message: str) -> tuple[bool, str]:
        """Send one notification. Returns (ok, info). Never raises for normal
        transport failures — returns (False, reason)."""
