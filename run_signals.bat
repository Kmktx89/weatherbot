@echo off
REM Hourly qualifying-signal report. Started by the WeatherbotSignals task.
REM Read-only on the model; writes only hourly_signals.json. Not committed.
cd /d "C:\Users\KrisKnecht\weatherbot"
"C:\Users\KrisKnecht\AppData\Local\Programs\Python\Python313\python.exe" hourly_signals.py >> signals.log 2>&1
