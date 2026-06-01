"""Shared lightweight config — loads .env (no python-dotenv dependency) and reads
the notifier / Telegram settings.

.env is gitignored and created by the operator (see docs/TELEGRAM_SETUP.md and
env.example). Real process environment variables take precedence over the .env
file (we use os.environ.setdefault), so a scheduled task or shell export wins.
"""
import os
from pathlib import Path

_loaded = False


def load_env(path: str = ".env") -> None:
    """Load KEY=VALUE lines from .env into os.environ (without overriding values
    already set in the real environment). Idempotent; safe if .env is absent."""
    global _loaded
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            os.environ.setdefault(k.strip(), v)
    _loaded = True


def get(key: str, default=None):
    if not _loaded:
        load_env()
    return os.environ.get(key, default)


def notifier_name() -> str:
    """Which transport scheduled pushes use: 'telegram' or 'pushover'.
    Defaults to 'pushover' so an unset/typo'd value is a safe no-change."""
    return (get("NOTIFIER", "pushover") or "pushover").strip().lower()
