@echo off
REM Supervisor for the weatherbot Telegram command bot (WB-006).
REM Started by the WeatherbotTelegram scheduled task at logon.
REM If the bot exits for any reason, this loop restarts it after 3 seconds.
REM Uses pythonw.exe (windowless) so no console window appears — mirrors
REM run_dashboard.bat. Reads .env (NOTIFIER / TELEGRAM_BOT_TOKEN /
REM TELEGRAM_CHAT_ID). To roll back: disable the WeatherbotTelegram task and set
REM NOTIFIER=pushover in .env.
REM
REM NOTE: Telegram allows only ONE long-poller per bot token. Do not run a second
REM `python telegram_bot.py` while this task is active (Conflict / 409 errors).
cd /d "C:\Users\KrisKnecht\weatherbot"
:loop
echo === telegram supervisor: launching %DATE% %TIME% === >> telegram_bot.log
"C:\Users\KrisKnecht\AppData\Local\Programs\Python\Python313\pythonw.exe" telegram_bot.py >> telegram_bot.log 2>&1
echo === telegram supervisor: bot exited code=%ERRORLEVEL% %DATE% %TIME% === >> telegram_bot.log
timeout /t 3 /nobreak >nul
goto loop
