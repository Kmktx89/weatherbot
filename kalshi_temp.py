#!/usr/bin/env python3
"""kalshi_temp.py — Kalshi daily-high-temperature dashboard.

Usage:
    python kalshi_temp.py serve --port 8765

Fetches Kalshi temperature markets, forecasts from Open-Meteo (ECMWF + GFS),
NWS, and current METAR observations. Renders a local HTML dashboard at
http://localhost:<port>/ that shows model probability + EV per bucket.
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


def fetch_open_meteo(lat, lon, target_date, model):
    try:
        r = session.get(OPEN_METEO, params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "models": model,
            "temperature_unit": "fahrenheit",
            "timezone": "auto",
            "start_date": target_date,
            "end_date": target_date,
        }, timeout=15)
        r.raise_for_status()
        vals = (r.json().get("daily") or {}).get("temperature_2m_max") or []
        if vals and vals[0] is not None:
            return float(vals[0])
    except Exception as e:
        print(f"[open-meteo:{model}] {e}", file=sys.stderr)
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


def main():
    p = argparse.ArgumentParser(prog="kalshi_temp")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the local dashboard HTTP server")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
