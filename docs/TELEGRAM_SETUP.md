# Telegram bot setup (WB-006)

Two-way Telegram transport replacing Pushover. Pushover stays in place behind the
`NOTIFIER` switch for rollback.

## 1. Create the bot + get credentials
1. In Telegram, message **@BotFather** → `/newbot` → follow prompts. Copy the
   **HTTP API token** (`123456789:AA…`).
2. Message your new bot once (say `/start`) so it has a chat with you.
3. Fetch your chat id:
   `https://api.telegram.org/bot<TOKEN>/getUpdates` → read
   `result[].message.chat.id`.

## 2. Create the `.env` file (MANUAL — Claude cannot write it)
The permission deny-rule blocks writing any `.env*` file, so create it by hand:

```bash
cp env.example .env       # then edit .env
```

Set in `.env`:
```
NOTIFIER=telegram
TELEGRAM_BOT_TOKEN=<token from BotFather>
TELEGRAM_CHAT_ID=<your chat id>
```
`.env` is gitignored. Real shell/scheduled-task env vars override `.env`.

## 3. Run
- **Two-way command bot (long-polling):**
  `python telegram_bot.py`
  Commands: `/predict <city>` (ny, chi, mia, lax, den, aus, phil), `/report`,
  `/status`, `/health`. Unknown input and internal errors come back as readable
  text, never a stack trace.
- **Scheduled push** (e.g. from Task Scheduler / cron) — send one command's
  output to your chat:
  `python telegram_bot.py push report`   (or `status` / `health` / `predict lax`)

## 4. Rollback to Pushover
Set `NOTIFIER=pushover` (or remove the line) in `.env`. The hourly T-24 alerts
and daily digest revert to the original `pushover_config.json` path unchanged.

## Notes
- Scheduled alerts (hourly T-24 from `snapshot.py`, daily/catch-up digest from
  `generate_t24_card.py`) flow through the `NOTIFIER` switch automatically — set
  `NOTIFIER=telegram` and they go to Telegram instead of Pushover.
- The push path uses a plain HTTPS POST (`notifiers/telegram.py`); only the
  long-polling command server needs `python-telegram-bot`.
