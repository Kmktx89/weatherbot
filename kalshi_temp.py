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


# ---------- model ----------

def bucket_probability(market, mu, sigma):
    """Probability the integer daily-high lands in this bucket (continuity-corrected)."""
    st = market.get("strike_type")
    cap = market.get("cap_strike")
    floor = market.get("floor_strike")
    if st == "less" and cap is not None:
        return normal_cdf(cap - 0.5, mu, sigma)
    if st == "greater" and floor is not None:
        return 1.0 - normal_cdf(floor + 0.5, mu, sigma)
    if st == "between" and floor is not None and cap is not None:
        return normal_cdf(cap + 0.5, mu, sigma) - normal_cdf(floor - 0.5, mu, sigma)
    return None


def _sort_key(m):
    floor = m.get("floor_strike")
    cap = m.get("cap_strike")
    if m.get("strike_type") == "less":
        return -1e9 if cap is None else cap - 1000
    if m.get("strike_type") == "greater":
        return 1e9 if floor is None else floor + 1000
    return floor if floor is not None else (cap or 0)


def _market_summary(m):
    return {
        "ticker": m.get("ticker"),
        "subtitle": m.get("subtitle"),
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

    with ThreadPoolExecutor(max_workers=4) as pool:
        f_ec  = pool.submit(fetch_open_meteo, city["lat"], city["lon"], target, "ecmwf_ifs025")
        f_gfs = pool.submit(fetch_open_meteo, city["lat"], city["lon"], target, "gfs_seamless")
        f_nws = pool.submit(fetch_nws_high, city["lat"], city["lon"], target)
        f_met = pool.submit(fetch_metar_temp, city["icao"])
        ecmwf, gfs, nws, metar = f_ec.result(), f_gfs.result(), f_nws.result(), f_met.result()

    forecasts = {"ecmwf": ecmwf, "gfs": gfs, "nws": nws, "metar": metar}
    sources = [v for v in (ecmwf, gfs, nws) if v is not None]

    settled_bucket = None
    for m in markets:
        if (to_float(m.get("yes_bid_dollars")) or 0) >= 0.95:
            settled_bucket = m.get("subtitle")
            break
    settled = settled_bucket is not None

    if not sources or settled:
        model = {"mu": None, "sigma": None}
        out_markets = [dict(_market_summary(m), prob=None, ev_yes=None, ev_no=None) for m in markets]
    else:
        mu = sum(sources) / len(sources)
        spread = statistics.pstdev(sources) if len(sources) > 1 else 0.0
        sigma = math.sqrt(BASE_SIGMA ** 2 + spread ** 2)
        model = {"mu": mu, "sigma": sigma, "sources": len(sources)}
        out_markets = []
        for m in markets:
            prob = bucket_probability(m, mu, sigma)
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
</style>
</head>
<body>
<h1>Kalshi temperature predictor
  <small id="ts"></small>
  <button id="btn" onclick="refresh(true)">Refresh</button>
</h1>
<div id="err"></div>
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
    html += `<span>ECMWF <b>${fmt(f.ecmwf)}</b> · GFS/HRRR <b>${fmt(f.gfs)}</b> · NWS <b>${fmt(f.nws)}</b> · METAR <b>${fmt(f.metar)}</b></span>`;
    if (m.mu != null)
      html += `<span>Model: <b>μ ${m.mu.toFixed(1)}° · σ ${m.sigma.toFixed(1)}°</b></span>`;
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
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[http] %s\n" % (fmt % args))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode())
        elif self.path.startswith("/api/markets"):
            try:
                data = get_dashboard_data(force="refresh=1" in self.path)
                body = json.dumps(data).encode()
                self._send(200, "application/json", body)
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}).encode())
        else:
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

def fetch_event_and_markets(event_ticker):
    """Look up one event + its markets by ticker."""
    series = event_ticker.split("-")[0]
    try:
        evs = kalshi_get("/events",
                         {"series_ticker": series, "with_nested_markets": "true"}
                         ).get("events", []) or []
    except Exception as e:
        sys.exit(f"failed to fetch series {series}: {e}")
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
        sys.exit(f"event not found: {event_ticker}")
    try:
        markets = kalshi_get("/markets",
                             {"event_ticker": event_ticker, "limit": 200}
                             ).get("markets", []) or []
    except Exception as e:
        sys.exit(f"failed to fetch markets for {event_ticker}: {e}")
    return ev, markets


def cmd_predict(args):
    ev, markets = fetch_event_and_markets(args.event_ticker)
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
                try:
                    strike = datetime.fromisoformat(ev["strike_date"].replace("Z", "+00:00"))
                except Exception:
                    continue
                if strike >= cutoff:
                    out.append(ev)
            cursor = resp.get("cursor")
            if not cursor:
                break
    # dedupe by event_ticker, keep oldest -> newest
    seen, unique = set(), []
    for ev in sorted(out, key=lambda e: e["strike_date"]):
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

    mu = sum(sources) / len(sources)
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
        "winner_bucket": winner.get("subtitle"),
        "picked_ticker": pick["ticker"],
        "picked_bucket": pick.get("subtitle"),
        "model_prob": pick_p,
        "ecmwf": ecmwf, "gfs": gfs,
        "mu": mu, "sigma": sigma,
        "entry_price": price,
        "won": won,
        "pnl": pnl,
    }


def cmd_backtest(args):
    if args.events:
        tickers = [t.strip() for t in args.events.split(",") if t.strip()]
    elif args.series:
        evs = list_events_for_series(args.series, args.days)
        tickers = [e["event_ticker"] for e in evs]
    else:
        sys.exit("backtest requires --events OR --series")

    if not tickers:
        sys.exit("no events to backtest")

    print(f"Backtesting {len(tickers)} event(s) at T-{args.lead_hours}h "
          f"(threshold={args.threshold:.2f})", file=sys.stderr)

    results = []
    for i, t in enumerate(tickers, 1):
        r = backtest_one_event(t, args.lead_hours, args.threshold)
        results.append(r)
        if not args.json:
            if "skipped" in r:
                print(f"  [{i}/{len(tickers)}] {t}  SKIPPED ({r['skipped']})")
            else:
                tag = "WIN " if r["won"] else "LOSS"
                print(f"  [{i}/{len(tickers)}] {t}  {tag}  "
                      f"pick={r['picked_bucket']:<14} winner={r['winner_bucket']:<14} "
                      f"P={r['model_prob']*100:5.1f}%  entry=${r['entry_price']:.2f}  "
                      f"pnl={r['pnl']:+.3f}")

    bets = [r for r in results if "skipped" not in r]
    summary = _backtest_summary(bets)

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
