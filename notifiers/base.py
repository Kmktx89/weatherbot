"""Notifier interface — the single contract scheduled pushes go through.
Telegram is the only transport (Pushover was removed 2026-06-01)."""
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
