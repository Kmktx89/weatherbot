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
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

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

# Per-city mean error of (ECMWF+GFS)/2 vs Kalshi-reported winning bucket midpoint,
# measured over 60 days of settled events. bias = forecast - actual.
# To debias, compute mu_corrected = mu_raw - BIAS[series].
BIAS = {
    "KXHIGHNY":   -0.53,
    "KXHIGHCHI":  -1.16,
    "KXHIGHMIA":  -2.07,
    "KXHIGHLAX":  +2.44,
    "KXHIGHDEN":  -0.81,
    "KXHIGHAUS":  -2.08,
    "KXHIGHPHIL": -1.43,
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


def yes_ask_at(series, ticker, decision_ts):
    """YES ask price at decision_ts (or the closest bar ≤ that time)."""
    window = 6 * 3600
    bars = fetch_kalshi_candlestick(series, ticker,
                                    decision_ts - window, decision_ts + 600, 60)
    best = None
    for b in bars:
        if b["end_period_ts"] <= decision_ts + 600:
            best = b
    if best:
        return to_float((best.get("yes_ask") or {}).get("close_dollars"))
    return None


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
                        params={"ids": icao, "format": "json", "hoursBeforeNow": hours},
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
<title>Kalshi Temp Predictor</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         max-width: 1400px; margin: 0 auto; padding: 1rem;
         background: #0f1115; color: #d8dde8; }
  h1 { font-size: 1.3rem; margin: 0 0 1rem; display: flex; gap: 0.75rem; align-items: center; }
  h1 small { color: #6b7280; font-weight: normal; font-size: 0.8rem; }
  .event { background: #1a1d24; border: 1px solid #2a2e38; border-radius: 8px;
           padding: 1rem; margin-bottom: 1.25rem; }
  .event h2 { font-size: 1rem; margin: 0 0 0.4rem; }
  .summary { display: flex; gap: 1.25rem; flex-wrap: wrap; font-size: 0.82rem; color: #a0a8b8; }
  .summary b { color: #e6ebf5; }
  table { width: 100%; border-collapse: collapse; margin-top: 0.75rem; font-size: 0.85rem; }
  th, td { padding: 0.35rem 0.5rem; text-align: right; border-bottom: 1px solid #232730; }
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
</style>
</head>
<body>
<h1>Kalshi temperature predictor
  <small id="ts"></small>
  <button id="btn" onclick="refresh(true)">Refresh</button>
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

async function refresh(force) {
  const btn = document.getElementById('btn');
  btn.disabled = true; btn.textContent = 'Loading…';
  document.getElementById('err').textContent = '';
  try {
    const r = await fetch('/api/markets' + (force ? '?refresh=1' : ''));
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    render(data);
  } catch (e) {
    document.getElementById('err').textContent = 'Error: ' + e.message;
  } finally {
    btn.disabled = false; btn.textContent = 'Refresh';
  }
}

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
    html += `<table><thead><tr>
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
    html += `</tbody></table>`;
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
  html += `<span class="kv">METAR <b>${fmt(f.metar)}</b></span></div>`;
  if (m.mu != null)
    html += `<div class="kv">Model: μ <b>${m.mu.toFixed(1)}°</b> σ <b>${m.sigma.toFixed(1)}°</b></div>`;
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
  if (!series && !events) { out.textContent = 'Choose a series or list events.'; return; }
  const btn = e.target.querySelector('button[type=submit]');
  btn.disabled = true;
  out.textContent = 'Running… (this can take 10–30s for 10+ events)';
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
  const s = d.summary || {};
  let html = `<div><b>${s.bets || 0}</b> bets / ${d.params.n_events} events · `
           + `<b>${s.wins || 0}</b> wins (${((s.win_rate||0)*100).toFixed(1)}%) · `
           + `P/L <b class="${(s.total_pnl||0) >= 0 ? 'pos' : 'neg'}">$${(s.total_pnl||0).toFixed(3)}</b> · `
           + `Max DD <b>$${(s.max_drawdown||0).toFixed(3)}</b> · `
           + `avg $${(s.avg_pnl||0).toFixed(4)}</div>`;
  if (s.skip_reasons && Object.keys(s.skip_reasons).length) {
    html += `<div class="dim">Skipped: ` +
      Object.entries(s.skip_reasons).map(([k,n]) => `${n}× ${k}`).join(' · ') + `</div>`;
  }
  html += `<table><thead><tr><th>Event</th><th>Pick</th><th>Winner</th><th>P</th>`
        + `<th>Entry</th><th>P/L</th></tr></thead><tbody>`;
  for (const r of d.results) {
    if (r.skipped) {
      html += `<tr><td>${r.event}</td><td colspan="5" class="dim">skipped: ${r.skipped}</td></tr>`;
    } else {
      const won = r.won;
      html += `<tr><td>${r.event}</td>`
            + `<td class="${won ? 'pos' : 'neg'}">${r.picked_bucket}</td>`
            + `<td>${r.winner_bucket}</td>`
            + `<td>${(r.model_prob*100).toFixed(1)}%</td>`
            + `<td>$${r.entry_price.toFixed(2)}</td>`
            + `<td class="${r.pnl >= 0 ? 'pos' : 'neg'}">${r.pnl >= 0 ? '+' : ''}${r.pnl.toFixed(3)}</td>`
            + `</tr>`;
    }
  }
  html += `</tbody></table>`;
  out.innerHTML = html;
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s\n" % (fmt % args))

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)

        if path == "/" or path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode())
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
                results, summary = run_backtest(tickers, lead_hours, threshold)
                payload = {
                    "params": {"series": series, "events": events, "days": days,
                               "lead_hours": lead_hours, "threshold": threshold,
                               "n_events": len(tickers)},
                    "results": results,
                    "summary": summary,
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
        self.wfile.write(body)


def cmd_serve(args):
    server = HTTPServer(("0.0.0.0", args.port), Handler)
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


def predict_event(event_ticker):
    """Return a JSON-serializable prediction summary for an event ticker."""
    ev, markets = fetch_event_and_markets(event_ticker)
    data = build_event_data(ev, markets)
    if data is None:
        raise LookupError_(f"unsupported series: {ev['series_ticker']}")
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
        "best_ev_yes": max((m for m in data["markets"] if m.get("ev_yes") is not None),
                           key=lambda m: m["ev_yes"], default=None),
        "best_ev_no":  max((m for m in data["markets"]
                            if m.get("ev_no") is not None and _sanity_keep_no(m)),
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
    """Returns a dict describing the simulated bet for one event, or {'skipped': reason}."""
    series = event_ticker.split("-")[0]
    city = CITIES.get(series)
    if not city:
        return {"event": event_ticker, "skipped": "unsupported_series"}
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
    ranked.sort(key=lambda x: x[1], reverse=True)
    pick, pick_p = ranked[0]
    if pick_p < threshold:
        return {"event": event_ticker, "skipped": f"below_threshold({pick_p:.2f})"}

    # decision time = close_time - lead_hours
    try:
        close_dt = datetime.fromisoformat(pick["close_time"].replace("Z", "+00:00"))
    except Exception:
        return {"event": event_ticker, "skipped": "no_close_time"}
    decision_ts = int(close_dt.timestamp() - lead_hours * 3600)

    price = yes_ask_at(series, pick["ticker"], decision_ts)
    if price is None or price <= 0 or price >= 1.0:
        return {"event": event_ticker, "skipped": "no_price"}

    won = pick.get("result") == "yes"
    pnl = (1.0 - price) if won else (-price)
    return {
        "event": event_ticker,
        "target_date": target_date,
        "winner_bucket": winner.get("subtitle") or synth_subtitle(winner) or "?",
        "picked_ticker": pick["ticker"],
        "picked_bucket": pick.get("subtitle") or synth_subtitle(pick) or "?",
        "model_prob": pick_p,
        "ecmwf": ecmwf, "gfs": gfs,
        "mu": mu, "sigma": sigma,
        "entry_price": price,
        "won": won,
        "pnl": pnl,
    }


def run_backtest(tickers, lead_hours, threshold, *, on_progress=None):
    """Core backtest loop. on_progress(i, n, result) callback after each event."""
    results = []
    for i, t in enumerate(tickers, 1):
        r = backtest_one_event(t, lead_hours, threshold)
        results.append(r)
        if on_progress is not None:
            on_progress(i, len(tickers), r)
    bets = [r for r in results if "skipped" not in r]
    summary = _backtest_summary(bets)
    skip_reasons = {}
    for r in results:
        if "skipped" in r:
            key = r["skipped"].split("(")[0]
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
    summary["skip_reasons"] = skip_reasons
    return results, summary


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
        else:
            tag = "WIN " if r["won"] else "LOSS"
            print(f"  [{i}/{n}] {r['event']}  {tag}  "
                  f"pick={r['picked_bucket']:<14} winner={r['winner_bucket']:<14} "
                  f"P={r['model_prob']*100:5.1f}%  entry=${r['entry_price']:.2f}  "
                  f"pnl={r['pnl']:+.3f}")

    results, summary = run_backtest(tickers, args.lead_hours, args.threshold,
                                    on_progress=report)

    if args.json:
        print(json.dumps({"results": results, "summary": summary}, indent=2))
        return

    print()
    print("=" * 60)
    print(f"Events tested:    {len(results)}")
    print(f"Bets placed:      {summary['bets']}")
    print(f"Wins:             {summary['wins']} ({summary['win_rate']*100:.1f}%)")
    print(f"Total P/L:        ${summary['total_pnl']:+.3f}")
    print(f"Avg P/L per bet:  ${summary['avg_pnl']:+.4f}")
    print(f"Max drawdown:     ${summary['max_drawdown']:.3f}")
    print(f"Best win:         ${summary['best_win']:+.3f}")
    print(f"Worst loss:       ${summary['worst_loss']:+.3f}")
    if summary.get("skip_reasons"):
        print()
        print("Skipped:")
        for reason, n in sorted(summary["skip_reasons"].items(), key=lambda x: -x[1]):
            print(f"  {n:>3} × {reason}")


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
    bt.add_argument("--lead-hours", dest="lead_hours", type=float, default=24.0,
                    help="hours before market close to use as entry time (default 24)")
    bt.add_argument("--threshold", type=float, default=0.0,
                    help="min model probability required to place a bet (default 0)")
    bt.add_argument("--json", action="store_true", help="emit JSON instead of text")
    bt.set_defaults(func=cmd_backtest)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
