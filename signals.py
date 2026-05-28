"""Interim qualifying-signal filter: turns live event data into clean order
tickets that pass the probation bar (2026-05-27 weighted-sigma tightening).

Detection/reporting only — reads model output + prices, never trades, never
writes anything except via the hourly_signals.py runner. Filter logic lives
ONLY here (single source of truth). See spec
docs/superpowers/specs/2026-05-28-hourly-signal-report-design.md.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import kalshi_temp as kt

# Interim bar (tunable as the post-deploy live calibration comes in).
YES_EV_MIN = 0.10
EV_MAX = 0.25          # drop "huge" edges (likely sigma-artifacts post-tightening)
SPREAD_MAX = 0.05
NO_PRINTED_MIN = 0.90  # NO only when very confident (pre-haircut 1 - P_yes)
NO_LEAD_MIN = 18.0     # NO never shown near close
KELLY_FRACTION = 0.125  # 1/8-Kelly during probation


def size_pct(ev: float, price: float) -> float:
    """1/8-Kelly stake as a % of bankroll = 100 * 0.125 * ev/price."""
    if not price:
        return 0.0
    return round(100 * KELLY_FRACTION * ev / price, 1)


def market_favorite_index(markets: list[dict]) -> int | None:
    """Index of the bucket with the highest market mid (yes_bid+yes_ask)/2."""
    best_i, best_mid = None, None
    for i, m in enumerate(markets):
        yb, ya = m.get("yes_bid"), m.get("yes_ask")
        if yb is None or ya is None:
            continue
        mid = (yb + ya) / 2.0
        if best_mid is None or mid > best_mid:
            best_i, best_mid = i, mid
    return best_i


def lead_hours_for(series: str, target_date: str, *, now: datetime | None = None) -> float:
    """Hours from now until ~01:00 local on the day AFTER target_date
    (the verified KXHIGH close convention)."""
    tz = ZoneInfo(kt.CITIES[series]["tz"])
    now = now.astimezone(tz) if now else datetime.now(tz)
    close_day = datetime.fromisoformat(target_date).date() + timedelta(days=1)
    close = datetime(close_day.year, close_day.month, close_day.day, 1, 0, tzinfo=tz)
    return (close - now).total_seconds() / 3600.0
