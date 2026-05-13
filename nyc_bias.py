"""Diagnose NYC forecast bias over last 60 days of settled events.

Pulls each KXHIGHNY event's winning bucket (= actual high integer range), then
fetches what the historical Open-Meteo forecasts said (ECMWF, GFS) at the
default Central-Park coords AND at two alternates (exact Central Park,
LaGuardia). Computes per-source bias (forecast minus actual midpoint) and
checks whether a coordinate change would resolve it.
"""

import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import kalshi_temp as kt

DAYS = 60

# alternate coordinates to test
COORDS = {
    "current (40.7794, -73.9692)": (40.7794, -73.9692),
    "exact Central Park (40.7831, -73.9712)": (40.7831, -73.9712),
    "LaGuardia (KLGA) (40.7772, -73.8726)": (40.7772, -73.8726),
    "JFK (KJFK) (40.6398, -73.7789)": (40.6398, -73.7789),
}


def actual_high_midpoint(winner_market):
    """Mid-integer of the winning bucket — best proxy for the day's actual high."""
    st = winner_market.get("strike_type")
    floor = winner_market.get("floor_strike")
    cap = winner_market.get("cap_strike")
    if st == "between" and floor is not None and cap is not None:
        return (floor + cap) / 2.0
    if st == "less" and cap is not None:
        return cap - 1.0  # "X or below" bucket, midpoint = X-1
    if st == "greater" and floor is not None:
        return floor + 1.0  # "X or above" bucket, midpoint = X+1
    return None


def fetch_event(event_ticker):
    series = event_ticker.split("-")[0]
    try:
        markets = kt.kalshi_get("/markets",
                                {"event_ticker": event_ticker, "limit": 200}
                                ).get("markets", []) or []
    except Exception:
        return None
    if not markets:
        return None
    winner = next((m for m in markets if m.get("result") == "yes"), None)
    if winner is None:
        return None
    actual = actual_high_midpoint(winner)
    if actual is None:
        return None
    target_date = kt.event_local_date({"event_ticker": event_ticker,
                                       "strike_date": markets[0].get("close_time", "")})
    forecasts = {}
    for name, (lat, lon) in COORDS.items():
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_ec  = pool.submit(kt.fetch_open_meteo, lat, lon, target_date,
                                "ecmwf_ifs025", historical=True)
            f_gfs = pool.submit(kt.fetch_open_meteo, lat, lon, target_date,
                                "gfs_seamless", historical=True)
            ecmwf, gfs = f_ec.result(), f_gfs.result()
        if ecmwf is not None and gfs is not None:
            forecasts[name] = {"ecmwf": ecmwf, "gfs": gfs, "mu": (ecmwf + gfs) / 2.0}
    return {
        "event": event_ticker,
        "date": target_date,
        "actual": actual,
        "winner_subtitle": winner.get("subtitle") or kt.synth_subtitle(winner),
        "forecasts": forecasts,
    }


def summarize_errors(errs, label):
    if not errs:
        return f"  {label:<42}  n=0"
    mean = statistics.mean(errs)
    median = statistics.median(errs)
    stdev = statistics.pstdev(errs) if len(errs) > 1 else 0.0
    pos = sum(1 for e in errs if e > 0.5)
    neg = sum(1 for e in errs if e < -0.5)
    near = len(errs) - pos - neg
    return (f"  {label:<42}  n={len(errs):>3}  mean={mean:+5.2f}  median={median:+5.2f}  "
            f"sd={stdev:4.2f}  over>0.5={pos}/{len(errs)}  under>0.5={neg}/{len(errs)}  near={near}")


def main():
    print(f"Pulling NYC settled events (last {DAYS} days)...", file=sys.stderr)
    events = kt.list_events_for_series("KXHIGHNY", DAYS)
    print(f"  {len(events)} events", file=sys.stderr)

    rows = []
    for i, ev in enumerate(events, 1):
        d = fetch_event(ev["event_ticker"])
        if d:
            rows.append(d)
        if i % 10 == 0:
            print(f"  {i}/{len(events)}", file=sys.stderr)

    print()
    print(f"NYC forecast bias — {len(rows)} settled events, last {DAYS} days")
    print("=" * 100)
    print()
    print("Bias = forecast - actual (positive = forecast too HIGH, negative = too LOW)")
    print()
    print("Per-coordinate, combined ECMWF+GFS mu:")
    for name in COORDS:
        errs = [r["forecasts"][name]["mu"] - r["actual"]
                for r in rows if name in r["forecasts"]]
        print(summarize_errors(errs, name))

    print()
    print("Per-source at default coords:")
    name = list(COORDS.keys())[0]
    for src in ("ecmwf", "gfs"):
        errs = [r["forecasts"][name][src] - r["actual"]
                for r in rows if name in r["forecasts"]]
        print(summarize_errors(errs, f"{src.upper()} at {name.split(' ')[0]}"))

    # Per-event detail
    print()
    print("Per-event detail (mu - actual at default coords):")
    print(f"  {'event':<24} {'date':<12} {'actual':>7} {'mu':>6} {'err':>6}  bucket")
    cur_name = list(COORDS.keys())[0]
    for r in sorted(rows, key=lambda x: x["date"]):
        if cur_name not in r["forecasts"]:
            continue
        mu = r["forecasts"][cur_name]["mu"]
        err = mu - r["actual"]
        flag = ""
        if abs(err) >= 3: flag = " <-- BIG MISS"
        elif abs(err) >= 2: flag = " <-- miss"
        print(f"  {r['event']:<24} {r['date']:<12} {r['actual']:>6.1f}  {mu:>5.1f}  {err:>+5.1f}  "
              f"{r['winner_subtitle']}{flag}")


if __name__ == "__main__":
    main()
