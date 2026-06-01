# weatherbot operations runbook

Host/runtime operations for the live weatherbot stack. NOT model logic — model
changes go in `MODEL_CHANGES.md` and through the gated pipeline. Module inventory:
`weatherbot_module_registry.md`.

Host: `kmkFramework` (Windows 11), repo at `C:\Users\KrisKnecht\weatherbot`.

---

## Keep-alive — REQUIRED for 24/7 (Modern Standby)

The laptop has **only Modern Standby (S0 Low Power Idle); no S3** (`powercfg /a`).
On lid-close, and after the idle sleep timeout, S0 standby **suspends every
weatherbot process** — Telegram bot, dashboard `serve`, and the snapshot / signals
/ health scheduled tasks — so closing the lid silently stops the bot and the model.

**Applied 2026-06-01 (Balanced scheme, both AC and DC):** lid-close = *Do nothing*,
sleep = *never*. The machine no longer sleeps on lid-close or idle on either power
source, so the stack runs with the lid shut, plugged in or on battery.

Set (re-apply after any power-plan reset / OS reinstall — this is host config, not
enforced by the repo):
```
set L=5ca83367-6e45-459f-a27b-476b1d01c936
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS %L% 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS %L% 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0
powercfg /setactive SCHEME_CURRENT
```
Verify: `powercfg /q SCHEME_CURRENT SUB_BUTTONS %L%` and `... SUB_SLEEP STANDBYIDLE`
should show AC+DC Index `0x0`. (Lid-close setting is hidden by default; unhide with
`powercfg /attributes SUB_BUTTONS %L% -ATTRIB_HIDE`.)

**Tradeoff:** closed + awake on battery has no airflow → runs warm (don't seal it in
a bag for long) and drains; an unplugged closed laptop will eventually deplete and
power off (tasks auto-restart on next logon). Revert battery to sleeping:
```
powercfg /setdcvalueindex SCHEME_CURRENT SUB_BUTTONS 5ca83367-6e45-459f-a27b-476b1d01c936 1
powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 600
powercfg /setactive SCHEME_CURRENT
```

---

## Scheduled tasks (run at logon; see registry for detail)

| Task | Runs | Notes |
|------|------|-------|
| WeatherbotDashboard | `run_dashboard.bat` → `kalshi_temp serve` (HTTPS 8765) | supervised restart loop |
| WeatherbotSnapshot | `snapshot.py` hourly | live calibration log + T-24 alerts |
| WeatherbotSignals | `hourly_signals.py` hourly | `hourly_signals.json` |
| WeatherbotHealth | `lab.cli health --write` daily | `docs/MODEL_HEALTH.md` |
| WeatherbotTelegram | `run_telegram_bot.bat` → `telegram_bot.py` (long-poll) | WB-006 two-way bot |

Manage: `Get-/Start-/Stop-/Disable-ScheduledTask <name>` (PowerShell).

---

## Telegram (WB-006)

- Transport switch: `NOTIFIER` in `.env` (`telegram` | `pushover`; default pushover).
- **One long-poller per bot token only** — do not run `python telegram_bot.py` by
  hand while `WeatherbotTelegram` is active (409 Conflict). One-off sends:
  `python telegram_bot.py push <status|report|health|predict lax>` (non-polling).
- `.env` is gitignored / operator-created (`cp env.example .env`); see
  `docs/TELEGRAM_SETUP.md`.

## Rollback levers

- Notifications → Pushover: set `NOTIFIER=pushover` in `.env`.
- Stop the bot: `Disable-ScheduledTask WeatherbotTelegram`.
- Restore default sleep: the revert `powercfg` block above.
