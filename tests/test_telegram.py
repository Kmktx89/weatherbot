"""WB-006 Telegram transport + bot. Pure command handlers are tested directly
(the "answers /status against a stub token" gate); the bot builds with a stub
token without connecting; the NOTIFIER switch routes correctly."""
import json
import pytest

import telegram_commands as cmds


# ---------- city resolution ----------

def test_resolve_city_aliases_and_tickers():
    assert cmds.resolve_city("lax") == "KXHIGHLAX"
    assert cmds.resolve_city("Los Angeles") == "KXHIGHLAX"
    assert cmds.resolve_city("NYC") == "KXHIGHNY"
    assert cmds.resolve_city("KXHIGHCHI") == "KXHIGHCHI"
    assert cmds.resolve_city("atlantis") is None
    assert cmds.resolve_city("") is None


# ---------- pure handlers (no token, no network) ----------

def test_cmd_status_reads_artifacts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hourly_signals.json").write_text(json.dumps({
        "generated_at": "2026-05-31T14:00:10-05:00", "n_open_events": 12,
        "picks": [{"series": "KXHIGHDEN"}]}), encoding="utf-8")
    (tmp_path / "live_picks_log.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "MODEL_HEALTH.md").write_text(
        "# Model Health\n\n_Generated 2026-06-01T04:05:02+00:00 · window 14d._\n",
        encoding="utf-8")
    out = cmds.cmd_status()
    assert "weatherbot status" in out
    assert "notifier:" not in out
    assert "12 open events" in out
    assert "2026-06-01" in out          # health doc timestamp surfaced


def test_cmd_health_extracts_flags(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "MODEL_HEALTH.md").write_text(
        "# Model Health\n\n_Generated 2026-06-01T04:05:02+00:00._\n\n"
        "## Flags\n- **ALERT** dispersion_k_KXHIGHLAX = 0.4\n- **WATCH** bias\n\n"
        "## Opportunities\n- none\n", encoding="utf-8")
    out = cmds.cmd_health()
    assert "ALERT" in out and "dispersion_k_KXHIGHLAX" in out
    assert "Opportunities" not in out   # stops at the next section
    assert "2026-06-01" in out


def test_cmd_health_missing_file_is_readable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert "model_health.md" in cmds.cmd_health().lower()


def test_cmd_report_formats_picks(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "hourly_signals.json").write_text(json.dumps({
        "generated_at": "2026-05-31T14:00:10-05:00", "n_open_events": 12,
        "picks": [{"series": "KXHIGHDEN", "city": "Denver", "target_date": "2026-05-31",
                   "side": "YES", "bucket": "83 to 84", "printed_prob": 0.174,
                   "market_price": 0.07, "ev": 0.1039, "size_pct": 18.6,
                   "lead_hours": 12.0}]}), encoding="utf-8")
    out = cmds.cmd_report()
    assert "DEN" in out and "YES" in out and "83 to 84" in out
    assert "1 qualifying pick" in out


def test_cmd_predict_unknown_city_is_readable():
    assert "unknown city" in cmds.cmd_predict("atlantis").lower()
    assert "usage" in cmds.cmd_predict("").lower()


def test_cmd_predict_network_error_is_readable(monkeypatch):
    import kalshi_temp as kt
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(kt, "get_dashboard_data", boom)
    out = cmds.cmd_predict("lax")
    assert "couldn't fetch live data" in out.lower()
    assert "Traceback" not in out      # readable message, not a stack trace


def test_cmd_predict_formats_live_event(monkeypatch):
    import kalshi_temp as kt
    fake = {"events": [{
        "event_ticker": "KXHIGHLAX-26JUN01", "target_date": "2026-06-01",
        "station": "Los Angeles International (KLAX)", "settled": False,
        "model": {"mu": 75.2, "sigma": 1.0, "sources": 3},
        "markets": [{"ticker": "T", "subtitle": "75 to 76", "prob": 0.5,
                     "yes_ask": 0.40, "no_ask": 0.55,
                     "cal_ev_yes": 0.10, "cal_ev_no": -0.05,
                     "cal_prob_yes": 0.5, "cal_prob_no": 0.5}]}]}
    monkeypatch.setattr(kt, "get_dashboard_data", lambda: fake)
    out = cmds.cmd_predict("lax")
    assert "KXHIGHLAX-26JUN01" in out
    assert "mu75.2" in out
    assert "best YES" in out and "best NO" in out


# ---------- NOTIFIER switch ----------

def test_get_notifier_is_telegram():
    from notifiers import get_notifier, TelegramNotifier
    assert isinstance(get_notifier(), TelegramNotifier)
    assert isinstance(get_notifier("telegram"), TelegramNotifier)


def test_get_notifier_pushover_name_now_telegram():
    # Pushover removed: even the legacy name resolves to Telegram.
    from notifiers import get_notifier, TelegramNotifier
    assert isinstance(get_notifier("pushover"), TelegramNotifier)


class _RecordingNotifier:
    name = "recording"

    def __init__(self):
        self.sent = []

    def available(self):
        return True

    def send(self, title, message):
        self.sent.append((title, message))
        return True, "ok"


def test_send_t24_alerts_routes_through_notifier(monkeypatch, tmp_path):
    """Integration: the rerouted alert path builds the message and sends via
    get_notifier() (not a hardcoded Pushover call), and dedup still works."""
    monkeypatch.chdir(tmp_path)          # isolate alerts_sent_tickers.txt
    import notifiers
    import alerts
    rec = _RecordingNotifier()
    monkeypatch.setattr(notifiers, "get_notifier", lambda *a, **k: rec)
    row = {"event_ticker": "KXHIGHNY-26JUN01", "series": "KXHIGHNY",
           "target_date": "2026-06-01", "lead_hours": 24.0, "settled": False,
           "model": {"mu": 70.0, "sigma": 1.0}, "buckets": []}
    n = alerts.send_t24_alerts([row])
    assert n == 1
    assert len(rec.sent) == 1
    title, body = rec.sent[0]
    assert "T-24h" in title and "NY" in body
    # dedup: same ticker does not fire again
    assert alerts.send_t24_alerts([row]) == 0
    assert len(rec.sent) == 1


def test_notify_paths_no_crash_when_unavailable(monkeypatch, tmp_path):
    """Both rewritten notify paths no-op cleanly when the Telegram transport is
    unavailable (no .env creds here) — no raise, no cycle."""
    monkeypatch.chdir(tmp_path)
    import alerts
    import generate_t24_card as g
    assert alerts.send_t24_alerts([]) == 0
    g.maybe_notify(None, [], "2026-06-01")        # must not raise
    g.maybe_notify(None, [], "2026-06-01", blocked=True)
    g.push_catchup([], "2026-06-01")              # must not raise


def test_telegram_notifier_unavailable_without_creds(monkeypatch):
    import wb_config
    monkeypatch.setattr(wb_config, "get", lambda k, d=None: None)
    from notifiers.telegram import TelegramNotifier
    n = TelegramNotifier()
    assert n.available() is False
    ok, info = n.send("t", "m")
    assert ok is False and "missing" in info.lower()    # never raises


# ---------- bot builds with a stub token (no connection) ----------

def test_bot_builds_with_stub_token():
    pytest.importorskip("telegram")     # python-telegram-bot
    import telegram_bot
    app = telegram_bot.build_application("123456789:STUB-TOKEN-not-real-aaaaaaaaaaaaa")
    # handlers registered; no network call was made by build_application
    handlers = app.handlers.get(0, [])
    assert len(handlers) >= 5


def test_status_handler_answers_with_stub_token():
    """The explicit gate: built with a stub token (no live connection), the bot
    answers /status. Invoke the registered handler with a fake Update/Context and
    capture the reply — proves transport->pure-function wiring end to end."""
    pytest.importorskip("telegram")
    import asyncio
    import telegram_bot
    app = telegram_bot.build_application("123456789:STUB-TOKEN-not-real-aaaaaaaaaaaaa")
    status_h = next((h for h in app.handlers.get(0, [])
                     if "status" in (getattr(h, "commands", set()) or set())), None)
    assert status_h is not None, "no /status handler registered"

    captured = {}

    class FakeMsg:
        async def reply_text(self, text):
            captured["text"] = text

    class FakeUpdate:
        message = FakeMsg()

    class FakeCtx:
        args = []

    asyncio.run(status_h.callback(FakeUpdate(), FakeCtx()))
    assert "weatherbot status" in captured["text"]
    assert "notifier:" not in captured["text"]
