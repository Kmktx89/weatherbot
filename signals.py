"""Interim qualifying-signal filter: turns live event data into clean order
tickets that pass the probation bar (2026-05-27 weighted-sigma tightening).

Detection/reporting only — reads model output + prices, never trades, never
writes anything except via the hourly_signals.py runner. Filter logic lives
ONLY here (single source of truth). See spec
docs/superpowers/specs/2026-05-28-hourly-signal-report-design.md.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
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


def _ticket(side: str, pick: dict, event: dict, lead: float, price: float, ev: float) -> dict:
    series = event["event_ticker"].split("-")[0]
    return {
        "series": series,
        "city": kt.CITIES[series]["name"],
        "event_ticker": event["event_ticker"],
        "target_date": event["target_date"],
        "side": side,
        "bucket": pick["subtitle"],
        "ticker": pick["ticker"],
        "printed_prob": round(pick["cal_prob_yes"] if side == "YES" else pick["cal_prob_no"], 3),
        "market_price": round(price, 2),
        "ev": round(ev, 4),
        "size_pct": size_pct(ev, price),
        "lead_hours": round(lead, 1),
    }


def qualify_event(event: dict) -> list[dict]:
    """0-2 order tickets (YES and/or NO) for one event that pass the interim bar.
    Pure given the event dict (best_yes/no_pick are deterministic over markets)."""
    if event.get("settled"):
        return []
    markets = event.get("markets") or []
    series = event["event_ticker"].split("-")[0]
    lead = lead_hours_for(series, event["target_date"])
    tickets: list[dict] = []

    # YES
    y = kt.best_yes_pick(markets)
    if y is not None:
        ev = y.get("cal_ev_yes")
        ya, yb = y.get("yes_ask"), y.get("yes_bid")
        fav = market_favorite_index(markets)
        try:
            yi = markets.index(y)
        except ValueError:
            yi = None
        agree = fav is not None and yi is not None and abs(yi - fav) <= 1
        spread = round(ya - yb, 4) if (ya is not None and yb is not None) else 1.0
        if (ev is not None and YES_EV_MIN <= ev <= EV_MAX and agree
                and spread <= SPREAD_MAX and ya):
            tickets.append(_ticket("YES", y, event, lead, ya, ev))

    # NO
    n = kt.best_no_pick(markets)
    if n is not None and lead >= NO_LEAD_MIN:
        ev = n.get("cal_ev_no")
        na, prob = n.get("no_ask"), n.get("prob")
        ya, yb = n.get("yes_ask"), n.get("yes_bid")
        spread = round(ya - yb, 4) if (ya is not None and yb is not None) else 1.0
        if (ev is not None and YES_EV_MIN <= ev <= EV_MAX and prob is not None
                and (1 - prob) >= NO_PRINTED_MIN and spread <= SPREAD_MAX and na):
            tickets.append(_ticket("NO", n, event, lead, na, ev))

    return tickets


def qualifying_signals(events: list[dict]) -> list[dict]:
    """Flatten qualify_event over all (non-settled) events."""
    out: list[dict] = []
    for e in events:
        out.extend(qualify_event(e))
    return out


def build_report(events: list[dict]) -> dict:
    """The report payload: only fully-qualifying picks (settled events skipped
    inside qualify_event)."""
    picks = qualifying_signals(events)
    n_open = sum(1 for e in events if not e.get("settled"))
    return {
        "bar": "interim-2026-05-27",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "n_open_events": n_open,
        "picks": picks,
    }


def read_report_payload(path: str = "hourly_signals.json", *, stale_after_min: int = 90) -> dict:
    """Read the latest report for serving; mark stale if missing or old."""
    p = Path(path)
    if not p.exists():
        return {"bar": "interim-2026-05-27", "generated_at": None,
                "n_open_events": 0, "picks": [], "stale": True}
    data = json.loads(p.read_text(encoding="utf-8"))
    gen = data.get("generated_at")
    stale = True
    if gen:
        try:
            age_min = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(gen).astimezone(timezone.utc)).total_seconds() / 60.0
            stale = age_min > stale_after_min
        except Exception:
            stale = True
    data["stale"] = stale
    return data
