#!/usr/bin/env python3
"""Snapshot logger — append one JSONL row per open KXHIGH event.

Designed to run hourly via Windows Task Scheduler. Builds the same data the
live dashboard does (LIVE_TODAY: NWS overlay + METAR + today_max) for every
active event, then flattens it into a calibration-friendly schema with
lead_hours computed against the market's close_time.

Output: live_picks_log.jsonl (append-only, one row per event per fire).
Exit code: always 0 (per-event errors are caught and logged to stderr).
"""
import json
import sys
from datetime import datetime, timezone

from kalshi_temp import build_event_data, fetch_kalshi_events, to_float


LOG_PATH = "live_picks_log.jsonl"


def _close_dt(markets):
    if not markets:
        return None, None
    ct = markets[0].get("close_time")
    if not ct:
        return None, None
    try:
        return ct, datetime.fromisoformat(ct.replace("Z", "+00:00"))
    except Exception:
        return None, None


def _bucket(b: dict, raw: dict) -> dict:
    return {
        "ticker": b.get("ticker"),
        "subtitle": b.get("subtitle"),
        "strike_type": b.get("strike_type"),
        "yes_bid": b.get("yes_bid"),
        "yes_ask": b.get("yes_ask"),
        "no_bid": to_float(raw.get("no_bid_dollars")),
        "no_ask": b.get("no_ask"),
        "prob": b.get("prob"),
        "ev_yes": b.get("ev_yes"),
        "ev_no": b.get("ev_no"),
        "volume_24h": b.get("vol_24h"),
    }


def _row(ev: dict, markets: list, ts: datetime) -> dict | None:
    data = build_event_data(ev, markets)
    if data is None:
        return None
    close_time, close_dt = _close_dt(markets)
    lead_hours = (close_dt - ts).total_seconds() / 3600.0 if close_dt else None
    raw_by_ticker = {m["ticker"]: m for m in markets}
    buckets = [
        _bucket(b, raw_by_ticker.get(b.get("ticker"), {}))
        for b in data.get("markets", [])
    ]
    return {
        "ts": ts.isoformat(),
        "event_ticker": data["event_ticker"],
        "series": ev.get("series_ticker"),
        "target_date": data.get("target_date"),
        "close_time": close_time,
        "lead_hours": lead_hours,
        "settled": data.get("settled"),
        "settled_bucket": data.get("settled_bucket"),
        "model": data.get("model"),
        "forecasts": data.get("forecasts"),
        "buckets": buckets,
    }


def main() -> int:
    ts = datetime.now(timezone.utc)
    try:
        events = fetch_kalshi_events()
    except Exception as e:
        print(f"[snapshot] fetch_kalshi_events: {e}", file=sys.stderr)
        return 0

    n_written = 0
    for ev, markets in events:
        et = ev.get("event_ticker", "?")
        try:
            row = _row(ev, markets, ts)
            if row is None:
                continue
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            n_written += 1
        except Exception as e:
            print(f"[snapshot] {et}: {e}", file=sys.stderr)

    print(f"[snapshot] wrote {n_written} rows to {LOG_PATH} at {ts.isoformat()}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
