"""Capture (ModelInputs, expected_output) snapshots from CURRENT production.

Run from repo root, BEFORE the kalshi_temp.py extraction in Tasks 10-11.

    python tests/_record_baseline.py KXHIGHNY-26MAY15 KXHIGHNY-26MAY16 ...

For each event ticker, this script:
    1. Calls kalshi_temp.build_event_data (the current live path)
    2. Captures the inputs (forecasts, metar, today_max, markets) that
       went into the model decision and the outputs (mu, sigma, probs)
    3. Writes tests/fixtures/baseline/<event_ticker>.json

These snapshots become the ground truth that Task 11 must reproduce
to 4 decimal places after the extraction.
"""
import json
import sys
import time
from pathlib import Path

# Ensure repo root is on sys.path so kalshi_temp is importable when the
# script is run as  python tests/_record_baseline.py  from the repo root.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import kalshi_temp as kt


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "baseline"


def capture_one(event_ticker: str) -> dict:
    ev, markets = kt.fetch_event_and_markets(event_ticker)
    data = kt.build_event_data(ev, markets)
    if data is None:
        raise SystemExit(f"unsupported event: {event_ticker}")

    f = data["forecasts"]
    m = data["model"]

    # Derive today_max_active the same way compute(LIVE_TODAY) does:
    # truncation = today_max - 0.5 if today_max else None
    # today_max_active = truncation > mu_after_bias
    today_max = f.get("today_max")
    mu_raw = m.get("mu_raw")
    bias = m.get("bias", 0.0)
    mu_after_bias = (mu_raw - bias) if mu_raw is not None else None
    truncation = (today_max - 0.5) if today_max is not None else None
    today_max_active = bool(
        truncation is not None
        and mu_after_bias is not None
        and truncation > mu_after_bias
    )

    fixture = {
        "event_ticker": event_ticker,
        "series": ev["series_ticker"],
        "target_date": data["target_date"],
        "forecasts": {
            "ecmwf": f.get("ecmwf"),
            "gfs": f.get("gfs"),
            "nws": f.get("nws"),
        },
        "metar_current": f.get("metar"),
        "today_max": today_max,
        "markets": [
            {k: v for k, v in market.items()
             if k in {"ticker", "strike_type", "floor_strike", "cap_strike",
                      "yes_ask_dollars", "yes_bid_dollars", "no_ask_dollars",
                      "subtitle"}}
            for market in markets
        ],
        "captured_at": int(time.time()),
        "expected": {
            "mu": m.get("mu"),
            "sigma": m.get("sigma"),
            "mu_raw": m.get("mu_raw"),
            "bias_applied": m.get("bias", 0.0),
            "truncation": m.get("truncation"),
            "today_max_active": today_max_active,
            "probs": {x["ticker"]: x["prob"] for x in data["markets"]
                      if x.get("prob") is not None},
        },
    }
    return fixture


def main(argv):
    if not argv:
        sys.exit("Usage: python tests/_record_baseline.py TICKER1 [TICKER2 ...]")
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for ticker in argv:
        print(f"Capturing {ticker}...", file=sys.stderr)
        fixture = capture_one(ticker)
        path = FIXTURE_DIR / f"{ticker}.json"
        path.write_text(json.dumps(fixture, indent=2))
        print(f"  -> {path}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
