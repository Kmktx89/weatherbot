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
from pathlib import Path

import requests

# Canonical thresholds (single source of truth — shared with the backtest model,
# lab.replay, and the daily health scan). Re-exported as module attributes below
# so kt.MIN_BEST_EV / kt.BASE_SIGMA etc. stay valid for existing callers.
from wb_thresholds import (
    BASE_SIGMA,
    MIN_BEST_EV,
    MIN_PRINTED_NO,
    NO_HAIRCUT,
    SANITY_MARKET_CONFIDENT_YES,
    SANITY_MODEL_LOW_PROB,
)


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
# BASE_SIGMA (°F floor on forecast uncertainty) is imported from wb_thresholds.
# Graduated 2026-05-19 from 2.0 -> 1.0 based on the sigma sweep in
# docs/superpowers/reports/2026-05-19-sigma-sweep-and-reading-guide.md:
# +20% total backtest PnL, NO win rate 70 -> 84, calibration gaps shrink
# on both sides. Per-city forecast sd averages ~1.55; 2.0 overstated
# uncertainty. LIVE_TODAY in lab/configs.py inherits this via _kt.BASE_SIGMA.
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
    """GET against Kalshi with simple rate-limit, 429 retry, and
    connection/DNS-error retry with backoff.

    Transient DNS failures (getaddrinfo / NameResolutionError, which
    subclasses ConnectionError) and connection timeouts were previously not
    retried here, so a single blip dropped a city's rows for the whole
    snapshot run and could block that day's T-24 card. Retry those the same
    way as a 429, re-raising the last error only if every attempt failed."""
    global _kalshi_last_call
    url = f"{KALSHI}{path}"
    last_exc = None
    for attempt in range(3):
        with _kalshi_lock:
            wait = KALSHI_MIN_GAP - (time.time() - _kalshi_last_call)
            if wait > 0:
                time.sleep(wait)
            _kalshi_last_call = time.time()
        try:
            r = session.get(url, params=params, timeout=15)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last_exc = e
            time.sleep(1.0 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(1.0 * (attempt + 1))
            continue
        r.raise_for_status()
        return r.json()
    if last_exc is not None:
        raise last_exc
    r.raise_for_status()
    return r.json()


# ---------- live-path forecast resilience: retry + on-disk cache ----------
# The forecast/observation endpoints (open-meteo, NWS, METAR) previously had a
# single 15s-timeout attempt and no caching, so a transient ConnectionReset /
# read timeout either dropped a city's input (-> None) or hung the whole loop on
# the full 15s. We add (a) tight-timeout retry with linear backoff so a blip
# recovers fast instead of hanging, and (b) a small on-disk cache so repeated
# builds (dashboard refreshes; the hourly snapshot vs interleaved /api/markets
# hits) reuse a recent fetch instead of going back to the network.
# Live Kalshi PRICES are deliberately NOT cached here — only slow-moving weather
# inputs — and TTLs stay short enough to respect settlement freshness.
FORECAST_CACHE_PATH = "forecast_cache.sqlite"
FORECAST_TTL = 1800       # open-meteo / NWS forecasts: 30 min (forecast refresh cadence)
METAR_TTL = 300           # live obs: 5 min (matches dashboard CACHE_TTL; fresh for settlement)
FORECAST_TIMEOUT = 8      # per-attempt seconds — short so a hung socket fails fast then retries
FORECAST_ATTEMPTS = 3
HIST_TIMEOUT = 15         # backtest archive can be slower; keep the original budget

_fc_lock = threading.Lock()
_forecast_cache = None     # None = uninitialised, False = disabled, else a DataCache


def _forecast_cache_get():
    """Lazily open the live forecast cache. Returns a DataCache or None (disabled).
    Lazy + guarded so importing kalshi_temp never creates a file or a cycle."""
    global _forecast_cache
    if _forecast_cache is None:
        with _fc_lock:
            if _forecast_cache is None:
                try:
                    from lab.data_cache import DataCache
                    _forecast_cache = DataCache(FORECAST_CACHE_PATH)
                except Exception as e:
                    print(f"[forecast-cache] disabled: {e}", file=sys.stderr)
                    _forecast_cache = False
    return _forecast_cache or None


def _fc_read(cache, key, ttl):
    """Cache read that degrades to a miss on any error. forecast_cache.sqlite is
    written by two live processes (serve + snapshot); a contended read must fall
    through to a network fetch, never propagate and drop a city's row."""
    try:
        return cache.get(key, ttl=ttl)
    except Exception as e:
        print(f"[forecast-cache] read failed ({key}): {e}", file=sys.stderr)
        return None


def _fc_write(cache, key, value, source, target_date):
    """Cache write that swallows errors (sqlite contention) — a failed write just
    means the next call refetches; it must never break the build."""
    try:
        cache.set(key, value, source=source, target_date=target_date)
    except Exception as e:
        print(f"[forecast-cache] write failed ({key}): {e}", file=sys.stderr)


def _get_json_retry(url, params=None, *, timeout=FORECAST_TIMEOUT,
                    attempts=FORECAST_ATTEMPTS, label=""):
    """GET -> parsed JSON, retrying only transient connection/timeout errors with
    linear backoff. Re-raises the last error if every attempt fails (callers keep
    their existing try/except so a hard failure still degrades to None)."""
    last = None
    for i in range(attempts):
        try:
            r = session.get(url, params=params, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = e
            time.sleep(0.6 * (i + 1))
    raise last


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
    # Live path uses the resilience cache; the historical/backtest path is cached
    # separately by lab.inputs, so it bypasses this one.
    cache = None if historical else _forecast_cache_get()
    ckey = f"live:open_meteo:{model}:{lat:.4f},{lon:.4f}:{target_date}"
    if cache is not None:
        hit = _fc_read(cache, ckey, FORECAST_TTL)
        if hit is not None:
            return hit.get("value")
    val = None
    try:
        data = _get_json_retry(url, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "models": model,
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "start_date": target_date,
            "end_date": target_date,
        }, timeout=(HIST_TIMEOUT if historical else FORECAST_TIMEOUT))
        daily = data.get("daily") or {}
        vals = daily.get("temperature_2m_max") or daily.get(f"temperature_2m_max_{model}") or []
        if vals and vals[0] is not None:
            val = float(vals[0])
    except Exception as e:
        print(f"[open-meteo:{model}{':hist' if historical else ''}] {e}", file=sys.stderr)
        return None
    if cache is not None and val is not None:   # never cache a failed (None) fetch
        _fc_write(cache, ckey, {"value": val}, "live:open_meteo", target_date)
    return val


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
    cache = _forecast_cache_get()
    ckey = f"live:nws:{lat:.4f},{lon:.4f}:{target_date}"
    if cache is not None:
        hit = _fc_read(cache, ckey, FORECAST_TTL)
        if hit is not None:
            return hit.get("value")
    val = None
    try:
        pts = _get_json_retry(f"{NWS}/points/{lat:.4f},{lon:.4f}", label="nws-points")
        forecast_url = pts["properties"]["forecast"]
        fc = _get_json_retry(forecast_url, label="nws-forecast")
        for p in fc["properties"]["periods"]:
            if not p.get("isDaytime"):
                continue
            if p["startTime"][:10] == target_date:
                temp = float(p["temperature"])
                if p.get("temperatureUnit") == "C":
                    temp = temp * 9 / 5 + 32
                val = temp
                break
    except Exception as e:
        print(f"[nws] {e}", file=sys.stderr)
        return None
    if cache is not None and val is not None:
        _fc_write(cache, ckey, {"value": val}, "live:nws", target_date)
    return val


def fetch_metar_temp(icao):
    cache = _forecast_cache_get()
    ckey = f"live:metar:{icao}"
    if cache is not None:
        hit = _fc_read(cache, ckey, METAR_TTL)
        if hit is not None:
            return hit.get("value")
    val = None
    try:
        data = _get_json_retry(METAR_URL, params={"ids": icao, "format": "json"},
                               label=f"metar:{icao}")
        if data and data[0].get("temp") is not None:
            val = float(data[0]["temp"]) * 9 / 5 + 32
    except Exception as e:
        print(f"[metar:{icao}] {e}", file=sys.stderr)
        return None
    if cache is not None and val is not None:
        _fc_write(cache, ckey, {"value": val}, "live:metar", None)
    return val


def fetch_metar_today_max(icao, hours=10):
    """Max temperature (°F) observed at icao in the last `hours` hours."""
    cache = _forecast_cache_get()
    ckey = f"live:metar_max:{icao}:{hours}"
    if cache is not None:
        hit = _fc_read(cache, ckey, METAR_TTL)
        if hit is not None:
            return hit.get("value")
    val = None
    try:
        data = _get_json_retry(METAR_URL,
                               params={"ids": icao, "format": "json", "hours": hours},
                               label=f"metar-window:{icao}")
        temps = [float(o["temp"]) * 9 / 5 + 32
                 for o in (data or []) if o.get("temp") is not None]
        val = max(temps) if temps else None
    except Exception as e:
        print(f"[metar-window:{icao}] {e}", file=sys.stderr)
        return None
    if cache is not None and val is not None:
        _fc_write(cache, ckey, {"value": val}, "live:metar_max", None)
    return val


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

    settled_bucket = None
    for m in markets:
        if (to_float(m.get("yes_bid_dollars")) or 0) >= 0.95:
            settled_bucket = m.get("subtitle") or synth_subtitle(m) or "?"
            break
    settled = settled_bucket is not None

    if settled:
        model = {"mu": None, "sigma": None}
        out_markets = [dict(_market_summary(m), prob=None, ev_yes=None, ev_no=None)
                       for m in markets]
    else:
        from model import ModelInputs, compute
        from lab.configs import LIVE_TODAY
        inputs = ModelInputs(
            series=series, event_ticker=ev["event_ticker"], target_date=target,
            forecasts={"ecmwf": ecmwf, "gfs": gfs, "nws": nws},
            metar_current=metar, today_max=today_max,
            markets=markets,
            decision_ts=int(time.time()),
            fetched_at=int(time.time()),
        )
        out = compute(inputs, LIVE_TODAY)
        if out.mu is None:
            model = {"mu": None, "sigma": None}
            out_markets = [dict(_market_summary(m), prob=None, ev_yes=None, ev_no=None)
                           for m in markets]
        else:
            sources_count = sum(1 for k in ("ecmwf", "gfs", "nws")
                                if forecasts.get(k) is not None)
            model = {"mu": out.mu, "mu_raw": out.mu_raw, "bias": out.bias_applied,
                     "sigma": out.sigma, "sources": sources_count,
                     "today_max": today_max, "truncation": out.truncation}
            out_markets = []
            for m in markets:
                prob = out.probs.get(m["ticker"])
                ya = to_float(m.get("yes_ask_dollars"))
                na = to_float(m.get("no_ask_dollars"))
                ev_yes = (prob - ya) if (prob is not None and ya not in (None, 0.0)) else None
                ev_no = ((1 - prob) - na) if (prob is not None and na not in (None, 0.0)) else None
                out_markets.append(dict(_market_summary(m), prob=prob,
                                        ev_yes=ev_yes, ev_no=ev_no))

            # Shadow A/B: compute candidate configs on the same inputs.
            try:
                from shadow.runner import run_shadow
                from shadow.active import ACTIVE_SHADOWS
                run_shadow(inputs, ACTIVE_SHADOWS)
            except Exception as e:
                print(f"[shadow] {e}", file=sys.stderr)

    out_markets.sort(key=lambda x: x["_sort"])
    finalize_markets(out_markets)
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
<link rel="apple-touch-icon" href="/icon.png">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Weatherbot">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
  :root {
    color-scheme: dark;
    --bg: #000;
    --bg-elev: #0a0a0a;
    --border: #1f1f1f;
    --border-strong: #2a2a2a;
    --text: #fafafa;
    --text-muted: #8a8a8a;
    --text-dim: #555;
    --pos: #00d97e;
    --neg: #ff3a3a;
  }
  body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         font-variant-numeric: tabular-nums;
         font-feature-settings: 'tnum' 1, 'cv11' 1;
         max-width: 1400px; margin: 0 auto; padding: 1rem;
         background: var(--bg); color: var(--text);
         -webkit-text-size-adjust: 100%;
         -webkit-font-smoothing: antialiased; }
  h1 { font-size: 1.5rem; font-weight: 700; letter-spacing: -0.02em;
       margin: 0 0 1rem;
       display: flex; gap: 0.75rem; align-items: baseline; flex-wrap: wrap;
       border-bottom: 2px solid var(--text); padding-bottom: 0.75rem; }
  h1 small { color: var(--text-muted); font-weight: 400; font-size: 0.78rem; }
  .event { background: var(--bg-elev); border: 1px solid var(--border);
           border-radius: 0; padding: 1rem 1.25rem; margin-bottom: 1.25rem; }
  .event h2 { font-size: 1rem; font-weight: 600; margin: 0 0 0.5rem;
              letter-spacing: -0.01em;
              border-bottom: 1px solid var(--border-strong); padding-bottom: 0.5rem; }
  .summary { display: flex; gap: 1.25rem; flex-wrap: wrap; font-size: 0.78rem;
             color: var(--text-muted); margin-bottom: 0.5rem; }
  .summary b { color: var(--text); font-weight: 600; }
  .table-wrap { overflow-x: auto; margin-top: 0.75rem; -webkit-overflow-scrolling: touch; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { padding: 0.4rem 0.55rem; text-align: right; border-bottom: 1px solid var(--border);
           white-space: nowrap; }
  th { color: var(--text-muted); font-weight: 600; font-size: 0.68rem;
       text-transform: uppercase; letter-spacing: 0.07em;
       border-bottom: 1px solid var(--text-muted); }
  th:first-child, td:first-child { text-align: left; }
  .pos { color: var(--pos); font-weight: 600; }
  .neg { color: var(--neg); font-weight: 600; }
  .dim { color: var(--text-dim); }
  .bucket-name { font-weight: 500; color: var(--text); }
  .prob-cell { font-weight: 600; background-clip: padding-box; }
  .hi-row td { border-bottom-color: var(--pos); }
  .hi-row td.bucket-name { font-weight: 700; }
  button { background: var(--text); color: var(--bg); border: 0;
           padding: 0.45rem 0.95rem; border-radius: 0; cursor: pointer;
           font: inherit; font-weight: 600; font-size: 0.85rem;
           letter-spacing: 0.02em; text-transform: uppercase; }
  button:hover { background: var(--pos); }
  button:disabled { opacity: 0.4; cursor: wait; }
  #err { color: var(--neg); font-weight: 500; font-size: 0.85rem; }
  .tools { display: grid; grid-template-columns: 1fr 2fr; gap: 1rem; margin-bottom: 1.25rem; }
  .tool { background: var(--bg-elev); border: 1px solid var(--border); padding: 0.9rem 1rem; }
  .tool h3 { font-size: 0.72rem; font-weight: 600;
             color: var(--text-muted);
             text-transform: uppercase; letter-spacing: 0.07em;
             margin: 0 0 0.75rem;
             display: flex; justify-content: space-between; align-items: center; gap: 0.75rem;
             border-bottom: 1px solid var(--border-strong); padding-bottom: 0.5rem; }
  .presets { font-size: 0.7rem; color: var(--text-muted); font-weight: 400; }
  .preset { background: transparent; color: var(--text);
            border: 1px solid var(--border-strong);
            padding: 0.25rem 0.6rem; margin-left: 0.3rem;
            font-size: 0.7rem; font-weight: 500;
            text-transform: uppercase; letter-spacing: 0.05em;
            cursor: pointer; }
  .preset:hover { background: var(--text); color: var(--bg); }
  .tool form { display: flex; flex-wrap: wrap; gap: 0.5rem 0.75rem; align-items: end; }
  .tool label { display: flex; flex-direction: column; font-size: 0.65rem;
                color: var(--text-muted); gap: 0.25rem;
                text-transform: uppercase; letter-spacing: 0.05em; font-weight: 500; }
  .tool label.wide { flex: 1 1 100%; }
  .tool input, .tool select { background: var(--bg); color: var(--text);
                              border: 1px solid var(--border-strong); border-radius: 0;
                              padding: 0.4rem 0.55rem; font: inherit; font-weight: 500;
                              font-variant-numeric: tabular-nums;
                              min-width: 7rem; }
  .tool input:focus, .tool select:focus { outline: none; border-color: var(--text); }
  .tool input[type=text] { min-width: 14rem; }
  .tool-out { margin-top: 0.75rem; font-size: 0.82rem; color: var(--text); }
  .tool-out table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
  .tool-out th, .tool-out td { padding: 0.3rem 0.5rem; text-align: right;
                                border-bottom: 1px solid var(--border); font-size: 0.78rem; }
  .tool-out th:first-child, .tool-out td:first-child { text-align: left; }
  .kv { display: inline-block; margin-right: 1.25rem; color: var(--text-muted); font-size: 0.82rem; }
  .kv b { color: var(--text); font-weight: 600; margin-left: 0.25rem; }
  @media (max-width: 900px) { .tools { grid-template-columns: 1fr; } }
  @media (max-width: 600px) {
    body { padding: 0.7rem; }
    h1 { font-size: 1.25rem; gap: 0.5rem; }
    .tool h3 { flex-direction: column; align-items: flex-start; gap: 0.25rem; }
    .presets { display: flex; flex-wrap: wrap; gap: 0.25rem; }
    .preset { margin-left: 0; padding: 0.4rem 0.7rem; font-size: 0.72rem; }
    /* 16px minimum on inputs prevents iOS Safari from auto-zooming on focus. */
    .tool input, .tool select, button { font-size: 1rem; padding: 0.55rem 0.65rem; }
    .tool input[type=text] { min-width: 0; width: 100%; }
    .summary { font-size: 0.75rem; gap: 0.5rem 1rem; }
    table { font-size: 0.78rem; }
    th, td { padding: 0.35rem 0.4rem; }
  }
</style>
</head>
<body>
<h1>Kalshi temperature predictor
  <small id="ts"></small>
  <button id="btn" onclick="refresh(true)">Refresh</button>
  <button onclick="openPane('/schedule', 'kt-schedule', 460, 460)" style="background:#2f4366;">Schedule</button>
  <button onclick="openPane('/sources', 'kt-sources', 620, 720)" style="background:#2f4366;">Sources</button>
  <button onclick="openPane('/t24', 'kt-t24', 720, 900)" style="background:#2f4366;">T-24h</button>
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

<section id="signals" style="border:2px solid #2d7;border-radius:8px;padding:10px;margin:0 0 14px 0;background:#0c1a12">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <b style="color:#2d7">Qualifying Signals — interim bar</b>
    <span id="signals-updated" style="font-size:12px;color:#888"></span>
  </div>
  <div id="signals-body" style="margin-top:8px;font-size:14px">loading…</div>
</section>

<div id="root">Loading…</div>
<script>
const fmt    = v => v == null ? '—' : v.toFixed(1) + '°';
const pct    = v => v == null ? '—' : (v * 100).toFixed(1) + '%';
const money  = v => v == null ? '—' : '$' + v.toFixed(2);
const signed = v => v == null ? '—' : (v >= 0 ? '+' : '') + (v * 100).toFixed(1) + '¢';
const cls    = v => v == null ? 'dim' : v > 0.03 ? 'pos' : v < -0.03 ? 'neg' : 'dim';

// Conditional cell formatting helpers — Bloomberg-style heat:
// prob cells get a horizontal green fill scaled to the probability;
// EV cells get a background tint whose alpha scales with |EV|.
const probBg = p => {
  if (p == null) return '';
  const pctW = Math.round(p * 100);
  const alpha = (0.06 + p * 0.28).toFixed(3);
  return ` style="background:linear-gradient(to right,rgba(0,217,126,${alpha}) ${pctW}%,transparent ${pctW}%);"`;
};
const evCellAttr = v => {
  if (v == null) return ' class="dim"';
  if (v > 0.03) {
    const a = Math.min(0.35, v * 2.0).toFixed(3);
    return ` class="pos" style="background:rgba(0,217,126,${a});"`;
  }
  if (v < -0.03) {
    const a = Math.min(0.35, -v * 2.0).toFixed(3);
    return ` class="neg" style="background:rgba(255,58,58,${a});"`;
  }
  return ' class="dim"';
};
const wrClass = wr => {
  if (wr == null) return 'dim';
  if (wr >= 0.70) return 'pos';
  if (wr <= 0.40) return 'neg';
  return '';
};

// Opens an aux page. On desktop it's a separate OS window (resizable,
// snappable, draggable). On iOS / Android / standalone PWAs, window.open
// either ignores the dimensions and breaks out of the PWA, or fails
// silently — so navigate in-tab instead. The aux pages have a "back"
// link for that path.
function openPane(path, name, w, h) {
  const isStandalone = (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches)
                    || window.navigator.standalone === true;
  const isMobile = window.innerWidth < 700 || /iPad|iPhone|iPod|Android/i.test(navigator.userAgent);
  if (isStandalone || isMobile) {
    window.location.href = path;
    return;
  }
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
      const evMax = Math.max(mk.cal_ev_yes ?? -1, mk.cal_ev_no ?? -1);
      html += `<tr class="${evMax > 0.05 ? 'hi-row' : ''}">`;
      html += `<td class="bucket-name">${mk.subtitle}</td>`;
      html += `<td class="prob-cell"${probBg(mk.prob)}>${pct(mk.prob)}</td>`;
      html += `<td>${money(mk.yes_bid)}</td>`;
      html += `<td>${money(mk.yes_ask)}</td>`;
      html += `<td>${money(mk.no_ask)}</td>`;
      html += `<td${evCellAttr(mk.cal_ev_yes)}>${signed(mk.cal_ev_yes)}</td>`;
      html += `<td${evCellAttr(mk.cal_ev_no)}>${signed(mk.cal_ev_no)}</td>`;
      html += `<td class="dim">${mk.vol_24h ? mk.vol_24h.toFixed(0) : '—'}</td>`;
      html += `</tr>`;
    }
    html += `</tbody></table></div>`;
    el.innerHTML = html;
    root.appendChild(el);
  }
}

refresh(false);

async function renderSignals() {
  try {
    const r = await fetch('/api/signals'); const d = await r.json();
    const upd = document.getElementById('signals-updated');
    const body = document.getElementById('signals-body');
    if (d.generated_at) {
      const ago = Math.round((Date.now() - new Date(d.generated_at)) / 60000);
      upd.textContent = 'Last updated: ' + new Date(d.generated_at).toLocaleString() + ' (' + ago + 'm ago)';
      upd.style.color = d.stale ? '#c33' : '#888';
    } else { upd.textContent = 'no report yet'; upd.style.color = '#c33'; }
    if (!d.picks || !d.picks.length) { body.textContent = 'No qualifying picks this hour.'; return; }
    body.innerHTML = d.picks.map(function(p){ return (
      '<div style="padding:5px 0;border-top:1px solid #234">'
      + '<b>' + p.city + ' ' + p.target_date.slice(5) + '</b> · BUY <b>' + p.side + '</b> ' + p.bucket
      + ' @ <b>' + Math.round(p.market_price*100) + 'c</b> · EV ' + Math.round(p.ev*100) + 'c'
      + ' · size <b>' + p.size_pct + '%</b> · lead ' + p.lead_hours + 'h'
      + ' <span style="font-size:11px;color:#789"> ' + p.ticker + '</span></div>'); }).join('');
  } catch (e) { var b=document.getElementById('signals-body'); if(b) b.textContent = 'signals error: ' + e; }
}
renderSignals();
setInterval(renderSignals, 60000);

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
         + `&nbsp;YES @ ${money(top.yes_ask)}  → <span class="${cls(top.cal_ev_yes)}">${signed(top.cal_ev_yes)}</span>, `
         + `NO @ ${money(top.no_ask)}  → <span class="${cls(top.cal_ev_no)}">${signed(top.cal_ev_no)}</span></div>`;
  }
  const by = d.best_ev_yes, bn = d.best_ev_no;
  if (by && (!top || by.ticker !== top.ticker))
    html += `<div>Best EV YES: ${by.subtitle} @ ${money(by.yes_ask)} → <span class="${cls(by.cal_ev_yes)}">${signed(by.cal_ev_yes)}</span> (P=${(by.cal_prob_yes*100).toFixed(1)}%)</div>`;
  if (bn)
    html += `<div>Best EV NO: ${bn.subtitle} @ ${money(bn.no_ask)} → <span class="${cls(bn.cal_ev_no)}">${signed(bn.cal_ev_no)}</span> (P_no=${(bn.cal_prob_no*100).toFixed(1)}%)</div>`;
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
    return `<div><b>${label}:</b> ${s.bets} bets · ${s.wins} wins `
         + `(<span class="${wrClass(s.win_rate)}">${(s.win_rate*100).toFixed(1)}%</span>) · `
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
<link rel="apple-touch-icon" href="/icon.png">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
  :root { color-scheme: dark;
          --bg: #000; --border: #1f1f1f; --border-strong: #2a2a2a;
          --text: #fafafa; --text-muted: #8a8a8a; --text-dim: #555;
          --pos: #00d97e; --warn: #ffd700; }
  body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         font-variant-numeric: tabular-nums;
         -webkit-font-smoothing: antialiased;
         background: var(--bg); color: var(--text); margin: 0; padding: 1rem 1.25rem; }
  h1 { font-size: 1.05rem; font-weight: 700; letter-spacing: -0.01em;
       margin: 0 0 0.5rem; color: var(--text);
       border-bottom: 2px solid var(--text); padding-bottom: 0.5rem; }
  p  { font-size: 0.78rem; color: var(--text-muted); margin: 0 0 0.9rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { padding: 0.45rem 0.55rem; text-align: left; border-bottom: 1px solid var(--border); }
  th { color: var(--text-muted); font-weight: 600; font-size: 0.68rem;
       text-transform: uppercase; letter-spacing: 0.07em;
       border-bottom: 1px solid var(--text-muted); }
  .status { font-weight: 600; font-size: 0.75rem;
            text-transform: uppercase; letter-spacing: 0.05em; }
  .status.in    { color: var(--pos); }
  .status.pre   { color: var(--warn); }
  .status.post  { color: var(--text-dim); }
  .now { color: var(--text-muted); font-size: 0.7rem; margin-top: 0.75rem;
         text-transform: uppercase; letter-spacing: 0.05em; }
  .back { display: inline-block; color: var(--text-muted); text-decoration: none;
          font-size: 0.7rem; margin-bottom: 0.75rem;
          text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
  .back:hover { color: var(--text); }
</style>
</head><body>
  <a class="back" href="/">← Dashboard</a>
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
<link rel="apple-touch-icon" href="/icon.png">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
  :root { color-scheme: dark;
          --bg: #000; --bg-elev: #0a0a0a; --border: #1f1f1f; --border-strong: #2a2a2a;
          --text: #fafafa; --text-muted: #8a8a8a; --text-dim: #555;
          --pos: #00d97e; --warn: #ffd700; }
  body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         font-variant-numeric: tabular-nums;
         -webkit-font-smoothing: antialiased;
         background: var(--bg); color: var(--text); margin: 0; padding: 1rem 1.25rem;
         line-height: 1.5; }
  h1 { font-size: 1.2rem; font-weight: 700; letter-spacing: -0.02em;
       margin: 0 0 0.75rem; color: var(--text);
       border-bottom: 2px solid var(--text); padding-bottom: 0.5rem; }
  h2 { font-size: 0.85rem; font-weight: 600; margin: 1.4rem 0 0.5rem; color: var(--text);
       text-transform: uppercase; letter-spacing: 0.06em; }
  p  { font-size: 0.84rem; color: var(--text); margin: 0.3rem 0; }
  .src { background: var(--bg-elev); border: 1px solid var(--border); border-radius: 0;
         padding: 0.85rem 1rem; margin: 0.6rem 0; }
  .src h3 { margin: 0 0 0.3rem; font-size: 0.85rem; font-weight: 600;
            color: var(--text); text-transform: uppercase; letter-spacing: 0.05em; }
  .src .meta { font-size: 0.74rem; color: var(--text-muted); margin: 0.2rem 0 0.5rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.82rem; margin: 0.4rem 0; }
  th, td { padding: 0.4rem 0.55rem; text-align: left; border-bottom: 1px solid var(--border);
           vertical-align: top; }
  th { color: var(--text-muted); font-weight: 600; font-size: 0.68rem;
       text-transform: uppercase; letter-spacing: 0.07em;
       border-bottom: 1px solid var(--text-muted); }
  .live    { color: var(--pos); font-weight: 600; }
  .fcst    { color: var(--warn); font-weight: 600; }
  code { background: var(--bg); border: 1px solid var(--border-strong);
         padding: 0.05rem 0.35rem; border-radius: 0;
         font-size: 0.76rem; color: var(--text); font-family: 'Inter', monospace; }
  .tldr { background: var(--bg-elev); border-left: 3px solid var(--text);
          padding: 0.6rem 0.85rem; font-size: 0.84rem; color: var(--text);
          margin: 1rem 0; }
  .back { display: inline-block; color: var(--text-muted); text-decoration: none;
          font-size: 0.7rem; margin-bottom: 0.75rem;
          text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
  .back:hover { color: var(--text); }
</style>
</head><body>
  <a class="back" href="/">← Dashboard</a>
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


T24_HTML = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>T-24h calls</title>
<link rel="apple-touch-icon" href="/icon.png">
<meta name="theme-color" content="#000000">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap">
<style>
  :root { color-scheme: dark;
          --bg: #000; --bg-elev: #0a0a0a; --border: #1f1f1f; --border-strong: #2a2a2a;
          --text: #fafafa; --text-muted: #8a8a8a; --text-dim: #555;
          --pos: #00d97e; --neg: #ff5b6e; --warn: #ffd700; }
  body { font-family: 'Inter', -apple-system, BlinkMacSystemFont, system-ui, sans-serif;
         font-variant-numeric: tabular-nums;
         -webkit-font-smoothing: antialiased;
         background: var(--bg); color: var(--text); margin: 0; padding: 1rem 1.25rem;
         line-height: 1.5; }
  h1 { font-size: 1.2rem; font-weight: 700; letter-spacing: -0.02em;
       margin: 0 0 0.5rem; color: var(--text);
       border-bottom: 2px solid var(--text); padding-bottom: 0.5rem; }
  .lede { font-size: 0.78rem; color: var(--text-muted); margin: 0 0 1rem; }
  .summary { font-size: 0.75rem; color: var(--text-muted);
             text-transform: uppercase; letter-spacing: 0.05em;
             margin-bottom: 1rem; }
  .summary b { color: var(--text); font-weight: 600; }
  .summary .pos b { color: var(--pos); }
  .summary .warn b { color: var(--warn); }
  .summary .neg b { color: var(--neg); }
  .card { background: var(--bg-elev); border: 1px solid var(--border);
          padding: 0.9rem 1rem; margin: 0.7rem 0; }
  .card.missing { color: var(--text-muted); }
  .card.blocked { border-color: var(--neg); }
  .card-head { display: flex; flex-wrap: wrap; align-items: baseline;
               justify-content: space-between; gap: 0.5rem;
               margin-bottom: 0.4rem; }
  .card-head .title { font-size: 0.95rem; font-weight: 600; color: var(--text);
                      letter-spacing: 0.01em; }
  .card-head .title small { color: var(--text-muted); font-weight: 500;
                            margin-left: 0.4rem; font-size: 0.78rem; }
  .card-head .meta  { font-size: 0.72rem; color: var(--text-muted);
                      text-transform: uppercase; letter-spacing: 0.05em; }
  .card-head .meta a { color: var(--text-muted); text-decoration: none;
                       border-bottom: 1px dotted var(--text-muted); }
  .card-head .meta a:hover { color: var(--text); }
  .row { font-size: 0.78rem; color: var(--text); margin: 0.2rem 0; }
  .row .lbl { color: var(--text-muted); margin-right: 0.4rem;
              text-transform: uppercase; letter-spacing: 0.05em;
              font-size: 0.7rem; }
  table { width: 100%; border-collapse: collapse; font-size: 0.78rem;
          margin: 0.5rem 0 0.2rem; }
  th, td { padding: 0.3rem 0.5rem; text-align: right; border-bottom: 1px solid var(--border); }
  th:first-child, td:first-child { text-align: left; }
  th { color: var(--text-muted); font-weight: 600; font-size: 0.66rem;
       text-transform: uppercase; letter-spacing: 0.07em;
       border-bottom: 1px solid var(--text-muted); }
  .pos { color: var(--pos); font-weight: 600; }
  .neg { color: var(--neg); font-weight: 600; }
  .dim { color: var(--text-dim); }
  .badge { display: inline-block; background: rgba(255,215,0,0.12);
           color: var(--warn); border: 1px solid rgba(255,215,0,0.4);
           padding: 0.05rem 0.4rem; border-radius: 3px;
           font-size: 0.65rem; font-weight: 600;
           text-transform: uppercase; letter-spacing: 0.04em;
           margin-right: 0.3rem; }
  .badge.err { background: rgba(255,91,110,0.12); color: var(--neg);
               border-color: rgba(255,91,110,0.4); }
  .resolved   { color: var(--pos); font-weight: 600; }
  .stale-note { color: var(--warn); margin-left: 0.4rem; }
  .back { display: inline-block; color: var(--text-muted); text-decoration: none;
          font-size: 0.7rem; margin-bottom: 0.75rem;
          text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
  .back:hover { color: var(--text); }
  .footer { font-size: 0.7rem; color: var(--text-muted); margin-top: 1rem;
            border-top: 1px solid var(--border); padding-top: 0.6rem; }
</style>
</head><body>
  <a class="back" href="/">← Dashboard</a>
  <h1 id="title">T-24h calls</h1>
  <p class="lede">Frozen prediction for each event at the snapshot nearest 24h before close.
     These are the calls of record at the entry window the lead-time sweep identified as
     best risk-adjusted. They are not the same as a live "Predict" run later in the day.</p>
  <div id="summary" class="summary"></div>
  <div id="cards">Loading…</div>
  <div id="footer" class="footer"></div>

<script>
const fmtTemp = v => v == null ? "—" : v.toFixed(1) + "°";
const fmtPct  = v => v == null ? "—" : (v * 100).toFixed(1) + "%";
const fmtSign = v => v == null ? "—" : (v >= 0 ? "+" : "") + (v * 100).toFixed(1) + "¢";
const money   = v => v == null ? "—" : "$" + v.toFixed(2);
const cls     = v => v == null ? "dim" : v > 0.03 ? "pos" : v < -0.03 ? "neg" : "dim";

function fmtUtc(iso) {
  if (!iso) return "—";
  try { return new Date(iso).toISOString().replace("T", " ").slice(0, 16) + " UTC"; }
  catch { return iso; }
}

function probBg(p) {
  if (p == null) return "";
  const w = Math.round(p * 100);
  const a = (0.06 + p * 0.28).toFixed(3);
  return ` style="background:linear-gradient(to right,rgba(0,217,126,${a}) ${w}%,transparent ${w}%);"`;
}

function renderCard(ev) {
  if (ev.status === "missing") {
    return `<div class="card missing">
      <div class="card-head">
        <div class="title">${ev.city} <small>—</small> <span class="badge err">waiting</span></div>
        <div class="meta">${ev.reason || "no snapshot"}</div>
      </div>
      <div class="row">No T-24h snapshot available for this event today.</div>
    </div>`;
  }
  const qc = ev.qc || {};
  const warnBadges = (qc.warnings || []).map(w => `<span class="badge">${w}</span>`).join(" ");
  const catchupBadge = ev.catchup_at
    ? `<span class="badge" style="background:#3b3f5c;color:#dfe3ff;" title="Filled in by hourly catch-up at ${fmtUtc(ev.catchup_at)}">catch-up</span> `
    : "";
  const badges = catchupBadge + warnBadges;
  const liveLink = `<a href="/" title="Open live dashboard">→ live</a>`;

  // Predict-button-style summary: only highest probability, best EV YES (if
  // distinct), best EV NO. Same shape predict_event(ticker) surfaces.
  const top = ev.highest_probability;
  const by  = ev.best_ev_yes;
  const bn  = ev.best_ev_no;

  let picks = "";
  if (top) {
    picks += `<div class="row pick">
      <span class="lbl">Highest probability</span>${top.subtitle} (${fmtPct(top.prob)})<br>
      &nbsp;YES @ ${money(top.yes_ask)} → <span class="${cls(top.cal_ev_yes)}">${fmtSign(top.cal_ev_yes)}</span>,
      NO @ ${money(top.no_ask)} → <span class="${cls(top.cal_ev_no)}">${fmtSign(top.cal_ev_no)}</span>
    </div>`;
  }
  if (by && (!top || by.ticker !== top.ticker)) {
    picks += `<div class="row pick">
      <span class="lbl">Best EV YES</span>${by.subtitle} @ ${money(by.yes_ask)} →
      <span class="${cls(by.cal_ev_yes)}">${fmtSign(by.cal_ev_yes)}</span>
      <span class="dim">(P=${fmtPct(by.cal_prob_yes)})</span>
    </div>`;
  }
  if (bn) {
    picks += `<div class="row pick">
      <span class="lbl">Best EV NO</span>${bn.subtitle} @ ${money(bn.no_ask)} →
      <span class="${cls(bn.cal_ev_no)}">${fmtSign(bn.cal_ev_no)}</span>
      <span class="dim">(P_no=${fmtPct(bn.cal_prob_no)})</span>
    </div>`;
  }
  if (!picks) {
    picks = `<div class="row dim">No qualifying picks (no EV above ${(0.05*100).toFixed(0)}¢).</div>`;
  }

  const resolution = ev.settled
    ? `<div class="row resolved">Resolved: ${ev.settled_bucket}</div>`
    : `<div class="row"><span class="lbl">Resolves</span>${fmtUtc(ev.close_time)}</div>`;

  return `<div class="card">
    <div class="card-head">
      <div class="title">${ev.city} <small>${ev.event_ticker}</small> ${badges}</div>
      <div class="meta">lead ${ev.lead_hours == null ? "—" : ev.lead_hours.toFixed(1)}h
        · snapshot ${fmtUtc(ev.snapshot_ts)} · ${liveLink}</div>
    </div>
    ${picks}
    ${resolution}
  </div>`;
}

async function load() {
  try {
    const r = await fetch("/api/t24");
    if (r.status === 503) {
      const j = await r.json().catch(() => ({}));
      document.getElementById("cards").innerHTML =
        `<div class="card blocked"><div class="row"><b>QC blocked.</b> ${j.error || "no archive yet"}</div></div>`;
      return;
    }
    const j = await r.json();
    const s = j.qc_summary || {ok:0, warnings:0, errors:0};
    document.getElementById("title").textContent = `T-24h calls — ${j.target_date}`;
    let summary = `<span class="pos">OK <b>${s.ok}</b></span> · ` +
                  `<span class="warn">WARN <b>${s.warnings}</b></span> · ` +
                  `<span class="neg">ERR <b>${s.errors}</b></span>`;
    if (j.stale) summary += `<span class="stale-note">· showing ${j.stale_for_date} (today's not yet generated)</span>`;
    document.getElementById("summary").innerHTML = summary;
    document.getElementById("cards").innerHTML =
      (j.events || []).map(renderCard).join("");
    document.getElementById("footer").textContent =
      `Generated ${fmtUtc(j.generated_at)}`;
  } catch (e) {
    document.getElementById("cards").innerHTML =
      `<div class="card blocked"><div class="row"><b>Error loading.</b> ${e.message}</div></div>`;
  }
}
load();
</script>
</body></html>
"""


STATIC_PAGES = {
    "/":         DASHBOARD_HTML.encode(),
    "/schedule": SCHEDULE_HTML.encode(),
    "/sources":  SOURCES_HTML.encode(),
    "/t24":      T24_HTML.encode(),
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


def get_t24_card_payload():
    """Read today's T-24 card archive, fall back to most recent if today's
    is missing, overlay live settlement, and return the JSON payload.

    Raises FileNotFoundError if no archive exists at all (caller should
    return 503).
    """
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    today_iso = datetime.now(timezone.utc).astimezone(et).date().isoformat()
    cards_dir = Path("t24_cards")
    archive = cards_dir / f"{today_iso}.json"
    stale = False
    if not archive.exists():
        # Fall back to the most recent prior archive file.
        if cards_dir.exists():
            prior = sorted(cards_dir.glob("[0-9]*.json"))
            if prior:
                archive = prior[-1]
                stale = True
        if not archive.exists():
            blocked = cards_dir / f"blocked_{today_iso}.json"
            if blocked.exists():
                payload = json.loads(blocked.read_text(encoding="utf-8"))
                payload["blocked"] = True
                return payload
            raise FileNotFoundError("no t24 cards generated yet")

    payload = json.loads(archive.read_text(encoding="utf-8"))
    if stale:
        payload["stale"] = True
        payload["stale_for_date"] = payload.get("target_date")

    # Settlement overlay: scan live_picks_log.jsonl for settled rows matching
    # each event's (target_date, series). Cheap because the log is small
    # (a few hundred lines/day).
    log_path = Path("live_picks_log.jsonl")
    if log_path.exists():
        latest_settled = {}  # (target_date, series) -> bucket
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not r.get("settled") or not r.get("settled_bucket"):
                    continue
                key = (r.get("target_date"), r.get("series"))
                latest_settled[key] = r["settled_bucket"]
        for ev in payload.get("events", []):
            if ev.get("status") == "missing":
                continue
            key = (payload.get("target_date"), ev.get("series"))
            if key in latest_settled:
                ev["settled"] = True
                ev["settled_bucket"] = latest_settled[key]

    return payload


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

        if path == "/api/signals":
            try:
                import signals as _signals
                self._send(200, "application/json",
                           json.dumps(_signals.read_report_payload()).encode())
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}).encode())
            return

        if path == "/api/t24":
            try:
                payload = get_t24_card_payload()
            except FileNotFoundError as e:
                self._send(503, "application/json",
                           json.dumps({"error": str(e)}).encode())
                return
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}).encode())
                return
            self._send(200, "application/json", json.dumps(payload).encode())
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
    scheme = "http"
    if args.https:
        import ssl
        if not args.cert or not args.key:
            sys.exit("--https requires --cert and --key (use mkcert to generate; "
                     "see docs/superpowers/reports/* for setup notes)")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=args.cert, keyfile=args.key)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = "https"
    print(f"kalshi_temp dashboard: {scheme}://localhost:{args.port}/", flush=True)
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


# Selection constants (SANITY_MARKET_CONFIDENT_YES, SANITY_MODEL_LOW_PROB,
# MIN_BEST_EV, MIN_PRINTED_NO) are imported from wb_thresholds at the top of this
# module — single source of truth shared with lab.replay and the health scan.

# --- calibration params (haircut applied upstream of selection) ---
# Per-side list of printed-prob bands -> haircut h (in probability points).
# haircut = printed - realized: positive h = overconfident (shrink), negative
# h = underconfident (raise). The authoritative values live in
# calibration_params.json, written by `python -m lab.cli live-calibration
# --emit-params`. This code default is PROVISIONAL (NO ~11pp from the 2026-05-26
# n=49 cut; YES identity) and is superseded by the file when present. Refit the
# file at the 2026-06-03 cut.
DEFAULT_CAL_PARAMS = {
    "no":  [{"lo": MIN_PRINTED_NO, "hi": 1.01, "h": NO_HAIRCUT}],
    "yes": [{"lo": 0.00, "hi": 1.01, "h": 0.00}],
}
CAL_PARAMS_PATH = "calibration_params.json"


def load_calibration_params(path=CAL_PARAMS_PATH):
    """Load per-side haircut bands. Fall back to DEFAULT_CAL_PARAMS (and log)
    if the file is absent or malformed."""
    p = Path(path)
    if not p.exists():
        return DEFAULT_CAL_PARAMS
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        for side in ("yes", "no"):
            assert isinstance(data.get(side), list)
            for band in data[side]:
                float(band["lo"]); float(band["hi"]); float(band["h"])
        return data
    except Exception as e:
        print(f"[calibration] falling back to default params: {e}", file=sys.stderr)
        return DEFAULT_CAL_PARAMS


_CAL_PARAMS = load_calibration_params()


def haircut_for(side, printed_prob, params=None):
    """Haircut h for `side` at this printed probability; 0.0 if no band matches."""
    params = params if params is not None else _CAL_PARAMS
    for band in params.get(side, []):
        if band["lo"] <= printed_prob < band["hi"]:
            return float(band["h"])
    return 0.0


def _clamp01(x):
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def apply_calibration(market, params=None):
    """Add cal_prob_{yes,no} and cal_ev_{yes,no} to `market` in place,
    computed from prob/yes_ask/no_ask in probability space.

    Idempotent (cal_* never feed back in). Exact identity when a side's
    haircut is 0: cal_prob == prob and cal_ev == ev bit-for-bit (no clamp,
    no rounding) so a zero-haircut side cannot flip a borderline pick.
    """
    params = params if params is not None else _CAL_PARAMS
    prob = market.get("prob")
    ya = market.get("yes_ask")
    na = market.get("no_ask")
    if prob is None:
        market["cal_prob_yes"] = None
        market["cal_prob_no"] = None
        market["cal_ev_yes"] = None
        market["cal_ev_no"] = None
        return market
    printed_no = 1.0 - prob
    h_yes = haircut_for("yes", prob, params)
    h_no = haircut_for("no", printed_no, params)
    cal_prob_yes = prob if h_yes == 0.0 else _clamp01(prob - h_yes)
    cal_prob_no = printed_no if h_no == 0.0 else _clamp01(printed_no - h_no)
    market["cal_prob_yes"] = cal_prob_yes
    market["cal_prob_no"] = cal_prob_no
    market["cal_ev_yes"] = (cal_prob_yes - ya) if ya not in (None, 0.0) else None
    market["cal_ev_no"] = (cal_prob_no - na) if na not in (None, 0.0) else None
    return market


def finalize_markets(markets, params=None):
    """Apply calibration to every market in a list (in place). Used to finalize
    build_event_data output so cal_* ship with the live dashboard and snapshots."""
    for m in markets:
        apply_calibration(m, params)
    return markets


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


def best_no_pick(markets):
    """Canonical 'Best EV NO' — single source of truth for the dashboard, CLI,
    T-24 card, and alerts.

    Ranks on CALIBRATED EV (cal_ev_no), so the haircut decides whether a pick
    surfaces at all. Subject to:
      - cal_ev_no >= MIN_BEST_EV       (calibrated edge worth taking)
      - _sanity_keep_no                (don't fight a highly-confident market)
      - (1 - prob) >= MIN_PRINTED_NO   (printed-NO floor; pre-haircut conviction)

    Computes cal_* lazily for markets missing them (e.g. historical log rows),
    so this re-scores live_picks_log.jsonl identically to the live pipeline.
    """
    for m in markets:
        if "cal_ev_no" not in m:
            apply_calibration(m)
    cands = [m for m in markets
             if m.get("cal_ev_no") is not None and m["cal_ev_no"] >= MIN_BEST_EV
             and m.get("prob") is not None and _sanity_keep_no(m)
             and (1 - m["prob"]) >= MIN_PRINTED_NO]
    return max(cands, key=lambda m: m["cal_ev_no"], default=None)


def best_yes_pick(markets):
    """Canonical 'Best EV YES' — argmax cal_ev_yes over the MIN_BEST_EV floor.
    Under the default identity YES haircut this equals argmax(ev_yes)."""
    for m in markets:
        if "cal_ev_yes" not in m:
            apply_calibration(m)
    cands = [m for m in markets
             if m.get("cal_ev_yes") is not None and m["cal_ev_yes"] >= MIN_BEST_EV]
    return max(cands, key=lambda m: m["cal_ev_yes"], default=None)


def predict_summary(markets):
    """Canonical {highest_probability, best_ev_yes, best_ev_no} over a markets
    list. The one place the dashboard, CLI, T-24 card, and alerts derive picks."""
    for m in markets:
        if "cal_ev_no" not in m:
            apply_calibration(m)
    probs = [m for m in markets if m.get("prob") is not None]
    return {
        "highest_probability": (max(probs, key=lambda m: m["prob"]) if probs else None),
        "best_ev_yes": best_yes_pick(markets),
        "best_ev_no": best_no_pick(markets),
    }


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
    summary = predict_summary(data["markets"])
    return {
        "event_ticker": data["event_ticker"],
        "title": data["title"],
        "target_date": data["target_date"],
        "station": data["station"],
        "forecasts": data["forecasts"],
        "model": data["model"],
        "settled": data["settled"],
        "settled_bucket": data["settled_bucket"],
        "highest_probability": summary["highest_probability"],
        "best_ev_yes": summary["best_ev_yes"],
        "best_ev_no": summary["best_ev_no"],
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

    f = data["forecasts"]
    summary = predict_summary(data["markets"])

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
            "highest_probability": summary["highest_probability"],
            "best_ev_yes": summary["best_ev_yes"],
            "best_ev_no": summary["best_ev_no"],
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

    top = summary["highest_probability"]
    if top is None:
        return

    print()
    print(f"Highest-probability bucket: {top['subtitle']}  ({top['prob']*100:.1f}%)")
    print(f"  ticker:   {top['ticker']}")
    print(f"  YES ask:  ${_money(top['yes_ask'])}    EV: {_signed(top['cal_ev_yes'])}")
    print(f"  NO  ask:  ${_money(top['no_ask'])}     EV: {_signed(top['cal_ev_no'])}")

    best_yes = summary["best_ev_yes"]
    best_no = summary["best_ev_no"]
    if best_yes and best_yes["ticker"] != top["ticker"]:
        print(f"Best EV YES: {best_yes['subtitle']} @ ${_money(best_yes['yes_ask'])} "
              f"-> {_signed(best_yes['cal_ev_yes'])} (P {best_yes['cal_prob_yes']*100:.1f}%)")
    if best_no:
        print(f"Best EV NO:  {best_no['subtitle']} @ ${_money(best_no['no_ask'])} "
              f"-> {_signed(best_no['cal_ev_no'])} (P_no {best_no['cal_prob_no']*100:.1f}%)")


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

    try:
        close_dt = datetime.fromisoformat(markets[0]["close_time"].replace("Z", "+00:00"))
    except Exception:
        return {"event": event_ticker, "skipped": "no_close_time"}

    from model import ModelInputs, compute
    from lab.configs import BACKTEST_TODAY
    inputs = ModelInputs(
        series=series, event_ticker=event_ticker, target_date=target_date,
        forecasts={"ecmwf": ecmwf, "gfs": gfs, "nws": None},
        metar_current=None, today_max=None,
        markets=markets,
        decision_ts=int(close_dt.timestamp() - lead_hours * 3600),
        fetched_at=0,
    )
    out = compute(inputs, BACKTEST_TODAY)
    if out.mu is None:
        return {"event": event_ticker, "skipped": "no_probability"}
    mu, sigma = out.mu, out.sigma

    ranked = [(m, out.probs[m["ticker"]]) for m in markets if m["ticker"] in out.probs]
    if not ranked:
        return {"event": event_ticker, "skipped": "no_probability"}

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
    s.add_argument("--https", action="store_true",
                   help="serve over HTTPS; requires --cert and --key")
    s.add_argument("--cert", help="path to PEM cert (use mkcert to generate)")
    s.add_argument("--key", help="path to PEM private key")
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
