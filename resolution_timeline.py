"""When does each market actually resolve?

For each settled event over the last N days:
  - Get the winning bucket (result == 'yes')
  - Fetch its candlestick history covering the trading window
  - Find the earliest timestamp where yes_bid first crossed 0.85
    (= "the market priced it as a near-certainty")
  - Compute hours_before_close = (close_ts - resolution_ts) / 3600
  - Convert resolution_ts to the city's local clock hour

Output: per-series mean/median hours-before-close and local-clock-hour.

Use the median hours-before-close to set a per-series 'safe entry lead'
(typically 2-3 hours BEFORE the median resolution, so the market hasn't
already priced in the answer).
"""

import statistics
import sys
import time
from datetime import datetime, timezone

import kalshi_temp as kt

DAYS = 60
THRESHOLD_YES_BID = 0.85   # "market priced as near-certain"


def find_resolution_ts(series, ticker, close_ts):
    """Find first timestamp where yes_bid >= THRESHOLD_YES_BID. Returns unix-ts or None."""
    # 36-hour lookback covers the full trading window for daily-high events
    bars = kt.fetch_kalshi_candlestick(series, ticker,
                                       close_ts - 36 * 3600, close_ts + 600, 60)
    for b in sorted(bars, key=lambda x: x["end_period_ts"]):
        yes_bid = kt.to_float((b.get("yes_bid") or {}).get("close_dollars"))
        if yes_bid is not None and yes_bid >= THRESHOLD_YES_BID:
            return b["end_period_ts"]
    return None


def analyze_event(event_ticker):
    series = event_ticker.split("-")[0]
    city = kt.CITIES.get(series)
    if not city:
        return None
    try:
        markets = kt.kalshi_get("/markets",
                                {"event_ticker": event_ticker, "limit": 200}
                                ).get("markets", []) or []
    except Exception as e:
        print(f"  [skip] {event_ticker}: {e}", file=sys.stderr)
        return None
    if not markets:
        return None
    winner = next((m for m in markets if m.get("result") == "yes"), None)
    if winner is None:
        return None
    try:
        close_dt = datetime.fromisoformat(winner["close_time"].replace("Z", "+00:00"))
    except Exception:
        return None
    close_ts = int(close_dt.timestamp())
    resolution_ts = find_resolution_ts(series, winner["ticker"], close_ts)
    if resolution_ts is None:
        return None
    hours_before_close = (close_ts - resolution_ts) / 3600.0
    # Local clock hour at resolution
    try:
        from zoneinfo import ZoneInfo
        local_dt = datetime.fromtimestamp(resolution_ts, ZoneInfo(city["tz"]))
        local_hour = local_dt.hour + local_dt.minute / 60.0
    except Exception:
        local_hour = None
    return {
        "event": event_ticker,
        "series": series,
        "close_ts": close_ts,
        "resolution_ts": resolution_ts,
        "hours_before_close": hours_before_close,
        "local_hour": local_hour,
    }


def main():
    print(f"Resolution timeline study, last {DAYS} days")
    print("=" * 90)

    per_series = {s: [] for s in kt.CITIES}
    t0 = time.time()
    total = 0
    for s in kt.CITIES:
        evs = kt.list_events_for_series(s, DAYS)
        for ev in evs:
            res = analyze_event(ev["event_ticker"])
            total += 1
            if res:
                per_series[s].append(res)
            if total % 20 == 0:
                print(f"  progress: {total} events scanned ({time.time()-t0:.0f}s)",
                      file=sys.stderr)

    print()
    print(f"  {'series':<14}  n   hours-before-close              local clock hour")
    print(f"  {'':<14}      mean  median  std   min..max          mean  median")
    print("-" * 90)
    summary_rows = {}
    for s in kt.CITIES:
        rows = per_series[s]
        if not rows:
            print(f"  {s:<14}  0   --")
            continue
        hbc = [r["hours_before_close"] for r in rows]
        local = [r["local_hour"] for r in rows if r["local_hour"] is not None]
        mean_hbc = statistics.mean(hbc)
        med_hbc = statistics.median(hbc)
        std_hbc = statistics.pstdev(hbc) if len(hbc) > 1 else 0.0
        mean_local = statistics.mean(local) if local else None
        med_local = statistics.median(local) if local else None
        print(f"  {s:<14}  {len(rows):>2}  "
              f"{mean_hbc:>5.1f}  {med_hbc:>5.1f}  {std_hbc:>4.1f}  "
              f"{min(hbc):>4.1f}..{max(hbc):<5.1f}      "
              f"{mean_local:>5.1f}  {med_local:>5.1f}")
        summary_rows[s] = {
            "n": len(rows),
            "median_hours_before_close": round(med_hbc, 1),
            "median_local_hour": round(med_local, 1) if med_local is not None else None,
            "std_hours_before_close": round(std_hbc, 1),
        }

    print()
    print("Recommended lead_hours (= median hours-before-close + 3h safety buffer):")
    print("  EVENT_LEAD_HOURS = {")
    for s, st in summary_rows.items():
        rec = round(st["median_hours_before_close"] + 3.0, 1)
        print(f"      {s!r:<14}: {rec},  # median resolves at T-{st['median_hours_before_close']:.1f}h (local hour {st['median_local_hour']})")
    print("  }")


if __name__ == "__main__":
    main()
