import math
from signals import (
    YES_EV_MIN, EV_MAX, SPREAD_MAX, NO_PRINTED_MIN, NO_LEAD_MIN, KELLY_FRACTION,
    size_pct, lead_hours_for, market_favorite_index,
)


def test_size_pct_eighth_kelly():
    assert size_pct(ev=0.12, price=0.52) == round(100 * 0.125 * 0.12 / 0.52, 1)
    assert size_pct(ev=0.10, price=0.50) == 2.5


def test_size_pct_zero_price_is_zero():
    assert size_pct(ev=0.1, price=0.0) == 0.0


def test_market_favorite_index_picks_highest_mid():
    markets = [
        {"yes_bid": 0.05, "yes_ask": 0.07},
        {"yes_bid": 0.40, "yes_ask": 0.44},
        {"yes_bid": 0.20, "yes_ask": 0.24},
    ]
    assert market_favorite_index(markets) == 1


def test_market_favorite_index_none_when_no_prices():
    assert market_favorite_index([{"yes_bid": None, "yes_ask": None}]) is None


def test_lead_hours_for_uses_close_one_am_local_day_after():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 5, 28, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    lead = lead_hours_for("KXHIGHLAX", "2026-05-28", now=now)
    assert abs(lead - 16.0) < 0.05
