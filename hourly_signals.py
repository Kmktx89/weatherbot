"""Hourly runner: pull live event data, apply the interim bar, write
hourly_signals.json. Started by the WeatherbotSignals scheduled task.
Read-only on the model — its ONLY write is hourly_signals.json."""
import json
import ssl
import sys
import urllib.request
from pathlib import Path

import signals

OUT_PATH = "hourly_signals.json"
MARKETS_URL = "https://127.0.0.1:8765/api/markets"


def fetch_events() -> list[dict]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(MARKETS_URL, timeout=30, context=ctx) as r:
        return json.loads(r.read().decode())


def main() -> int:
    events = fetch_events()
    report = signals.build_report(events)
    Path(OUT_PATH).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{report['generated_at']}: {len(report['picks'])} qualifying "
          f"({report['n_open_events']} open events)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
