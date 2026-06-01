"""Two-way weatherbot Telegram bot (long-polling).

Thin transport only: each command handler delegates to a pure function in
telegram_commands (args in -> text out), so command logic is testable without a
token or a live connection. Credentials come from .env via wb_config
(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID). See docs/TELEGRAM_SETUP.md.

Usage:
  python telegram_bot.py            # long-poll for /predict /report /status /health
  python telegram_bot.py push report   # push one command's output to TELEGRAM_CHAT_ID
                                        # (for a scheduled forecast/report update)

Requires python-telegram-bot for the polling mode only (see requirements.txt);
the `push` mode uses the requests-based TelegramNotifier and needs no extra deps.
"""
import sys

import wb_config
import telegram_commands as cmds

# Map command name -> pure handler. context.args (predict) handled in the wrapper.
_COMMANDS = {
    "predict": lambda args: cmds.cmd_predict(" ".join(args) if args else ""),
    "report": lambda args: cmds.cmd_report(),
    "status": lambda args: cmds.cmd_status(),
    "health": lambda args: cmds.cmd_health(),
    "help": lambda args: cmds.cmd_help(),
    "start": lambda args: cmds.cmd_help(),
}


def build_application(token: str):
    """Build the python-telegram-bot Application and register handlers.

    Does NOT open a network connection (that happens in run_polling), so this is
    safe to call with a stub token in tests."""
    from telegram.ext import Application, CommandHandler

    app = Application.builder().token(token).build()

    def _make(name):
        async def handler(update, context):
            try:
                text = _COMMANDS[name](getattr(context, "args", None) or [])
            except Exception as e:                      # belt-and-suspenders: never leak a trace
                text = f"sorry, /{name} failed: {e}"
            if update.message is not None:
                await update.message.reply_text(text)
        return handler

    app.add_handler(CommandHandler("predict", _make("predict")))
    app.add_handler(CommandHandler("report", _make("report")))
    app.add_handler(CommandHandler("status", _make("status")))
    app.add_handler(CommandHandler("health", _make("health")))
    app.add_handler(CommandHandler(["start", "help"], _make("help")))
    return app


def push(command: str, args=None) -> int:
    """Send one command's output to TELEGRAM_CHAT_ID (scheduled push path)."""
    fn = _COMMANDS.get(command)
    if fn is None:
        print(f"unknown command '{command}'; choose from {sorted(_COMMANDS)}", file=sys.stderr)
        return 2
    from notifiers.telegram import TelegramNotifier
    notifier = TelegramNotifier()
    if not notifier.available():
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set (see docs/TELEGRAM_SETUP.md)",
              file=sys.stderr)
        return 1
    body = fn(args or [])
    ok, info = notifier.send(f"weatherbot /{command}", body)
    if not ok:
        print(f"[telegram push] failed: {info[:200]}", file=sys.stderr)
        return 1
    print(f"[telegram push] sent /{command}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "push":
        cmd = argv[1] if len(argv) > 1 else "report"
        return push(cmd, argv[2:])

    token = wb_config.get("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN not set (see docs/TELEGRAM_SETUP.md)", file=sys.stderr)
        return 1
    app = build_application(token)
    print("weatherbot telegram bot: long-polling for commands...", file=sys.stderr)
    app.run_polling()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
