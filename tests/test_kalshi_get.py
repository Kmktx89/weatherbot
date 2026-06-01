"""Regression tests for kalshi_get's connection/DNS-error retry.

A transient DNS failure (NameResolutionError, a ConnectionError subclass)
used to propagate immediately because the retry loop only handled HTTP 429,
zeroing out a city's rows for the whole snapshot run and occasionally
blocking that day's T-24 card. These tests lock in the retry behaviour.
"""
import requests
import pytest

import kalshi_temp


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


def test_kalshi_get_retries_connection_error_then_succeeds(monkeypatch):
    """ConnectionError on the first two attempts, 200 on the third."""
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise requests.exceptions.ConnectionError("getaddrinfo failed")
        return _Resp({"ok": True})

    monkeypatch.setattr(kalshi_temp.session, "get", fake_get)
    monkeypatch.setattr(kalshi_temp.time, "sleep", lambda *_a, **_k: None)

    assert kalshi_temp.kalshi_get("/anything") == {"ok": True}
    assert calls["n"] == 3


def test_kalshi_get_retries_timeout_then_succeeds(monkeypatch):
    """Timeout is also treated as transient and retried."""
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 2:
            raise requests.exceptions.Timeout("read timed out")
        return _Resp({"ok": True})

    monkeypatch.setattr(kalshi_temp.session, "get", fake_get)
    monkeypatch.setattr(kalshi_temp.time, "sleep", lambda *_a, **_k: None)

    assert kalshi_temp.kalshi_get("/anything") == {"ok": True}
    assert calls["n"] == 2


def test_kalshi_get_raises_after_exhausting_retries(monkeypatch):
    """Every attempt fails -> the last ConnectionError propagates (no NameError
    from an unbound response, which the pre-fix code would have raised)."""
    def always_fail(url, params=None, timeout=None):
        raise requests.exceptions.ConnectionError("getaddrinfo failed")

    monkeypatch.setattr(kalshi_temp.session, "get", always_fail)
    monkeypatch.setattr(kalshi_temp.time, "sleep", lambda *_a, **_k: None)

    with pytest.raises(requests.exceptions.ConnectionError):
        kalshi_temp.kalshi_get("/anything")
