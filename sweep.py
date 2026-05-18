"""Threshold + lead-hour sweep across all KXHIGH series. Reads model + price
data once per (event, lead) and explores (threshold, lead) in-memory."""

import math
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import kalshi_temp as kt

SERIES = ["KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA",
          "KXHIGHLAX", "KXHIGHDEN", "KXHIGHAUS", "KXHIGHPHIL"]
import argparse as _ap
_p = _ap.ArgumentParser()
_p.add_argument("--days", type=int, default=14)
_p.add_argument("--min-bets", type=int, default=10)
_p.add_argument("--leads", type=str, default="12,18,24,36,48",
                help="comma-separated lead hours to test")
_args = _p.parse_args()
DAYS = _args.days
LEAD_HOURS = [int(x) for x in _args.leads.split(",") if x.strip()]
THRESHOLDS = [0.30, 0.40, 0.50, 0.60, 0.70]
MIN_BETS = _args.min_bets  # ignore configs with too few bets when reporting top picks


def precompute_event(event_ticker):
    """Returns model+winner data once per event. None if unusable."""
    series = event_ticker.split("-")[0]
    city = kt.CITIES.get(series)
    if not city:
        return None
    try:
        markets = kt.kalshi_get("/markets",
                                {"event_ticker": event_ticker, "limit": 200}
                                ).get("markets", []) or []
    except Exception as e:
        print(f"  [skip] {event_ticker}: market fetch {e}", file=sys.stderr)
        return None
    if not markets:
        return None
    winner = next((m for m in markets if m.get("result") == "yes"), None)
    if winner is None:
        return None
    target_date = kt.event_local_date({"event_ticker": event_ticker,
                                       "strike_date": markets[0].get("close_time", "")})
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_ec  = pool.submit(kt.fetch_open_meteo, city["lat"], city["lon"],
                            target_date, "ecmwf_ifs025", historical=True)
        f_gfs = pool.submit(kt.fetch_open_meteo, city["lat"], city["lon"],
                            target_date, "gfs_seamless", historical=True)
        ecmwf, gfs = f_ec.result(), f_gfs.result()
    sources = [v for v in (ecmwf, gfs) if v is not None]
    if not sources:
        return None
    mu_raw = kt.weighted_mean(series, ecmwf, gfs)
    if mu_raw is None:
        mu_raw = sum(sources) / len(sources)
    mu = mu_raw - kt.BIAS.get(series, 0.0)
    spread = statistics.pstdev(sources) if len(sources) > 1 else 0.0
    sigma = math.sqrt(kt.BASE_SIGMA ** 2 + spread ** 2)
    ranked = []
    for m in markets:
        p = kt.bucket_probability(m, mu, sigma)
        if p is not None:
            ranked.append((m, p))
    if not ranked:
        return None
    ranked.sort(key=lambda x: x[1], reverse=True)
    pick, pick_p = ranked[0]
    if not pick.get("close_time"):
        return None
    try:
        close_dt = datetime.fromisoformat(pick["close_time"].replace("Z", "+00:00"))
    except Exception:
        return None
    return {
        "series": series,
        "event": event_ticker,
        "winner_ticker": winner["ticker"],
        "winner_bucket": winner.get("subtitle") or kt.synth_subtitle(winner) or "?",
        "pick_ticker": pick["ticker"],
        "pick_bucket": pick.get("subtitle") or kt.synth_subtitle(pick) or "?",
        "pick_p": pick_p,
        "mu": mu, "sigma": sigma,
        "close_ts": int(close_dt.timestamp()),
        "won_if_bet": pick["ticker"] == winner["ticker"],
    }


def collect_prices(events, lead_hours):
    """For each event, fetch yes_ask close price at (close_ts - lead*3600) per lead in lead_hours."""
    prices = {}  # (event, lead) -> yes_ask price or None
    total = len(events) * len(lead_hours)
    n = 0
    for ev in events:
        series = ev["series"]
        for lead in lead_hours:
            decision_ts = ev["close_ts"] - int(lead * 3600)
            price = kt.yes_ask_at(series, ev["pick_ticker"], decision_ts)
            prices[(ev["event"], lead)] = price
            n += 1
            if n % 20 == 0:
                print(f"  prices {n}/{total}", file=sys.stderr)
    return prices


def evaluate(events, prices, threshold, lead):
    """Walk events, apply threshold + use price at given lead. Return summary dict."""
    bets, wins, pnls = 0, 0, []
    skip_threshold = skip_noprice = 0
    for ev in events:
        if ev["pick_p"] < threshold:
            skip_threshold += 1
            continue
        price = prices.get((ev["event"], lead))
        if price is None or price <= 0.01 or price >= 0.99:
            skip_noprice += 1
            continue
        bets += 1
        if ev["won_if_bet"]:
            wins += 1
            pnls.append(1.0 - price)
        else:
            pnls.append(-price)
    if not bets:
        return {"bets": 0, "wins": 0, "win_rate": 0.0, "pnl": 0.0,
                "max_dd": 0.0, "avg": 0.0,
                "skip_threshold": skip_threshold, "skip_noprice": skip_noprice}
    total = sum(pnls)
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {"bets": bets, "wins": wins, "win_rate": wins / bets,
            "pnl": total, "max_dd": max_dd, "avg": total / bets,
            "skip_threshold": skip_threshold, "skip_noprice": skip_noprice}


def main():
    print("Phase 1: enumerating settled events ...", file=sys.stderr)
    all_events = []
    for s in SERIES:
        evs = kt.list_events_for_series(s, DAYS)
        print(f"  {s}: {len(evs)} settled events", file=sys.stderr)
        all_events.extend(evs)
    print(f"  total: {len(all_events)} events", file=sys.stderr)

    print("Phase 2: precomputing model + winner for each event ...", file=sys.stderr)
    t0 = time.time()
    pre = []
    for i, ev in enumerate(all_events, 1):
        p = precompute_event(ev["event_ticker"])
        if p:
            pre.append(p)
        if i % 10 == 0:
            print(f"  model {i}/{len(all_events)}  ({time.time()-t0:.0f}s)", file=sys.stderr)
    print(f"  usable events: {len(pre)}  ({time.time()-t0:.0f}s)", file=sys.stderr)

    print(f"Phase 3: fetching yes_ask prices at lead hours {LEAD_HOURS} ...", file=sys.stderr)
    t0 = time.time()
    prices = collect_prices(pre, LEAD_HOURS)
    print(f"  done ({time.time()-t0:.0f}s)", file=sys.stderr)

    print()
    print("=" * 84)
    print(f"  Sweep over {len(pre)} events, thresholds {THRESHOLDS}, lead hours {LEAD_HOURS}")
    print("=" * 84)

    rows = []
    for th in THRESHOLDS:
        for lead in LEAD_HOURS:
            s = evaluate(pre, prices, th, lead)
            rows.append({"th": th, "lead": lead, **s})

    # Combined table
    print()
    print("COMBINED sweep:")
    print(f"  {'th':>5} {'lead':>5} {'bets':>5} {'wins':>5} {'WR%':>6} "
          f"{'PnL':>8} {'avg':>8} {'maxDD':>8}")
    for r in rows:
        print(f"  {r['th']:>5.2f} {r['lead']:>5} {r['bets']:>5} {r['wins']:>5} "
              f"{r['win_rate']*100:>5.1f}% {r['pnl']:>+8.3f} {r['avg']:>+8.4f} {r['max_dd']:>8.3f}")

    # Best by win rate (≥ MIN_BETS)
    print()
    print(f"Top 5 configs by WR (min {MIN_BETS} bets):")
    qualifiers = [r for r in rows if r["bets"] >= MIN_BETS]
    for r in sorted(qualifiers, key=lambda r: r["win_rate"], reverse=True)[:5]:
        print(f"  th={r['th']:.2f} lead={r['lead']}h "
              f"bets={r['bets']} WR={r['win_rate']*100:.1f}% "
              f"PnL=${r['pnl']:+.3f}  maxDD=${r['max_dd']:.3f}")

    # Best by PnL (≥ MIN_BETS)
    print()
    print(f"Top 5 configs by PnL (min {MIN_BETS} bets):")
    for r in sorted(qualifiers, key=lambda r: r["pnl"], reverse=True)[:5]:
        print(f"  th={r['th']:.2f} lead={r['lead']}h "
              f"bets={r['bets']} WR={r['win_rate']*100:.1f}% "
              f"PnL=${r['pnl']:+.3f}  maxDD=${r['max_dd']:.3f}")

    # Per-series breakdown at best combined config (highest WR with min_bets)
    if qualifiers:
        best = max(qualifiers, key=lambda r: r["win_rate"])
        print()
        print(f"Per-series at best config (th={best['th']:.2f}, lead={best['lead']}h):")
        for s in SERIES:
            sub = [e for e in pre if e["series"] == s]
            r = evaluate(sub, prices, best["th"], best["lead"])
            print(f"  {s:<14} bets={r['bets']:>2} wins={r['wins']:>2} "
                  f"WR={r['win_rate']*100:>5.1f}% PnL=${r['pnl']:+.3f}")


if __name__ == "__main__":
    main()
