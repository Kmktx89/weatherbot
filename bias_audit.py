"""Per-city forecast bias audit over the last N days of settled events.

For each of the 7 KXHIGH series, computes:
  - n events analyzed
  - mean error  = mean(combined_mu - actual_bucket_midpoint)
  - sd of error
  - per-source mean error (ECMWF, GFS)
  - direction split (% too low, % too high)

Outputs a Python BIAS dict suitable to paste into kalshi_temp.py.
"""

import json
import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import kalshi_temp as kt

DAYS = 60
SERIES = list(kt.CITIES.keys())


def actual_high_midpoint(winner):
    st = winner.get("strike_type")
    floor, cap = winner.get("floor_strike"), winner.get("cap_strike")
    if st == "between" and floor is not None and cap is not None:
        return (floor + cap) / 2.0
    if st == "less" and cap is not None:
        return cap - 1.0
    if st == "greater" and floor is not None:
        return floor + 1.0
    return None


def fetch_event(event_ticker):
    series = event_ticker.split("-")[0]
    city = kt.CITIES.get(series)
    if not city:
        return None
    try:
        markets = kt.kalshi_get("/markets",
                                {"event_ticker": event_ticker, "limit": 200}
                                ).get("markets", []) or []
    except Exception:
        return None
    winner = next((m for m in markets if m.get("result") == "yes"), None)
    if winner is None:
        return None
    actual = actual_high_midpoint(winner)
    if actual is None:
        return None
    target_date = kt.event_local_date({"event_ticker": event_ticker,
                                       "strike_date": markets[0].get("close_time", "")})
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_ec  = pool.submit(kt.fetch_open_meteo, city["lat"], city["lon"],
                            target_date, "ecmwf_ifs025", historical=True)
        f_gfs = pool.submit(kt.fetch_open_meteo, city["lat"], city["lon"],
                            target_date, "gfs_seamless", historical=True)
        ecmwf, gfs = f_ec.result(), f_gfs.result()
    if ecmwf is None or gfs is None:
        return None
    # Match the live model: weighted ECMWF+GFS per city (not simple average).
    mu_weighted = kt.weighted_mean(series, ecmwf, gfs)
    return {"event": event_ticker, "actual": actual,
            "ecmwf": ecmwf, "gfs": gfs, "mu": mu_weighted}


def summarize(label, rows):
    if not rows:
        print(f"  {label:<14}  n=0")
        return None
    errs    = [r["mu"]    - r["actual"] for r in rows]
    errs_ec = [r["ecmwf"] - r["actual"] for r in rows]
    errs_gf = [r["gfs"]   - r["actual"] for r in rows]
    n = len(errs)
    mean  = statistics.mean(errs)
    median = statistics.median(errs)
    sd    = statistics.pstdev(errs) if n > 1 else 0.0
    pos   = sum(1 for e in errs if e > 0.5)
    neg   = sum(1 for e in errs if e < -0.5)
    ec_mean = statistics.mean(errs_ec)
    gf_mean = statistics.mean(errs_gf)
    print(f"  {label:<14}  n={n:>3}  bias={mean:+5.2f}  med={median:+5.2f}  "
          f"sd={sd:4.2f}  ECMWF={ec_mean:+5.2f}  GFS={gf_mean:+5.2f}  "
          f"low={neg}/{n} high={pos}/{n}")
    return {"n": n, "bias": mean, "median": median, "sd": sd,
            "ecmwf_bias": ec_mean, "gfs_bias": gf_mean}


def main():
    print(f"Per-city bias audit, last {DAYS} days")
    print("=" * 100)
    print(f"  {'series':<14}  {'n':>4}  {'bias':>5}  {'med':>5}  {'sd':>4}  "
          f"{'ECMWF':>5}  {'GFS':>5}  low/n  high/n")

    table = {}
    t0 = time.time()
    for s in SERIES:
        evs = kt.list_events_for_series(s, DAYS)
        rows = []
        for ev in evs:
            d = fetch_event(ev["event_ticker"])
            if d:
                rows.append(d)
        stats = summarize(s, rows)
        if stats:
            table[s] = stats
        print(f"    (elapsed: {time.time()-t0:.0f}s)", file=sys.stderr)

    print()
    print("Suggested BIAS table to paste into kalshi_temp.py:")
    print()
    print("BIAS = {")
    for s, st in table.items():
        print(f"    {s!r:<14}: {st['bias']:+6.2f},  # n={st['n']}, sd={st['sd']:.2f}")
    print("}")

    print()
    print("As JSON (for the dashboard):")
    print(json.dumps({s: {"bias": round(st["bias"], 2), "n": st["n"], "sd": round(st["sd"], 2)}
                      for s, st in table.items()}, indent=2))


if __name__ == "__main__":
    main()
