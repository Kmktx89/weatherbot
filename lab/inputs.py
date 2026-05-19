"""Assemble ModelInputs for a settled event, going through DataCache.

Used by lab.replay (settled events, historical forecasts) and by future
shadow-mode analysis if needed.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import kalshi_temp as kt

from model import ModelInputs

from .data_cache import DataCache, default


# Per-source TTLs in seconds (see spec §11). None = forever.
TTL = {
    "open_meteo:hist": None,
    "open_meteo:live": 300,
    "nws": 300,
    "metar_hist": None,
    "kalshi_event_markets": 60,
}


def fetch_historical_open_meteo(lat: float, lon: float, target_date: str,
                                 model: str, cache: DataCache) -> float | None:
    key = f"open_meteo:hist:{model}:{lat:.4f},{lon:.4f}:{target_date}"
    cached = cache.get(key, ttl=TTL["open_meteo:hist"])
    if cached is not None:
        return cached.get("value")
    v = kt.fetch_open_meteo(lat, lon, target_date, model, historical=True)
    cache.set(key, {"value": v}, source="open_meteo:hist", target_date=target_date)
    return v


def fetch_event_markets(event_ticker: str, cache: DataCache) -> list[dict]:
    key = f"kalshi_event_markets:{event_ticker}"
    cached = cache.get(key, ttl=TTL["kalshi_event_markets"])
    if cached is not None:
        return cached
    markets = kt.kalshi_get("/markets", {"event_ticker": event_ticker, "limit": 200}
                            ).get("markets", []) or []
    cache.set(key, markets, source="kalshi_event_markets", target_date=None)
    return markets


def build_historical_inputs(event_ticker: str, *, cache: DataCache | None = None) -> ModelInputs | None:
    """Build ModelInputs for a settled event using cached historical forecasts.

    Returns None if the event is unsupported or fetches fail.
    """
    cache = cache or default()
    series = event_ticker.split("-")[0]
    city = kt.CITIES.get(series)
    if not city:
        return None
    markets = fetch_event_markets(event_ticker, cache)
    if not markets:
        return None
    target_date = kt.event_local_date({"event_ticker": event_ticker,
                                       "strike_date": markets[0].get("close_time", "")})
    try:
        close_dt = datetime.fromisoformat(markets[0]["close_time"].replace("Z", "+00:00"))
    except Exception:
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_ec = pool.submit(fetch_historical_open_meteo, city["lat"], city["lon"],
                           target_date, "ecmwf_ifs025", cache)
        f_gfs = pool.submit(fetch_historical_open_meteo, city["lat"], city["lon"],
                            target_date, "gfs_seamless", cache)
        ecmwf, gfs = f_ec.result(), f_gfs.result()

    return ModelInputs(
        series=series, event_ticker=event_ticker, target_date=target_date,
        forecasts={"ecmwf": ecmwf, "gfs": gfs, "nws": None},
        metar_current=None, today_max=None,
        markets=markets,
        decision_ts=int(close_dt.timestamp()),  # caller passes a lead-shifted value via replay
        fetched_at=int(datetime.now(timezone.utc).timestamp()),
    )


def winner_of(markets: list[dict]) -> dict | None:
    return next((m for m in markets if m.get("result") == "yes"), None)
