#!/usr/bin/env python3
"""kalshi_temp.py — Kalshi daily-high-temperature dashboard, predictor, backtester.

Usage:
    python kalshi_temp.py serve   --port 8765
    python kalshi_temp.py predict KXHIGHNY-26MAY13
    python kalshi_temp.py backtest --series KXHIGHNY --days 30
    python kalshi_temp.py backtest --events KXHIGHNY-26MAY10,KXHIGHNY-26MAY11

Fetches Kalshi temperature markets and forecasts from Open-Meteo (ECMWF + GFS),
NWS, and current METAR observations.
"""

import argparse
import json
import math
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


# series_ticker -> resolution station metadata
CITIES = {
    "KXHIGHNY":   {"name": "NYC (Central Park)",         "lat": 40.7794, "lon": -73.9692,  "icao": "KNYC", "tz": "America/New_York"},
    "KXHIGHCHI":  {"name": "Chicago Midway",             "lat": 41.7868, "lon": -87.7522,  "icao": "KMDW", "tz": "America/Chicago"},
    "KXHIGHMIA":  {"name": "Miami International",        "lat": 25.7959, "lon": -80.2870,  "icao": "KMIA", "tz": "America/New_York"},
    "KXHIGHLAX":  {"name": "Los Angeles International",  "lat": 33.9425, "lon": -118.4081, "icao": "KLAX", "tz": "America/Los_Angeles"},
    "KXHIGHDEN":  {"name": "Denver International",       "lat": 39.8617, "lon": -104.6731, "icao": "KDEN", "tz": "America/Denver"},
    "KXHIGHAUS":  {"name": "Austin-Bergstrom",           "lat": 30.1944, "lon": -97.6700,  "icao": "KAUS", "tz": "America/Chicago"},
    "KXHIGHPHIL": {"name": "Philadelphia International", "lat": 39.8744, "lon": -75.2424,  "icao": "KPHL", "tz": "America/New_York"},
}

KALSHI = "https://api.elections.kalshi.com/trade-api/v2"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_HIST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
NWS = "https://api.weather.gov"
METAR_URL = "https://aviationweather.gov/api/data/metar"

USER_AGENT = "kalshi-temp/0.1 (weatherbot fork)"
BASE_SIGMA = 2.0  # °F floor on forecast uncertainty
CACHE_TTL = 300   # seconds
NWS_LOG_PATH = "nws_log.jsonl"  # forward log for future NWS bias audit

# Per-city mean error of weighted_mean(ECMWF, GFS) vs Kalshi winning bucket
# midpoint, measured over 60 days of settled events. bias = forecast - actual.
# Re-fitted with the same per-city weights used in the live/backtest model
# (see SOURCE_WEIGHTS below). To debias: mu_corrected = mu_raw - BIAS[series].
BIAS = {
    "KXHIGHNY":   -0.44,   # n=60, sd=1.44
    "KXHIGHCHI":  -1.04,   # n=60, sd=1.73
    "KXHIGHMIA":  -1.52,   # n=59, sd=1.23
    "KXHIGHLAX":  -0.55,   # n=60, sd=1.63
    "KXHIGHDEN":  -0.81,   # n=60, sd=2.06
    "KXHIGHAUS":  -1.81,   # n=60, sd=1.40
    "KXHIGHPHIL": -1.22,   # n=60, sd=1.38
}

# Per-city source weights (ECMWF, GFS). GFS is less biased everywhere so given
# higher weight. LAX ECMWF is +6°F off — effectively suppressed.
SOURCE_WEIGHTS = {
    "KXHIGHNY":   {"ecmwf_ifs025": 0.40, "gfs_seamless": 0.60},
    "KXHIGHCHI":  {"ecmwf_ifs025": 0.40, "gfs_seamless": 0.60},
    "KXHIGHMIA":  {"ecmwf_ifs025": 0.25, "gfs_seamless": 0.75},
    "KXHIGHLAX":  {"ecmwf_ifs025": 0.10, "gfs_seamless": 0.90},
    "KXHIGHDEN":  {"ecmwf_ifs025": 0.50, "gfs_seamless": 0.50},
    "KXHIGHAUS":  {"ecmwf_ifs025": 0.30, "gfs_seamless": 0.70},
    "KXHIGHPHIL": {"ecmwf_ifs025": 0.35, "gfs_seamless": 0.65},
}

# Decision lead time (hours before close_time) for backtest "auto" mode.
# T-24h dominates T-12h on win rate and avg P/L per bet (see
# sweep_leadtest.out). All KXHIGH cities close ~01:00 local the day after
# resolution, so a uniform 24h lead applies across series.
DEFAULT_LEAD_HOURS = 24.0


def weighted_mean(series, ecmwf, gfs):
    """Combine forecasts using per-city weights. Falls back gracefully on missing sources."""
    w = SOURCE_WEIGHTS.get(series, {"ecmwf_ifs025": 0.5, "gfs_seamless": 0.5})
    parts, total_w = [], 0.0
    if ecmwf is not None:
        parts.append(ecmwf * w["ecmwf_ifs025"]); total_w += w["ecmwf_ifs025"]
    if gfs is not None:
        parts.append(gfs * w["gfs_seamless"]); total_w += w["gfs_seamless"]
    if total_w == 0:
        return None
    return sum(parts) / total_w

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

_kalshi_lock = threading.Lock()
_kalshi_last_call = 0.0
KALSHI_MIN_GAP = 0.25  # seconds


def kalshi_get(path, params=None):
    """GET against Kalshi with simple rate-limit + 429 retry."""
    global _kalshi_last_call
    url = f"{KALSHI}{path}"
    for attempt in range(3):
        with _kalshi_lock:
            wait = KALSHI_MIN_GAP - (time.time() - _kalshi_last_call)
            if wait > 0:
                time.sleep(wait)
            _kalshi_last_call = time.time()
        r = session.get(url, params=params, timeout=15)
        if r.status_code == 429:
            time.sleep(1.0 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()
    return r.json()


# ---------- helpers ----------

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def normal_cdf(x, mu, sigma):
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def event_local_date(ev):
    """Decode 'KXHIGHNY-26MAY12' -> '2026-05-12'."""
    try:
        suffix = ev["event_ticker"].rsplit("-", 1)[-1]
        yy = int(suffix[:2])
        mon = MONTHS[suffix[2:5].upper()]
        day = int(suffix[5:])
        year = 2000 + yy
        return f"{year:04d}-{mon:02d}-{day:02d}"
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(ev["strike_date"].replace("Z", "+00:00"))
        return (dt - timedelta(hours=12)).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


# ---------- data sources ----------

def fetch_kalshi_events():
    out = []
    for series in CITIES:
        try:
            events = kalshi_get("/events",
                                {"series_ticker": series, "limit": 5, "status": "open"}
                                ).get("events", []) or []
        except Exception as e:
            print(f"[kalshi] events {series}: {e}", file=sys.stderr)
            continue
        for ev in events[:2]:
            try:
                markets = kalshi_get("/markets",
                                     {"event_ticker": ev["event_ticker"], "limit": 200}
                                     ).get("markets", []) or []
            except Exception as e:
                print(f"[kalshi] markets {ev['event_ticker']}: {e}", file=sys.stderr)
                markets = []
            out.append((ev, markets))
    return out


def fetch_open_meteo(lat, lon, target_date, model, *, historical=False):
    """Fetch Open-Meteo daily max temp (°F) for target_date.

    historical=True uses the historical-forecast archive (forecasts that were
    issued before the date, used for backtesting). When historical=True the
    response keys are model-suffixed (temperature_2m_max_<model>).
    """
    url = OPEN_METEO_HIST if historical else OPEN_METEO
    try:
        r = session.get(url, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "models": model,
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "start_date": target_date,
            "end_date": target_date,
        }, timeout=15)
        r.raise_for_status()
        daily = r.json().get("daily") or {}
        vals = daily.get("temperature_2m_max") or daily.get(f"temperature_2m_max_{model}") or []
        if vals and vals[0] is not None:
            return float(vals[0])
    except Exception as e:
        print(f"[open-meteo:{model}{':hist' if historical else ''}] {e}", file=sys.stderr)
    return None


def fetch_kalshi_candlestick(series, ticker, start_ts, end_ts, period_minutes=60):
    """Return list of candlestick bars for a market over [start_ts, end_ts]."""
    try:
        return kalshi_get(f"/series/{series}/markets/{ticker}/candlesticks",
                          {"start_ts": start_ts, "end_ts": end_ts,
                           "period_interval": period_minutes}).get("candlesticks", []) or []
    except Exception as e:
        print(f"[kalshi candle] {ticker}: {e}", file=sys.stderr)
        return []


def yes_prices_at(series, ticker, decision_ts):
    """Return (yes_ask, yes_bid) at decision_ts, picking the closest bar ≤ that time."""
    window = 6 * 3600
    bars = fetch_kalshi_candlestick(series, ticker,
                                    decision_ts - window, decision_ts + 600, 60)
    best = None
    for b in bars:
        if b["end_period_ts"] <= decision_ts + 600:
            best = b
    if best:
        return (to_float((best.get("yes_ask") or {}).get("close_dollars")),
                to_float((best.get("yes_bid") or {}).get("close_dollars")))
    return None, None


def yes_ask_at(series, ticker, decision_ts):
    """YES ask price at decision_ts (or the closest bar ≤ that time)."""
    return yes_prices_at(series, ticker, decision_ts)[0]


def fetch_nws_high(lat, lon, target_date):
    try:
        r = session.get(f"{NWS}/points/{lat:.4f},{lon:.4f}", timeout=15)
        r.raise_for_status()
        forecast_url = r.json()["properties"]["forecast"]
        r = session.get(forecast_url, timeout=15)
        r.raise_for_status()
        for p in r.json()["properties"]["periods"]:
            if not p.get("isDaytime"):
                continue
            if p["startTime"][:10] == target_date:
                temp = float(p["temperature"])
                if p.get("temperatureUnit") == "C":
                    temp = temp * 9 / 5 + 32
                return temp
    except Exception as e:
        print(f"[nws] {e}", file=sys.stderr)
    return None


def fetch_metar_temp(icao):
    try:
        r = session.get(METAR_URL, params={"ids": icao, "format": "json"}, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data and data[0].get("temp") is not None:
            return float(data[0]["temp"]) * 9 / 5 + 32
    except Exception as e:
        print(f"[metar:{icao}] {e}", file=sys.stderr)
    return None


def fetch_metar_today_max(icao, hours=10):
    """Max temperature (°F) observed at icao in the last `hours` hours."""
    try:
        r = session.get(METAR_URL,
                        params={"ids": icao, "format": "json", "hours": hours},
                        timeout=15)
        r.raise_for_status()
        data = r.json()
        temps = [float(o["temp"]) * 9 / 5 + 32
                 for o in (data or []) if o.get("temp") is not None]
        return max(temps) if temps else None
    except Exception as e:
        print(f"[metar-window:{icao}] {e}", file=sys.stderr)
        return None


def _local_now(tz_name):
    """Datetime in the city's local tz (uses stdlib zoneinfo)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(tz_name))
    except Exception:
        return datetime.now(timezone.utc)


# ---------- model ----------

def bucket_bounds(market):
    """Continuous bounds for a bucket; lower/upper may be ±inf."""
    st = market.get("strike_type")
    cap = market.get("cap_strike")
    floor = market.get("floor_strike")
    if st == "less" and cap is not None:
        return float("-inf"), cap - 0.5
    if st == "greater" and floor is not None:
        return floor + 0.5, float("inf")
    if st == "between" and floor is not None and cap is not None:
        return floor - 0.5, cap + 0.5
    return None


def bucket_probability(market, mu, sigma, *, lower_truncation=None):
    """Probability the daily high lands in this bucket.

    If lower_truncation is provided (e.g. afternoon METAR max), returns the
    conditional probability P(bucket | high >= lower_truncation), zeroing
    impossible buckets and re-normalizing across the remaining tail.
    """
    bounds = bucket_bounds(market)
    if bounds is None:
        return None
    lower, upper = bounds
    if lower_truncation is None:
        return normal_cdf(upper, mu, sigma) - normal_cdf(lower, mu, sigma)
    if upper <= lower_truncation:
        return 0.0
    denom = 1.0 - normal_cdf(lower_truncation, mu, sigma)
    if denom <= 0:
        return 1.0 if lower_truncation < upper else 0.0
    eff_lower = max(lower, lower_truncation)
    raw = normal_cdf(upper, mu, sigma) - normal_cdf(eff_lower, mu, sigma)
    return max(0.0, raw / denom)


def _sort_key(m):
    floor = m.get("floor_strike")
    cap = m.get("cap_strike")
    if m.get("strike_type") == "less":
        return -1e9 if cap is None else cap - 1000
    if m.get("strike_type") == "greater":
        return 1e9 if floor is None else floor + 1000
    return floor if floor is not None else (cap or 0)


def synth_subtitle(m):
    """Build a 'X° to Y°' / 'X° or below' label from strike_type + floor/cap."""
    st = m.get("strike_type")
    floor, cap = m.get("floor_strike"), m.get("cap_strike")
    if st == "less" and cap is not None:
        return f"{cap - 1}° or below"
    if st == "greater" and floor is not None:
        return f"{floor + 1}° or above"
    if st == "between" and floor is not None and cap is not None:
        return f"{floor}° to {cap}°"
    return None


def _market_summary(m):
    return {
        "ticker": m.get("ticker"),
        "subtitle": m.get("subtitle") or synth_subtitle(m) or "?",
        "strike_type": m.get("strike_type"),
        "yes_bid": to_float(m.get("yes_bid_dollars")),
        "yes_ask": to_float(m.get("yes_ask_dollars")),
        "no_ask": to_float(m.get("no_ask_dollars")),
        "vol_24h": to_float(m.get("volume_24h_fp")) or to_float(m.get("volume_24h")),
        "_sort": _sort_key(m),
    }


def build_event_data(ev, markets):
    series = ev["series_ticker"]
    city = CITIES.get(series)
    if not city:
        return None
    target = event_local_date(ev)

    now_local = _local_now(city["tz"])
    today_local = now_local.date().isoformat()

    with ThreadPoolExecutor(max_workers=5) as pool:
        f_ec  = pool.submit(fetch_open_meteo, city["lat"], city["lon"], target, "ecmwf_ifs025")
        f_gfs = pool.submit(fetch_open_meteo, city["lat"], city["lon"], target, "gfs_seamless")
        f_nws = pool.submit(fetch_nws_high, city["lat"], city["lon"], target)
        f_met = pool.submit(fetch_metar_temp, city["icao"])
        if target == today_local and now_local.hour >= 12:
            f_today = pool.submit(fetch_metar_today_max, city["icao"], 10)
        else:
            f_today = None
        ecmwf, gfs, nws, metar = f_ec.result(), f_gfs.result(), f_nws.result(), f_met.result()
        today_max = f_today.result() if f_today else None

    forecasts = {"ecmwf": ecmwf, "gfs": gfs, "nws": nws, "metar": metar,
                 "today_max": today_max}
    sources = [v for v in (ecmwf, gfs, nws) if v is not None]

    settled_bucket = None
    for m in markets:
        if (to_float(m.get("yes_bid_dollars")) or 0) >= 0.95:
            settled_bucket = m.get("subtitle") or synth_subtitle(m) or "?"
            break
    settled = settled_bucket is not None

    if not sources or settled:
        model = {"mu": None, "sigma": None}
        out_markets = [dict(_market_summary(m), prob=None, ev_yes=None, ev_no=None) for m in markets]
    else:
        mu_raw = weighted_mean(series, ecmwf, gfs)
        if mu_raw is None:
            mu_raw = sum(sources) / len(sources)
        # NWS forecast is the most station-specific source (Kalshi resolves on
        # the NWS Climatological Report at the same station). Blended at 30%.
        # KNOWN LIMITATION: the BIAS table was fit on ECMWF+GFS weighted_mean
        # only — historical NWS forecasts aren't archived by Open-Meteo, so we
        # can't backtest with NWS in the blend. Adding NWS here introduces a
        # residual bias of ~0.3 * (NWS_bias - BIAS[series]), expected to be
        # small (< ~0.5°F) but uncalibrated. Forward logging to nws_log.jsonl
        # will let us refit BIAS for the blended model after ~30 settled events
        # per city accumulate (~4-6 weeks).
        if nws is not None:
            mu_raw = 0.7 * mu_raw + 0.3 * nws
        bias = BIAS.get(series, 0.0)
        mu = mu_raw - bias
        spread = statistics.pstdev(sources) if len(sources) > 1 else 0.0
        sigma = math.sqrt(BASE_SIGMA ** 2 + spread ** 2)

        truncation = None
        if today_max is not None:
            truncation = today_max - 0.5  # METAR readings round; give 0.5°F headroom
            if truncation > mu:
                mu = truncation + 0.3  # forecast can't be cooler than observed peak

        model = {"mu": mu, "mu_raw": mu_raw, "bias": bias,
                 "sigma": sigma, "sources": len(sources),
                 "today_max": today_max, "truncation": truncation}

        out_markets = []
        for m in markets:
            prob = bucket_probability(m, mu, sigma, lower_truncation=truncation)
            ya, na = to_float(m.get("yes_ask_dollars")), to_float(m.get("no_ask_dollars"))
            ev_yes = (prob - ya) if (prob is not None and ya not in (None, 0.0)) else None
            ev_no = ((1 - prob) - na) if (prob is not None and na not in (None, 0.0)) else None
            out_markets.append(dict(_market_summary(m), prob=prob, ev_yes=ev_yes, ev_no=ev_no))

    out_markets.sort(key=lambda x: x["_sort"])
    return {
        "event_ticker": ev["event_ticker"],
        "title": ev.get("title") or ev["event_ticker"],
        "target_date": target,
        "station": f"{city['name']} ({city['icao']})",
        "forecasts": forecasts,
        "model": model,
        "settled": settled,
        "settled_bucket": settled_bucket,
        "markets": out_markets,
    }


# ---------- cache + orchestration ----------

_cache_lock = threading.Lock()
_cache = {"ts": 0.0, "data": None}


def get_dashboard_data(force=False):
    with _cache_lock:
        if not force and _cache["data"] and time.time() - _cache["ts"] < CACHE_TTL:
            return _cache["data"]

    events = fetch_kalshi_events()
    out = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = [pool.submit(build_event_data, ev, ms) for ev, ms in events]
        for f in futs:
            try:
                d = f.result()
                if d:
                    out.append(d)
            except Exception as e:
                print(f"[build] {e}", file=sys.stderr)

    out.sort(key=lambda e: (e["target_date"], e["event_ticker"]))
    data = {"updated": datetime.now(timezone.utc).isoformat(), "events": out}

    with _cache_lock:
        _cache["ts"] = time.time()
        _cache["data"] = data
    return data


# ---------- HTTP layer ----------

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kalshi Temp Predictor</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" type="image/png" href="/icon.png">
<meta name="theme-color" content="#0f1115">
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         max-width: 1400px; margin: 0 auto; padding: 1rem;
         background: #0f1115; color: #d8dde8;
         -webkit-text-size-adjust: 100%; }
  h1 { font-size: 1.3rem; margin: 0 0 1rem; display: flex; gap: 0.75rem;
       align-items: center; flex-wrap: wrap; }
  h1 small { color: #6b7280; font-weight: normal; font-size: 0.8rem; }
  .event { background: #1a1d24; border: 1px solid #2a2e38; border-radius: 8px;
           padding: 1rem; margin-bottom: 1.25rem; }
  .event h2 { font-size: 1rem; margin: 0 0 0.4rem; }
  .summary { display: flex; gap: 1.25rem; flex-wrap: wrap; font-size: 0.82rem; color: #a0a8b8; }
  .summary b { color: #e6ebf5; }
  .event > table, .tool-out > table { /* fallback for legacy spots */ }
  .table-wrap { overflow-x: auto; margin-top: 0.75rem; -webkit-overflow-scrolling: touch; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { padding: 0.35rem 0.5rem; text-align: right; border-bottom: 1px solid #232730;
           white-space: nowrap; }
  th { color: #8892a4; font-weight: 500; }
  th:first-child, td:first-child { text-align: left; }
  .pos { color: #56d364; }
  .neg { color: #f85149; }
  .dim { color: #6b7280; }
  .hi  { background: rgba(86, 211, 100, 0.08); }
  .bar { display: inline-block; height: 6px; width: 60px; background: #2a2e38;
         border-radius: 3px; vertical-align: middle; margin-left: 6px; }
  .bar > span { display: block; height: 100%; background: #58a6ff; border-radius: 3px; }
  button { background: #238636; color: #fff; border: 0; padding: 0.4rem 0.8rem;
           border-radius: 4px; cursor: pointer; font: inherit; }
  button:disabled { opacity: 0.5; cursor: wait; }
  #err { color: #f85149; }
  .tools { display: grid; grid-template-columns: 1fr 2fr; gap: 1rem; margin-bottom: 1.25rem; }
  .tool { background: #1a1d24; border: 1px solid #2a2e38; border-radius: 8px; padding: 0.9rem; }
  .tool h3 { font-size: 0.95rem; margin: 0 0 0.6rem; color: #e6ebf5;
             display: flex; justify-content: space-between; align-items: center; gap: 0.75rem; }
  .presets { font-size: 0.75rem; color: #8892a4; font-weight: normal; }
  .preset { background: #2a2e38; padding: 0.2rem 0.55rem; margin-left: 0.3rem;
            font-size: 0.75rem; border-radius: 3px; }
  .preset:hover { background: #364050; }
  .tool form { display: flex; flex-wrap: wrap; gap: 0.5rem 0.75rem; align-items: end; }
  .tool label { display: flex; flex-direction: column; font-size: 0.75rem;
                color: #8892a4; gap: 0.2rem; }
  .tool label.wide { flex: 1 1 100%; }
  .tool input, .tool select { background: #0f1115; color: #d8dde8;
                              border: 1px solid #2a2e38; border-radius: 4px;
                              padding: 0.3rem 0.4rem; font: inherit; min-width: 7rem; }
  .tool input[type=text] { min-width: 14rem; }
  .tool-out { margin-top: 0.75rem; font-size: 0.85rem; color: #c0c7d4; }
  .tool-out table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
  .tool-out th, .tool-out td { padding: 0.25rem 0.5rem; text-align: right;
                                border-bottom: 1px solid #232730; font-size: 0.8rem; }
  .tool-out th:first-child, .tool-out td:first-child { text-align: left; }
  .kv { display: inline-block; margin-right: 1rem; }
  .kv b { color: #e6ebf5; }
  @media (max-width: 900px) { .tools { grid-template-columns: 1fr; } }
  @media (max-width: 600px) {
    body { padding: 0.6rem; }
    h1 { font-size: 1.15rem; gap: 0.5rem; }
    .tool h3 { flex-direction: column; align-items: flex-start; gap: 0.25rem; }
    .presets { display: flex; flex-wrap: wrap; gap: 0.25rem; }
    .preset { margin-left: 0; padding: 0.4rem 0.7rem; font-size: 0.8rem; }
    .tool input, .tool select, button { font-size: 0.95rem; padding: 0.5rem 0.6rem; }
    .tool input[type=text] { min-width: 0; width: 100%; }
    .summary { font-size: 0.78rem; gap: 0.6rem 1rem; }
    table { font-size: 0.78rem; }
    th, td { padding: 0.3rem 0.4rem; }
  }
</style>
</head>
<body>
<h1>Kalshi temperature predictor
  <small id="ts"></small>
  <button id="btn" onclick="refresh(true)">Refresh</button>
  <button onclick="openPane('/schedule', 'kt-schedule', 460, 460)" style="background:#2f4366;">Schedule</button>
  <button onclick="openPane('/sources', 'kt-sources', 620, 720)" style="background:#2f4366;">Sources</button>
</h1>
<div id="err"></div>

<div class="tools">
  <div class="tool">
    <h3>Predict by ticker</h3>
    <form onsubmit="runPredict(event)">
      <input id="p-ticker" type="text" placeholder="KXHIGHNY-26MAY13" required>
      <button type="submit">Predict</button>
    </form>
    <div id="p-out" class="tool-out"></div>
  </div>

  <div class="tool">
    <h3>Backtest
      <span class="presets">
        Preset:
        <button type="button" class="preset" onclick="setPreset(0.70,12,60)" title="84% WR · 31 bets / 60d · +$2.48 · max DD $1.37">Conservative</button>
        <button type="button" class="preset" onclick="setPreset(0.50,12,60)" title="79% WR · 62 bets / 60d · +$11.71 · max DD $1.37 (BEST risk-adjusted)">Balanced</button>
        <button type="button" class="preset" onclick="setPreset(0.30,12,60)" title="62% WR · 318 bets / 60d · +$37.59 · max DD $5.44 (highest PnL)">Aggressive</button>
        <button type="button" class="preset" onclick="setPreset(0,24,14)" title="No filter, default settings">Default</button>
      </span>
    </h3>
    <form onsubmit="runBacktest(event)">
      <label>Series
        <select id="b-series">
          <option value="">(all series)</option>
          <option>KXHIGHNY</option>
          <option>KXHIGHCHI</option>
          <option>KXHIGHMIA</option>
          <option>KXHIGHLAX</option>
          <option>KXHIGHDEN</option>
          <option>KXHIGHAUS</option>
          <option>KXHIGHPHIL</option>
        </select>
      </label>
      <label>Days <input id="b-days" type="number" min="1" max="365" value="14"></label>
      <label>Threshold <input id="b-threshold" type="number" min="0" max="1" step="0.05" value="0"></label>
      <label>Lead (h) <input id="b-lead" type="number" min="1" max="168" step="0.5" value="24"></label>
      <label class="wide">Events (overrides series)
        <input id="b-events" type="text" placeholder="comma-separated tickers">
      </label>
      <button type="submit">Run backtest</button>
    </form>
    <div id="b-out" class="tool-out"></div>
  </div>
</div>

<div id="root">Loading…</div>
<script>
const fmt    = v => v == null ? '—' : v.toFixed(1) + '°';
const pct    = v => v == null ? '—' : (v * 100).toFixed(1) + '%';
const money  = v => v == null ? '—' : '$' + v.toFixed(2);
const signed = v => v == null ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '¢';
const cls    = v => v == null ? 'dim' : v > 0.03 ? 'pos' : v < -0.03 ? 'neg' : 'dim';

// Opens an aux page as a standalone OS window (resizable, snappable, draggable).
function openPane(path, name, w, h) {
  const left = Math.max(0, (screen.availWidth - w) - 40);
  window.open(path, name,
    `width=${w},height=${h},left=${left},top=80,menubar=no,toolbar=no,location=no,status=no,resizable=yes,scrollbars=yes`);
}

// Wake/idle recovery: backend may be groggy for ~1-15s after the laptop
// resumes from sleep (watchdog runs every 60s). Retry the initial fetch a
// few times so reloading the page or clicking the bookmark "just works"
// instead of showing a transient error.
async function fetchJsonWithRetry(url, onAttempt) {
  const delays = [500, 1000, 2000, 4000, 8000];  // ~15.5s total budget
  let err = null;
  for (let i = 0; i <= delays.length; i++) {
    if (onAttempt) onAttempt(i + 1, delays.length + 1);
    try {
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return await r.json();
    } catch (e) {
      err = e;
      if (i < delays.length) await new Promise(res => setTimeout(res, delays[i]));
    }
  }
  throw err;
}

let _refreshing = false;
async function refresh(force) {
  if (_refreshing) return;
  _refreshing = true;
  const btn = document.getElementById('btn');
  const errEl = document.getElementById('err');
  btn.disabled = true; btn.textContent = 'Loading…';
  errEl.textContent = '';
  try {
    const url = '/api/markets' + (force ? '?refresh=1' : '');
    const data = await fetchJsonWithRetry(url, (n, total) => {
      if (n > 1) errEl.textContent = `Reconnecting… (${n}/${total})`;
    });
    errEl.textContent = '';
    render(data);
  } catch (e) {
    errEl.textContent = 'Error: ' + e.message + ' — will auto-retry on focus';
  } finally {
    btn.disabled = false; btn.textContent = 'Refresh';
    _refreshing = false;
  }
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') refresh(false);
});
window.addEventListener('online', () => refresh(false));

function render(data) {
  document.getElementById('ts').textContent =
    '· updated ' + new Date(data.updated).toLocaleString();
  const root = document.getElementById('root');
  root.innerHTML = '';
  if (!data.events || data.events.length === 0) {
    root.textContent = 'No open events.';
    return;
  }
  for (const ev of data.events) {
    const el = document.createElement('div');
    el.className = 'event';
    const f = ev.forecasts || {};
    const m = ev.model || {};
    let html = `<h2>${ev.title}${ev.settled ? ' <span class="dim">· settled (' + (ev.settled_bucket || '?') + ')</span>' : ''}</h2>`;
    html += `<div class="summary">`;
    html += `<span>Resolves: <b>${ev.target_date}</b></span>`;
    html += `<span>Station: <b>${ev.station}</b></span>`;
    html += `<span>ECMWF <b>${fmt(f.ecmwf)}</b> · GFS/HRRR <b>${fmt(f.gfs)}</b> · NWS <b>${fmt(f.nws)}</b> · METAR <b>${fmt(f.metar)}</b>`;
    if (f.today_max != null) html += ` · TODAY-MAX <b>${fmt(f.today_max)}</b>`;
    html += `</span>`;
    if (m.mu != null) {
      let mod = `<span>Model: <b>μ ${m.mu.toFixed(1)}° · σ ${m.sigma.toFixed(1)}°</b>`;
      if (m.truncation != null) mod += ` <span class="dim">(truncated ≥ ${m.truncation.toFixed(1)}°)</span>`;
      html += mod + `</span>`;
    }
    html += `</div>`;
    html += `<div class="table-wrap"><table><thead><tr>
      <th>Bucket</th><th>Model P</th><th>YES bid</th><th>YES ask</th>
      <th>NO ask</th><th>EV (yes)</th><th>EV (no)</th><th>Vol 24h</th></tr></thead><tbody>`;
    for (const mk of ev.markets) {
      const evMax = Math.max(mk.ev_yes ?? -1, mk.ev_no ?? -1);
      html += `<tr class="${evMax > 0.05 ? 'hi' : ''}">`;
      html += `<td>${mk.subtitle}</td>`;
      html += `<td>${pct(mk.prob)}<span class="bar"><span style="width:${((mk.prob||0)*100).toFixed(0)}%"></span></span></td>`;
      html += `<td>${money(mk.yes_bid)}</td>`;
      html += `<td>${money(mk.yes_ask)}</td>`;
      html += `<td>${money(mk.no_ask)}</td>`;
      html += `<td class="${cls(mk.ev_yes)}">${signed(mk.ev_yes)}</td>`;
      html += `<td class="${cls(mk.ev_no)}">${signed(mk.ev_no)}</td>`;
      html += `<td class="dim">${mk.vol_24h ? mk.vol_24h.toFixed(0) : '—'}</td>`;
      html += `</tr>`;
    }
    html += `</tbody></table></div>`;
    el.innerHTML = html;
    root.appendChild(el);
  }
}

refresh(false);

async function runPredict(e) {
  e.preventDefault();
  const ticker = document.getElementById('p-ticker').value.trim().toUpperCase();
  const out = document.getElementById('p-out');
  out.textContent = 'Loading…';
  try {
    const r = await fetch('/api/predict?event=' + encodeURIComponent(ticker));
    const data = await r.json();
    if (!r.ok) { out.textContent = 'Error: ' + (data.error || r.status); return; }
    renderPredict(data, out);
  } catch (err) {
    out.textContent = 'Error: ' + err.message;
  }
}

function renderPredict(d, out) {
  const f = d.forecasts || {}, m = d.model || {};
  let html = `<div><b>${d.title}</b></div>`;
  html += `<div class="kv">Resolves <b>${d.target_date}</b></div>`;
  html += `<div class="kv">${d.station}</div>`;
  html += `<div><span class="kv">ECMWF <b>${fmt(f.ecmwf)}</b></span>`;
  html += `<span class="kv">GFS/HRRR <b>${fmt(f.gfs)}</b></span>`;
  html += `<span class="kv">NWS <b>${fmt(f.nws)}</b></span>`;
  html += `<span class="kv">METAR <b>${fmt(f.metar)}</b></span>`;
  if (f.today_max != null) html += `<span class="kv">TODAY-MAX <b>${fmt(f.today_max)}</b></span>`;
  html += `</div>`;
  if (m.mu != null) {
    let mod = `<div class="kv">Model: μ <b>${m.mu.toFixed(1)}°</b> σ <b>${m.sigma.toFixed(1)}°</b>`;
    if (m.truncation != null) mod += ` <span class="dim">(truncated ≥ ${m.truncation.toFixed(1)}°)</span>`;
    html += mod + `</div>`;
  }
  if (d.settled) {
    html += `<div class="dim">Settled bucket: <b>${d.settled_bucket}</b></div>`;
    out.innerHTML = html; return;
  }
  const top = d.highest_probability;
  if (top) {
    html += `<div style="margin-top:0.5rem"><b>Highest probability:</b> ${top.subtitle} `
         + `(${(top.prob*100).toFixed(1)}%)<br>`
         + `&nbsp;YES @ ${money(top.yes_ask)}  → <span class="${cls(top.ev_yes)}">${signed(top.ev_yes)}</span>, `
         + `NO @ ${money(top.no_ask)}  → <span class="${cls(top.ev_no)}">${signed(top.ev_no)}</span></div>`;
  }
  const by = d.best_ev_yes, bn = d.best_ev_no;
  if (by && (!top || by.ticker !== top.ticker))
    html += `<div>Best EV YES: ${by.subtitle} @ ${money(by.yes_ask)} → <span class="${cls(by.ev_yes)}">${signed(by.ev_yes)}</span> (P=${(by.prob*100).toFixed(1)}%)</div>`;
  if (bn)
    html += `<div>Best EV NO: ${bn.subtitle} @ ${money(bn.no_ask)} → <span class="${cls(bn.ev_no)}">${signed(bn.ev_no)}</span> (P_no=${((1-bn.prob)*100).toFixed(1)}%)</div>`;
  out.innerHTML = html;
}

function setPreset(threshold, lead, days) {
  document.getElementById('b-threshold').value = threshold;
  document.getElementById('b-lead').value = lead;
  document.getElementById('b-days').value = days;
}

async function runBacktest(e) {
  e.preventDefault();
  const series = document.getElementById('b-series').value;
  const events = document.getElementById('b-events').value.trim();
  const days = document.getElementById('b-days').value;
  const threshold = document.getElementById('b-threshold').value;
  const lead = document.getElementById('b-lead').value;
  const out = document.getElementById('b-out');
  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true;
  const scope = events ? 'event list' : (series ? series : 'all series');
  out.textContent = `Running ${scope}… (all-series @ 60 days can take ~5 minutes)`;
  const params = new URLSearchParams({days, threshold, lead_hours: lead});
  if (series) params.set('series', series);
  if (events) params.set('events', events);
  try {
    const r = await fetch('/api/backtest?' + params.toString());
    const data = await r.json();
    if (!r.ok) { out.textContent = 'Error: ' + (data.error || r.status); return; }
    renderBacktest(data, out);
  } catch (err) {
    out.textContent = 'Error: ' + err.message;
  } finally {
    btn.disabled = false;
  }
}

function renderBacktest(d, out) {
  const ys = (d.summaries && d.summaries.yes) || {};
  const ns = (d.summaries && d.summaries.no)  || {};
  const summaryRow = (label, s) => {
    if (!s.bets) {
      const skip = s.skip_reasons ? Object.entries(s.skip_reasons).map(([k,n]) => `${n}× ${k}`).join(' · ') : '';
      return `<div><b>${label}:</b> no bets${skip ? ' · <span class="dim">' + skip + '</span>' : ''}</div>`;
    }
    return `<div><b>${label}:</b> ${s.bets} bets · ${s.wins} wins (${(s.win_rate*100).toFixed(1)}%) · `
         + `P/L <b class="${s.total_pnl >= 0 ? 'pos' : 'neg'}">$${s.total_pnl.toFixed(3)}</b> · `
         + `avg $${s.avg_pnl.toFixed(3)} · max DD $${s.max_drawdown.toFixed(3)}</div>`;
  };
  let html = summaryRow('YES (high temp)', ys) + summaryRow('NO (best EV)', ns);

  html += `<div class="table-wrap"><table><thead><tr>
    <th>Event</th><th>Winner</th>
    <th>YES pick</th><th>YES P</th><th>YES entry</th><th>YES P/L</th>
    <th>NO pick</th><th>NO P_no</th><th>NO entry</th><th>NO P/L</th>
  </tr></thead><tbody>`;
  for (const r of d.results) {
    if (r.skipped) {
      html += `<tr><td>${r.event}</td><td colspan="9" class="dim">skipped: ${r.skipped}</td></tr>`;
      continue;
    }
    const yo = r.yes || {};
    const no = r.no || {};
    const ycell = (k, fn) => yo.skipped ? '' : fn(yo[k]);
    const ncell = (k, fn) => no.skipped ? '' : fn(no[k]);
    html += `<tr><td>${r.event}</td><td>${r.winner_bucket}</td>`;
    // YES columns
    if (yo.skipped) html += `<td colspan="4" class="dim">skip: ${yo.skipped}</td>`;
    else html += `<td class="${yo.won ? 'pos' : 'neg'}">${yo.picked_bucket}</td>`
             + `<td>${(yo.model_prob*100).toFixed(1)}%</td>`
             + `<td>$${yo.entry_price.toFixed(2)}</td>`
             + `<td class="${yo.pnl >= 0 ? 'pos' : 'neg'}">${yo.pnl >= 0 ? '+' : ''}${yo.pnl.toFixed(3)}</td>`;
    // NO columns
    if (no.skipped) html += `<td colspan="4" class="dim">skip: ${no.skipped}</td>`;
    else html += `<td class="${no.won ? 'pos' : 'neg'}">${no.picked_bucket}</td>`
             + `<td>${(no.model_prob_no*100).toFixed(1)}%</td>`
             + `<td>$${no.entry_price.toFixed(2)}</td>`
             + `<td class="${no.pnl >= 0 ? 'pos' : 'neg'}">${no.pnl >= 0 ? '+' : ''}${no.pnl.toFixed(3)}</td>`;
    html += `</tr>`;
  }
  html += `</tbody></table></div>`;
  out.innerHTML = html;
}
</script>
</body>
</html>
"""


SCHEDULE_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Entry windows</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         background: #0f1115; color: #d8dde8; margin: 0; padding: 1rem; }
  h1 { font-size: 1.05rem; margin: 0 0 0.4rem; color: #e6ebf5; }
  p  { font-size: 0.82rem; color: #a0a8b8; margin: 0 0 0.9rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  th, td { padding: 0.4rem 0.5rem; text-align: left; border-bottom: 1px solid #232730; }
  th { color: #8892a4; font-weight: 500; }
  .status { font-weight: 600; }
  .status.in    { color: #56d364; }
  .status.pre   { color: #f0c674; }
  .status.post  { color: #6b7280; }
  .now { color: #8892a4; font-size: 0.75rem; margin-top: 0.75rem; }
</style>
</head><body>
  <h1>Entry windows — submit selections for <u>tomorrow's</u> resolving market</h1>
  <p>Window is centered on T-24h before close (close ≈ 01:00 local the day after the
     market resolves). Sweep over 30d / 210 events shows 24h leads beat 12h on win rate
     <i>and</i> avg P/L per bet at every threshold. See <code>sweep_leadtest.out</code>.</p>
  <p><b>Note:</b> dashboard predictions are live and real-time — recomputed each refresh
     from current ECMWF/GFS/NWS forecasts and current Kalshi prices. They are not
     back-dated. The schedule below tells you <i>when to submit</i>; the picks themselves
     are always fresh.</p>
  <table>
    <thead><tr><th>City</th><th>Window&nbsp;(local)</th><th>Window&nbsp;(ET)</th><th>Status</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
  <div class="now" id="now"></div>
<script>
// All cities close at ~01:00 local the day after the resolution day, so a single
// 19:00–23:00 local band lands at T-26h to T-30h — inside the 18–36h sweet spot
// from the lead-hour sweep. offsetToET = hours to add to local time to get ET.
const WINDOWS = [
  { city: 'NYC',  tz: 'America/New_York',    start: 19, end: 23, offsetToET: 0 },
  { city: 'CHI',  tz: 'America/Chicago',     start: 19, end: 23, offsetToET: 1 },
  { city: 'MIA',  tz: 'America/New_York',    start: 19, end: 23, offsetToET: 0 },
  { city: 'LAX',  tz: 'America/Los_Angeles', start: 19, end: 23, offsetToET: 3 },
  { city: 'DEN',  tz: 'America/Denver',      start: 19, end: 23, offsetToET: 2 },
  { city: 'AUS',  tz: 'America/Chicago',     start: 19, end: 23, offsetToET: 1 },
  { city: 'PHIL', tz: 'America/New_York',    start: 19, end: 23, offsetToET: 0 },
];

function rangeLabel(startH, endH) {
  const lbl = h => { const hh = ((h % 24) + 24) % 24;
                     const p = hh < 12 ? 'AM' : 'PM';
                     let h12 = hh % 12; if (h12 === 0) h12 = 12;
                     return `${h12} ${p}`; };
  return `${lbl(startH)}–${lbl(endH)}`;
}
function timeIn(tz) {
  const parts = new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: 'numeric',
                                                   hour12: false, timeZone: tz })
                .formatToParts(new Date());
  const h = +parts.find(p => p.type === 'hour').value;
  const m = +parts.find(p => p.type === 'minute').value;
  return { h, m };
}
function render() {
  const rows = WINDOWS.map(w => {
    const lo = timeIn(w.tz);
    const hourDec = lo.h + lo.m / 60;
    let status, cls;
    if (hourDec < w.start)      { status = 'BEFORE';     cls = 'pre';  }
    else if (hourDec <= w.end)  { status = 'IN WINDOW';  cls = 'in';   }
    else                        { status = 'AFTER';      cls = 'post'; }
    const etStart = w.start + w.offsetToET;
    const etEnd   = w.end   + w.offsetToET;
    return `<tr>
      <td>${w.city}</td>
      <td>${rangeLabel(w.start, w.end)}</td>
      <td>${rangeLabel(etStart, etEnd)}</td>
      <td class="status ${cls}">${status}</td>
    </tr>`;
  }).join('');
  document.getElementById('rows').innerHTML = rows;
  document.getElementById('now').textContent = 'Updated ' + new Date().toLocaleTimeString();
}
render();
setInterval(render, 30 * 1000); // refresh every 30s
</script>
</body></html>
"""


SOURCES_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Data sources</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         background: #0f1115; color: #d8dde8; margin: 0; padding: 1rem 1.25rem;
         line-height: 1.45; }
  h1 { font-size: 1.1rem; margin: 0 0 0.75rem; color: #e6ebf5; }
  h2 { font-size: 0.95rem; margin: 1.2rem 0 0.35rem; color: #e6ebf5; }
  p  { font-size: 0.86rem; color: #c0c7d4; margin: 0.3rem 0; }
  .src { background: #1a1d24; border: 1px solid #2a2e38; border-radius: 6px;
         padding: 0.7rem 0.9rem; margin: 0.6rem 0; }
  .src h3 { margin: 0 0 0.3rem; font-size: 0.95rem; color: #58a6ff; }
  .src .meta { font-size: 0.78rem; color: #8892a4; margin: 0.2rem 0 0.4rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; margin: 0.4rem 0; }
  th, td { padding: 0.35rem 0.5rem; text-align: left; border-bottom: 1px solid #232730; vertical-align: top; }
  th { color: #8892a4; font-weight: 500; }
  .live    { color: #56d364; font-weight: 600; }
  .fcst    { color: #f0c674; font-weight: 600; }
  code { background: #0f1115; padding: 0.05rem 0.3rem; border-radius: 3px;
         font-size: 0.78rem; color: #c0c7d4; }
  .tldr { background: #14181f; border-left: 3px solid #58a6ff;
          padding: 0.55rem 0.8rem; font-size: 0.85rem; color: #d8dde8;
          margin: 0.9rem 0; }
</style>
</head><body>
  <h1>Data sources</h1>

  <table>
    <thead><tr><th>Source</th><th>Type</th><th>Location</th><th>Update cadence</th></tr></thead>
    <tbody>
      <tr><td>ECMWF</td>     <td class="fcst">Forecast</td> <td>Grid → station lat/lon</td> <td>2–4× per day</td></tr>
      <tr><td>GFS/HRRR</td>  <td class="fcst">Forecast</td> <td>Grid → station lat/lon</td> <td>Up to hourly</td></tr>
      <tr><td>NWS</td>       <td class="fcst">Forecast</td> <td>2.5km grid cell at station</td> <td>~Every 3 hours</td></tr>
      <tr><td>METAR</td>     <td class="live">Live obs</td>   <td><b>Exactly the resolution station</b></td> <td>Hourly (~:51 past)</td></tr>
    </tbody>
  </table>

  <div class="src">
    <h3>ECMWF</h3>
    <div class="meta">European Centre for Medium-Range Weather Forecasts — IFS model, 0.25° grid. Pulled via Open-Meteo (<code>ecmwf_ifs025</code>).</div>
    <p><b>Type:</b> <span class="fcst">Forecast</span> — today's predicted high temperature.</p>
    <p><b>Location:</b> Global gridded model interpolated to the resolution station's lat/lon.</p>
    <p><b>Updates:</b> ECMWF runs typically twice per day (00Z, 12Z; sometimes 06Z and 18Z too). A new forecast appears ~6–9 hours after each run, so you'll see updated values 2–4 times per day.</p>
  </div>

  <div class="src">
    <h3>GFS / HRRR</h3>
    <div class="meta">NOAA models. GFS = Global Forecast System (6h cadence). HRRR = High Resolution Rapid Refresh (1h cadence, CONUS). Open-Meteo blends them as <code>gfs_seamless</code>.</div>
    <p><b>Type:</b> <span class="fcst">Forecast</span> — today's predicted high temperature.</p>
    <p><b>Location:</b> Interpolated from grid to station's lat/lon. ~25 km grid (GFS) → ~3 km grid (HRRR) for the latest forecast hours.</p>
    <p><b>Updates:</b> Up to <b>hourly</b> — HRRR refreshes every hour; GFS every 6 hours. This is usually the freshest of the three forecasts during the day.</p>
  </div>

  <div class="src">
    <h3>NWS</h3>
    <div class="meta">National Weather Service gridpoint forecast, pulled from <code>api.weather.gov</code>. The same forecast you'd see on weather.gov.</div>
    <p><b>Type:</b> <span class="fcst">Forecast</span> — today's predicted high temperature.</p>
    <p><b>Location:</b> NWS 2.5 km grid cell containing the resolution station's lat/lon.</p>
    <p><b>Updates:</b> Typically every ~3 hours, sometimes more often when conditions change rapidly. This is the forecast Kalshi events conceptually align with (NWS is the national authority for U.S. weather).</p>
  </div>

  <div class="src">
    <h3>METAR</h3>
    <div class="meta">Routine hourly surface observation issued by the airport weather station (e.g., KAUS, KNYC). Pulled from <code>aviationweather.gov</code>.</div>
    <p><b>Type:</b> <span class="live">Live observation</span> — the actual measured temperature at the station.</p>
    <p><b>Location:</b> <b>Exactly the resolution station</b> — the same station Kalshi resolves the event against.</p>
    <p><b>Updates:</b> A new METAR report every hour (~:51 past), plus occasional SPECI reports for rapid weather changes.</p>
    <p><b>On the dashboard, the METAR line shows two numbers:</b></p>
    <p>&nbsp;&nbsp;• <b>METAR</b> = the latest hourly reading (current temp)<br>
       &nbsp;&nbsp;• <b>TODAY-MAX</b> = the highest temp observed at the station so far today</p>
    <p>After the daily peak, <code>METAR</code> falls but <code>TODAY-MAX</code> stays at the peak — that's the running floor of where the day's high will resolve.</p>
  </div>

  <h2>How the model combines them</h2>
  <p>Weighted mean: <b>ECMWF + GFS/HRRR at 70% combined</b> (50/50 per source for most cities; LAX is 10/90 because ECMWF overforecasts LAX), <b>NWS at 30%</b>, then a per-city bias correction, then conditioned on TODAY-MAX as a hard lower bound (the day's high can't be below what's already been observed).</p>

  <div class="tldr">
    <b>TL;DR:</b> METAR / TODAY-MAX = what's actually happening. ECMWF + GFS/HRRR + NWS = what's predicted to happen. Forecasts refresh through the day on different cadences; METAR refreshes every hour with a real measurement.
  </div>

</body></html>
"""


STATIC_PAGES = {
    "/":         DASHBOARD_HTML.encode(),
    "/schedule": SCHEDULE_HTML.encode(),
    "/sources":  SOURCES_HTML.encode(),
}


MANIFEST_JSON = json.dumps({
    "name": "Weatherbot",
    "short_name": "Weatherbot",
    "description": "Kalshi temperature predictor",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0f1115",
    "theme_color": "#0f1115",
    "icons": [
        {"src": "/icon.png", "sizes": "512x512", "type": "image/png",
         "purpose": "any maskable"},
    ],
}).encode()

_ICON_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
try:
    with open(_ICON_PATH, "rb") as _f:
        ICON_PNG = _f.read()
except FileNotFoundError:
    ICON_PNG = b""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s\n" % (fmt % args))

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        key = "/" if path.startswith("/index") else path
        page = STATIC_PAGES.get(key)
        if page is not None:
            self._send(200, "text/html; charset=utf-8", page)
            return

        if path == "/manifest.webmanifest":
            self._send(200, "application/manifest+json", MANIFEST_JSON)
            return

        if path == "/icon.png":
            if ICON_PNG:
                self._send(200, "image/png", ICON_PNG)
            else:
                self._send(404, "text/plain", b"icon not found")
            return

        if path == "/api/markets":
            try:
                data = get_dashboard_data(force=qs.get("refresh", [""])[0] == "1")
                self._send(200, "application/json", json.dumps(data).encode())
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}).encode())
            return

        if path == "/api/predict":
            ticker = (qs.get("event", [""])[0] or "").strip().upper()
            if not ticker:
                self._send(400, "application/json",
                           json.dumps({"error": "missing ?event=TICKER"}).encode())
                return
            try:
                result = predict_event(ticker)
                self._send(200, "application/json", json.dumps(result).encode())
            except LookupError_ as e:
                self._send(404, "application/json",
                           json.dumps({"error": str(e)}).encode())
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}).encode())
            return

        if path == "/api/backtest":
            series = (qs.get("series", [""])[0] or "").strip().upper() or None
            events = (qs.get("events", [""])[0] or "").strip() or None
            try:
                days = int(qs.get("days", ["30"])[0])
                lead_hours = float(qs.get("lead_hours", ["24"])[0])
                threshold = float(qs.get("threshold", ["0"])[0])
            except ValueError as e:
                self._send(400, "application/json",
                           json.dumps({"error": f"bad number param: {e}"}).encode())
                return
            try:
                tickers = resolve_backtest_tickers(events, series, days,
                                                   all_series=(not series and not events))
                if not tickers:
                    self._send(400, "application/json",
                               json.dumps({"error": "no events resolved — provide series or events"}).encode())
                    return
                results, summaries = run_backtest(tickers, lead_hours, threshold)
                payload = {
                    "params": {"series": series, "events": events, "days": days,
                               "lead_hours": lead_hours, "threshold": threshold,
                               "n_events": len(tickers)},
                    "results": results,
                    "summaries": summaries,
                }
                self._send(200, "application/json", json.dumps(payload).encode())
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}).encode())
            return

        self._send(404, "text/plain", b"not found")

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass


def cmd_serve(args):
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"kalshi_temp dashboard: http://localhost:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", flush=True)


# ---------- predict ----------

class LookupError_(Exception):
    pass


def fetch_event_and_markets(event_ticker):
    """Look up one event + its markets by ticker. Raises LookupError_ on failure."""
    series = event_ticker.split("-")[0]
    try:
        evs = kalshi_get("/events",
                         {"series_ticker": series}).get("events", []) or []
    except Exception as e:
        raise LookupError_(f"failed to fetch series {series}: {e}")
    ev = next((e for e in evs if e["event_ticker"] == event_ticker), None)
    if ev is None:
        for status in ("settled", "closed"):
            try:
                evs = kalshi_get("/events",
                                 {"series_ticker": series, "status": status, "limit": 200}
                                 ).get("events", []) or []
                ev = next((e for e in evs if e["event_ticker"] == event_ticker), None)
                if ev:
                    break
            except Exception:
                continue
    if ev is None:
        raise LookupError_(f"event not found: {event_ticker}")
    try:
        markets = kalshi_get("/markets",
                             {"event_ticker": event_ticker, "limit": 200}
                             ).get("markets", []) or []
    except Exception as e:
        raise LookupError_(f"failed to fetch markets for {event_ticker}: {e}")
    return ev, markets


SANITY_MARKET_CONFIDENT_YES = 0.85   # if yes_ask >= this, market is highly confident YES
SANITY_MODEL_LOW_PROB       = 0.40   # if model_prob <= this, model strongly disagrees
MIN_BEST_EV                 = 0.05   # hide 'best' suggestions whose edge is below 5¢


def _sanity_keep_no(market):
    """Suppress 'Best EV NO' suggestions when betting against a highly confident market.

    Real-world signal: when a market is at >= $0.85 YES hours before close, the
    market usually has near-term observations (afternoon METAR, observed peak)
    that a static forecast model can't see. Our model thinking 'NO is +50¢ EV'
    is almost always wrong in that regime. Skip those bets.
    """
    yes_ask = market.get("yes_ask")
    prob = market.get("prob")
    if yes_ask is None or prob is None:
        return True
    if yes_ask >= SANITY_MARKET_CONFIDENT_YES and prob <= SANITY_MODEL_LOW_PROB:
        return False
    return True


def _log_nws_snapshot(data):
    """Append one JSONL row capturing forecasts at decision time for future NWS audit."""
    try:
        f = data["forecasts"]
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_ticker": data["event_ticker"],
            "target_date": data["target_date"],
            "station": data["station"],
            "ecmwf": f.get("ecmwf"),
            "gfs": f.get("gfs"),
            "nws": f.get("nws"),
            "metar": f.get("metar"),
            "today_max": f.get("today_max"),
            "model_mu": (data.get("model") or {}).get("mu"),
        }
        with open(NWS_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception as e:
        print(f"[nws-log] {e}", file=sys.stderr)


def predict_event(event_ticker):
    """Return a JSON-serializable prediction summary for an event ticker."""
    ev, markets = fetch_event_and_markets(event_ticker)
    data = build_event_data(ev, markets)
    if data is None:
        raise LookupError_(f"unsupported series: {ev['series_ticker']}")
    _log_nws_snapshot(data)
    probs = [m for m in data["markets"] if m.get("prob") is not None]
    return {
        "event_ticker": data["event_ticker"],
        "title": data["title"],
        "target_date": data["target_date"],
        "station": data["station"],
        "forecasts": data["forecasts"],
        "model": data["model"],
        "settled": data["settled"],
        "settled_bucket": data["settled_bucket"],
        "highest_probability": (max(probs, key=lambda m: m["prob"]) if probs else None),
        "best_ev_yes": max((m for m in data["markets"]
                            if m.get("ev_yes") is not None and m["ev_yes"] >= MIN_BEST_EV),
                           key=lambda m: m["ev_yes"], default=None),
        "best_ev_no":  max((m for m in data["markets"]
                            if m.get("ev_no") is not None and m["ev_no"] >= MIN_BEST_EV
                            and _sanity_keep_no(m)),
                           key=lambda m: m["ev_no"],  default=None),
        "markets": data["markets"],
    }


def cmd_predict(args):
    try:
        ev, markets = fetch_event_and_markets(args.event_ticker)
    except LookupError_ as e:
        sys.exit(str(e))
    data = build_event_data(ev, markets)
    if data is None:
        sys.exit(f"unsupported series: {ev['series_ticker']}")

    probs = [m for m in data["markets"] if m.get("prob") is not None]
    f = data["forecasts"]

    if args.json:
        out = {
            "event_ticker": data["event_ticker"],
            "title": data["title"],
            "target_date": data["target_date"],
            "station": data["station"],
            "forecasts": f,
            "model": data["model"],
            "settled": data["settled"],
            "settled_bucket": data["settled_bucket"],
            "highest_probability": (max(probs, key=lambda m: m["prob"]) if probs else None),
            "best_ev_yes": (max((m for m in data["markets"] if m.get("ev_yes") is not None),
                                key=lambda m: m["ev_yes"], default=None)),
            "best_ev_no":  (max((m for m in data["markets"] if m.get("ev_no")  is not None),
                                key=lambda m: m["ev_no"],  default=None)),
            "markets": data["markets"],
        }
        print(json.dumps(out, indent=2))
        return

    print(f"Event:    {data['title']}")
    print(f"Resolves: {data['target_date']} at {data['station']}")
    print(f"Forecasts (°F): ECMWF {_fmtf(f['ecmwf'])}  GFS/HRRR {_fmtf(f['gfs'])}  "
          f"NWS {_fmtf(f['nws'])}  METAR {_fmtf(f['metar'])}")
    m = data["model"]
    if m.get("mu") is not None:
        print(f"Model: mu={m['mu']:.2f}°F  sigma={m['sigma']:.2f}°F  sources={m.get('sources')}")
    else:
        print("Model: unavailable (no forecast sources" +
              (" — event settled" if data["settled"] else "") + ")")
    if data["settled"]:
        print(f"SETTLED: {data['settled_bucket']}")
        return
    if not probs:
        return

    top = max(probs, key=lambda m: m["prob"])
    print()
    print(f"Highest-probability bucket: {top['subtitle']}  ({top['prob']*100:.1f}%)")
    print(f"  ticker:   {top['ticker']}")
    print(f"  YES ask:  ${_money(top['yes_ask'])}    EV: {_signed(top['ev_yes'])}")
    print(f"  NO  ask:  ${_money(top['no_ask'])}     EV: {_signed(top['ev_no'])}")

    best_yes = max((m for m in data["markets"] if m.get("ev_yes") is not None),
                   key=lambda m: m["ev_yes"], default=None)
    best_no = max((m for m in data["markets"] if m.get("ev_no") is not None),
                  key=lambda m: m["ev_no"], default=None)
    if best_yes and best_yes["ticker"] != top["ticker"]:
        print(f"Best EV YES: {best_yes['subtitle']} @ ${_money(best_yes['yes_ask'])} "
              f"-> {_signed(best_yes['ev_yes'])} (model {best_yes['prob']*100:.1f}%)")
    if best_no:
        print(f"Best EV NO:  {best_no['subtitle']} @ ${_money(best_no['no_ask'])} "
              f"-> {_signed(best_no['ev_no'])} (model {(1-best_no['prob'])*100:.1f}% no)")


def _fmtf(v):  return "—" if v is None else f"{v:.1f}"
def _money(v): return "—" if v is None else f"{v:.2f}"
def _signed(v):
    if v is None: return "—"
    return f"{'+' if v >= 0 else ''}{v*100:.1f}c"


# ---------- backtest ----------

def list_events_for_series(series, days, status_options=("settled",)):
    """Pull recent events (settled by default) for a series within `days` lookback."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out, cursor = [], None
    for status in status_options:
        cursor = None
        while True:
            params = {"series_ticker": series, "status": status, "limit": 200}
            if cursor:
                params["cursor"] = cursor
            try:
                resp = kalshi_get("/events", params)
            except Exception as e:
                print(f"[kalshi] events {series} {status}: {e}", file=sys.stderr)
                break
            for ev in resp.get("events", []) or []:
                strike = None
                if ev.get("strike_date"):
                    try:
                        strike = datetime.fromisoformat(ev["strike_date"].replace("Z", "+00:00"))
                    except Exception:
                        strike = None
                if strike is None:
                    # some series omit strike_date — fall back to event ticker date
                    try:
                        d = datetime.strptime(event_local_date(ev), "%Y-%m-%d")
                        strike = d.replace(tzinfo=timezone.utc) + timedelta(days=1)
                    except Exception:
                        continue
                if strike >= cutoff:
                    out.append(ev)
            cursor = resp.get("cursor")
            if not cursor:
                break
    # dedupe by event_ticker, keep oldest -> newest
    seen, unique = set(), []
    for ev in sorted(out, key=lambda e: e.get("strike_date") or event_local_date(e)):
        if ev["event_ticker"] not in seen:
            seen.add(ev["event_ticker"])
            unique.append(ev)
    return unique


def backtest_one_event(event_ticker, lead_hours, threshold):
    """Run both YES (highest model-prob bucket) and NO (best-EV-NO with sanity cap)
    strategies on a single settled event. Returns one dict with 'yes' and 'no'
    sub-outcomes, or {'skipped': reason} if the event is unusable.

    lead_hours <= 0 means "use DEFAULT_LEAD_HOURS".
    """
    series = event_ticker.split("-")[0]
    city = CITIES.get(series)
    if not city:
        return {"event": event_ticker, "skipped": "unsupported_series"}
    if lead_hours <= 0:
        lead_hours = DEFAULT_LEAD_HOURS
    try:
        markets = kalshi_get("/markets", {"event_ticker": event_ticker, "limit": 200}
                             ).get("markets", []) or []
    except Exception as e:
        return {"event": event_ticker, "skipped": f"market_fetch:{e}"}
    if not markets:
        return {"event": event_ticker, "skipped": "no_markets"}

    winner = next((m for m in markets if m.get("result") == "yes"), None)
    if winner is None:
        return {"event": event_ticker, "skipped": "not_settled"}

    target_date = event_local_date({"event_ticker": event_ticker,
                                    "strike_date": markets[0].get("close_time", "")})

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_ec  = pool.submit(fetch_open_meteo, city["lat"], city["lon"], target_date,
                            "ecmwf_ifs025", historical=True)
        f_gfs = pool.submit(fetch_open_meteo, city["lat"], city["lon"], target_date,
                            "gfs_seamless", historical=True)
        ecmwf, gfs = f_ec.result(), f_gfs.result()

    sources = [v for v in (ecmwf, gfs) if v is not None]
    if not sources:
        return {"event": event_ticker, "skipped": "no_historical_forecast"}

    mu_raw = weighted_mean(series, ecmwf, gfs)
    if mu_raw is None:
        mu_raw = sum(sources) / len(sources)
    mu = mu_raw - BIAS.get(series, 0.0)
    spread = statistics.pstdev(sources) if len(sources) > 1 else 0.0
    sigma = math.sqrt(BASE_SIGMA ** 2 + spread ** 2)

    ranked = []
    for m in markets:
        p = bucket_probability(m, mu, sigma)
        if p is not None:
            ranked.append((m, p))
    if not ranked:
        return {"event": event_ticker, "skipped": "no_probability"}

    try:
        close_dt = datetime.fromisoformat(markets[0]["close_time"].replace("Z", "+00:00"))
    except Exception:
        return {"event": event_ticker, "skipped": "no_close_time"}
    decision_ts = int(close_dt.timestamp() - lead_hours * 3600)
    winner_ticker = winner["ticker"]

    # Fetch T-lead_hours prices for every bucket (shared by both strategies)
    bucket_prices = {}
    for m, _ in ranked:
        bucket_prices[m["ticker"]] = yes_prices_at(series, m["ticker"], decision_ts)

    yes_out = _eval_yes_strategy(ranked, threshold, bucket_prices, winner_ticker)
    no_out  = _eval_no_strategy(ranked, threshold, bucket_prices, winner_ticker)

    return {
        "event": event_ticker,
        "target_date": target_date,
        "winner_bucket": winner.get("subtitle") or synth_subtitle(winner) or "?",
        "ecmwf": ecmwf, "gfs": gfs, "mu": mu, "sigma": sigma,
        "yes": yes_out, "no": no_out,
    }


def _eval_yes_strategy(ranked, threshold, bucket_prices, winner_ticker):
    """Pick highest model-prob bucket; gate by P_yes >= threshold; entry yes_ask."""
    sorted_yes = sorted(ranked, key=lambda x: x[1], reverse=True)
    pick, p = sorted_yes[0]
    if p < threshold:
        return {"skipped": f"below_threshold(P={p:.2f})"}
    yes_ask, _ = bucket_prices.get(pick["ticker"], (None, None))
    if yes_ask is None or yes_ask <= 0 or yes_ask >= 1:
        return {"skipped": "no_price"}
    won = pick["ticker"] == winner_ticker
    pnl = (1.0 - yes_ask) if won else -yes_ask
    return {
        "picked_ticker": pick["ticker"],
        "picked_bucket": pick.get("subtitle") or synth_subtitle(pick) or "?",
        "model_prob": p,
        "entry_price": yes_ask,
        "won": won, "pnl": pnl,
    }


def _eval_no_strategy(ranked, threshold, bucket_prices, winner_ticker):
    """Pick best-EV-NO bucket using T-lead prices (mirrors the dashboard's 'Best EV NO').

    For each bucket:
      - Apply sanity cap using T-lead yes_ask (market consensus at decision time)
      - Compute EV_NO = (1 - p_yes_model) - no_ask, where no_ask ≈ 1 - yes_bid
      - Gate by P_no >= threshold and EV_NO >= MIN_BEST_EV
    Pick the bucket with the highest EV_NO. Win = bucket != winner.
    """
    candidates = []
    for m, p in ranked:
        yes_ask, yes_bid = bucket_prices.get(m["ticker"], (None, None))
        if yes_ask is None or yes_bid is None:
            continue
        if yes_ask >= SANITY_MARKET_CONFIDENT_YES and p <= SANITY_MODEL_LOW_PROB:
            continue
        if (1 - p) < threshold:
            continue
        no_ask = 1.0 - yes_bid
        if no_ask <= 0 or no_ask >= 1:
            continue
        ev_no = (1 - p) - no_ask
        if ev_no < MIN_BEST_EV:
            continue
        candidates.append((m, p, no_ask, ev_no))
    if not candidates:
        return {"skipped": "no_qualifying_no_bet"}
    candidates.sort(key=lambda x: x[3], reverse=True)
    pick, p_yes, no_ask, ev_no = candidates[0]
    won = pick["ticker"] != winner_ticker
    pnl = (1.0 - no_ask) if won else -no_ask
    return {
        "picked_ticker": pick["ticker"],
        "picked_bucket": pick.get("subtitle") or synth_subtitle(pick) or "?",
        "model_prob_yes": p_yes,
        "model_prob_no": 1 - p_yes,
        "ev_at_entry": ev_no,
        "entry_price": no_ask,
        "won": won, "pnl": pnl,
    }


def run_backtest(tickers, lead_hours, threshold, *, on_progress=None):
    """Run backtest for both YES and NO strategies. Returns (results, summaries)
    where summaries = {'yes': {...}, 'no': {...}}.
    """
    results = []
    for i, t in enumerate(tickers, 1):
        r = backtest_one_event(t, lead_hours, threshold)
        results.append(r)
        if on_progress is not None:
            on_progress(i, len(tickers), r)

    def _bucket(r, strat):
        return r.get(strat) if "skipped" not in r else None

    yes_bets, yes_skips = [], {}
    no_bets,  no_skips  = [], {}
    for r in results:
        if "skipped" in r:
            k = r["skipped"].split("(")[0]
            yes_skips[k] = yes_skips.get(k, 0) + 1
            no_skips[k]  = no_skips.get(k, 0) + 1
            continue
        yo = _bucket(r, "yes")
        no = _bucket(r, "no")
        if yo and "skipped" in yo:
            k = yo["skipped"].split("(")[0]
            yes_skips[k] = yes_skips.get(k, 0) + 1
        elif yo:
            yes_bets.append(yo)
        if no and "skipped" in no:
            k = no["skipped"].split("(")[0]
            no_skips[k] = no_skips.get(k, 0) + 1
        elif no:
            no_bets.append(no)

    yes_summary = _backtest_summary(yes_bets)
    yes_summary["skip_reasons"] = yes_skips
    no_summary = _backtest_summary(no_bets)
    no_summary["skip_reasons"] = no_skips
    return results, {"yes": yes_summary, "no": no_summary}


def resolve_backtest_tickers(events_str=None, series=None, days=30, *, all_series=False):
    if events_str:
        return [t.strip() for t in events_str.split(",") if t.strip()]
    if series:
        return [e["event_ticker"] for e in list_events_for_series(series, days)]
    if all_series:
        tickers = []
        for s in CITIES:
            try:
                tickers.extend(e["event_ticker"] for e in list_events_for_series(s, days))
            except Exception as e:
                print(f"[backtest] {s}: {e}", file=sys.stderr)
        return tickers
    return []


def cmd_backtest(args):
    tickers = resolve_backtest_tickers(args.events, args.series, args.days)
    if not tickers and not (args.events or args.series):
        sys.exit("backtest requires --events OR --series")
    if not tickers:
        sys.exit("no events to backtest")

    print(f"Backtesting {len(tickers)} event(s) at T-{args.lead_hours}h "
          f"(threshold={args.threshold:.2f})", file=sys.stderr)

    def report(i, n, r):
        if args.json:
            return
        if "skipped" in r:
            print(f"  [{i}/{n}] {r['event']}  SKIPPED ({r['skipped']})")
            return
        winner = r["winner_bucket"]
        def fmt_leg(leg):
            if leg is None: return "—"
            if "skipped" in leg:
                return f"skip({leg['skipped'][:18]})"
            tag = "WIN " if leg["won"] else "LOSS"
            return f"{tag} {leg['picked_bucket']:<14} entry=${leg['entry_price']:.2f} pnl={leg['pnl']:+.3f}"
        print(f"  [{i}/{n}] {r['event']}  winner={winner:<14}")
        print(f"          YES (high temp): {fmt_leg(r.get('yes'))}")
        print(f"          NO  (best EV):   {fmt_leg(r.get('no'))}")

    results, summaries = run_backtest(tickers, args.lead_hours, args.threshold,
                                      on_progress=report)
    summary = summaries["yes"]  # legacy name for the YES summary below
    no_summary = summaries["no"]

    if args.json:
        print(json.dumps({"results": results, "summaries": summaries}, indent=2))
        return

    print()
    print("=" * 70)
    print(f"Events tested: {len(results)}")
    for label, key in (("YES strategy (high temp)", "yes"),
                       ("NO strategy (best EV)",    "no")):
        s = summaries[key]
        print()
        print(f"  {label}")
        print(f"    Bets placed:     {s['bets']}")
        if s['bets']:
            print(f"    Wins:            {s['wins']} ({s['win_rate']*100:.1f}%)")
            print(f"    Total P/L:       ${s['total_pnl']:+.3f}")
            print(f"    Avg P/L per bet: ${s['avg_pnl']:+.4f}")
            print(f"    Max drawdown:    ${s['max_drawdown']:.3f}")
            print(f"    Best/worst:      ${s['best_win']:+.3f} / ${s['worst_loss']:+.3f}")
        if s.get("skip_reasons"):
            top = sorted(s["skip_reasons"].items(), key=lambda x: -x[1])[:5]
            print(f"    Skipped:         " + ", ".join(f"{n}× {r}" for r, n in top))


def _backtest_summary(bets):
    skipped = {}
    if not bets:
        return {"bets": 0, "wins": 0, "win_rate": 0.0, "total_pnl": 0.0,
                "avg_pnl": 0.0, "max_drawdown": 0.0, "best_win": 0.0,
                "worst_loss": 0.0, "skip_reasons": skipped}
    wins = sum(1 for r in bets if r["won"])
    pnls = [r["pnl"] for r in bets]
    total = sum(pnls)
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "bets": len(bets),
        "wins": wins,
        "win_rate": wins / len(bets),
        "total_pnl": total,
        "avg_pnl": total / len(bets),
        "max_drawdown": max_dd,
        "best_win": max(pnls),
        "worst_loss": min(pnls),
    }


def main():
    p = argparse.ArgumentParser(prog="kalshi_temp")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the local dashboard HTTP server")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    pr = sub.add_parser("predict", help="run the model on one event ticker")
    pr.add_argument("event_ticker", help="e.g. KXHIGHNY-26MAY13")
    pr.add_argument("--json", action="store_true", help="emit JSON instead of text")
    pr.set_defaults(func=cmd_predict)

    bt = sub.add_parser("backtest", help="backtest the model against settled events")
    bt.add_argument("--series", help="series ticker, e.g. KXHIGHNY")
    bt.add_argument("--days", type=int, default=30, help="lookback window in days (default 30)")
    bt.add_argument("--events", help="comma-separated event tickers (overrides --series)")
    bt.add_argument("--lead-hours", dest="lead_hours", type=float, default=0.0,
                    help="hours before market close to use as entry time; "
                         "0 = auto (use DEFAULT_LEAD_HOURS)")
    bt.add_argument("--threshold", type=float, default=0.0,
                    help="min model probability required to place a bet (default 0)")
    bt.add_argument("--json", action="store_true", help="emit JSON instead of text")
    bt.set_defaults(func=cmd_backtest)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
