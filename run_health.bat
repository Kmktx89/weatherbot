@echo off
REM Daily model-health scan. Started by the WeatherbotHealth scheduled task.
REM Writes docs/MODEL_HEALTH.md, stubs docs/MODEL_CHANGES.md on deploy change,
REM and commits ONLY those docs + the marker (never model code).
cd /d "C:\Users\KrisKnecht\weatherbot"
"C:\Users\KrisKnecht\AppData\Local\Programs\Python\Python313\python.exe" -m lab.cli health --days 14 --write >> health.log 2>&1
git add docs/MODEL_HEALTH.md docs/MODEL_CHANGES.md docs/.health_marker
git commit -m "health: daily scan %DATE%" >> health.log 2>&1
