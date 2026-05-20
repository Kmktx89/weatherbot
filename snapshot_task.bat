@echo off
REM Hourly snapshotter for the weatherbot live calibration log.
REM Invoked by Windows Task Scheduler; do not edit without updating the
REM scheduled task too. Output: live_picks_log.jsonl (rows) + snapshot.err (stderr).

cd /d "%~dp0"
"C:\Users\KrisKnecht\AppData\Local\Programs\Python\Python313\python.exe" snapshot.py >> snapshot.err 2>&1
