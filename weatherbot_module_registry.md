# weatherbot module registry

Inventory of weatherbot's executable units (scheduled tasks, services, transports).
Created 2026-06-01 — WB-001…WB-005 seeded from the running system as observed;
WB-006 added this session. Check here before adding a net-new module; prefer
extending an existing one.

| ID | Module | Entry point | Trigger | Purpose |
|----|--------|-------------|---------|---------|
| WB-001 | Dashboard server | `kalshi_temp.py serve` (run_dashboard.bat) | WeatherbotDashboard task (logon, supervised) | Live HTTPS dashboard + `/api/markets`, `/api/backtest` |
| WB-002 | Hourly snapshotter | `snapshot.py` (snapshot_task.bat) | hourly task | Append live calibration rows to `live_picks_log.jsonl`; fire T-24 alerts |
| WB-003 | Model-health loop | `lab.cli health --write` (run_health.bat) | daily WeatherbotHealth task | Calibration/dispersion/bias scan → `docs/MODEL_HEALTH.md` (detection only) |
| WB-004 | Hourly signals report | `hourly_signals.py` (run_signals.bat) | hourly WeatherbotSignals task | Qualifying-signal report → `hourly_signals.json` |
| WB-005 | T-24 card / digest | `generate_t24_card.py` | daily + hourly catch-up | Daily picks digest + late-fill catch-up notifications |
| WB-006 | Notifier transport | `notifiers/` + `telegram_bot.py` (run_telegram_bot.bat) | imported by WB-002/WB-005; bot = WeatherbotTelegram task (logon, supervised) | Pluggable notify transport (Telegram \| Pushover) + two-way Telegram command bot |

---

## WB-006 — Notifier transport (Telegram + Pushover switch)

- **Added:** 2026-06-01. Merged to `model-lab` and live (Telegram bot running as a
  scheduled task; `NOTIFIER=telegram`).
- **What:** Replaces direct Pushover calls with a `Notifier` interface selected by
  the `NOTIFIER` env switch (`telegram` | `pushover`; default `pushover` = rollback).
  Adds a two-way long-polling Telegram bot.
- **Components:**
  - `wb_config.py` — `.env` loader (no new dependency) + `notifier_name()`.
  - `notifiers/` — `base.Notifier`, `pushover.PushoverNotifier` (wraps existing
    `alerts.load_config`/`send_pushover`), `telegram.TelegramNotifier`
    (requests-based HTTPS POST), `get_notifier()` factory.
  - `telegram_commands.py` — **pure** handlers (`cmd_predict`/`cmd_report`/
    `cmd_status`/`cmd_health`/`cmd_help`), args→text, no Telegram I/O, errors
    returned as readable text.
  - `telegram_bot.py` — long-polling transport (`python telegram_bot.py`) wiring
    PTB `CommandHandler`s to the pure functions; `push <cmd>` subcommand for
    scheduled pushes (`python telegram_bot.py push report`).
  - `run_telegram_bot.bat` — supervisor (restart loop, windowless `pythonw.exe`;
    mirrors `run_dashboard.bat`). Logs to `telegram_bot.log` (gitignored).
- **Runtime:** the **WeatherbotTelegram** scheduled task (logon trigger for
  `AzureAD\KrisKnecht`, `MultipleInstances=IgnoreNew`, `RestartCount=3`, unlimited
  runtime) runs `run_telegram_bot.bat`, so the two-way bot is up 24/7 and restarts
  on crash/reboot. Only ONE poller per token — do not also run `python
  telegram_bot.py` by hand while the task is active (409 Conflict). Manage with
  `Start-/Stop-/Disable-ScheduledTask WeatherbotTelegram`.
- **Wired into:** `alerts.send_t24_alerts` (WB-002) and `generate_t24_card`'s
  `maybe_pushover`/`push_catchup` (WB-005) — both now route through `get_notifier()`.
- **Config / secrets:** `.env` (gitignored, operator-created — deny-rule blocks
  Claude from writing `.env*`): `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `NOTIFIER`.
  Template: `env.example`. Setup: `docs/TELEGRAM_SETUP.md`.
- **Dependency:** `python-telegram-bot` (polling mode only); push mode is
  requests-only.
- **Rollback:** `NOTIFIER=pushover` → original `pushover_config.json` path unchanged;
  `Disable-ScheduledTask WeatherbotTelegram` to stop the bot.
