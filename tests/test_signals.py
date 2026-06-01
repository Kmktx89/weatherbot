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


from signals import qualify_event, qualifying_signals


def _mkt(sub, prob, yb, ya, na, st="between"):
    return {"ticker": "T-" + sub, "subtitle": sub, "strike_type": st,
            "yes_bid": yb, "yes_ask": ya, "no_ask": na, "_sort": sub,
            "prob": prob, "cal_prob_yes": prob, "cal_prob_no": 1 - prob,
            "cal_ev_yes": (prob - ya) if ya else None,
            "cal_ev_no": ((1 - prob) - na) if na else None}


def _event(series, markets, target="2026-05-28"):
    return {"event_ticker": f"{series}-26MAY28", "target_date": target,
            "station": "x", "settled": False, "model": {"mu": 70.0},
            "markets": markets}


def test_qualify_event_yes_passes_clean(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    mkts = [_mkt("64-65", 0.05, 0.04, 0.06, 0.95),
            _mkt("66-67", 0.65, 0.50, 0.52, 0.50)]
    out = qualify_event(_event("KXHIGHNY", mkts))
    yes = [t for t in out if t["side"] == "YES"]
    assert len(yes) == 1
    assert yes[0]["bucket"] == "66-67" and yes[0]["ev"] == round(0.13, 4)
    assert yes[0]["market_price"] == 0.52 and yes[0]["size_pct"] > 0


def test_qualify_event_drops_yes_below_ev_bar(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    mkts = [_mkt("66-67", 0.55, 0.50, 0.52, 0.50)]
    assert qualify_event(_event("KXHIGHNY", mkts)) == []


def test_qualify_event_drops_yes_huge_edge(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    mkts = [_mkt("66-67", 0.90, 0.50, 0.52, 0.50)]
    assert qualify_event(_event("KXHIGHNY", mkts)) == []


def test_qualify_event_drops_yes_fighting_market(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    mkts = [_mkt("60-61", 0.65, 0.50, 0.52, 0.50),
            _mkt("64-65", 0.10, 0.10, 0.12, 0.88),
            _mkt("70-71", 0.20, 0.55, 0.58, 0.42)]
    assert qualify_event(_event("KXHIGHNY", mkts)) == []


def test_qualify_event_drops_yes_wide_spread(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    mkts = [_mkt("66-67", 0.65, 0.46, 0.52, 0.50)]
    assert qualify_event(_event("KXHIGHNY", mkts)) == []


def test_qualify_event_no_passes_when_confident_and_early(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 24.0)
    mkts = [_mkt("80-81", 0.05, 0.02, 0.04, 0.82)]
    out = qualify_event(_event("KXHIGHMIA", mkts))
    no = [t for t in out if t["side"] == "NO"]
    assert len(no) == 1 and no[0]["market_price"] == 0.82


def test_qualify_event_drops_no_near_close(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 10.0)
    mkts = [_mkt("80-81", 0.05, 0.02, 0.04, 0.82)]
    assert [t for t in qualify_event(_event("KXHIGHMIA", mkts)) if t["side"] == "NO"] == []


def test_qualifying_signals_skips_settled(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    ev = _event("KXHIGHNY", [_mkt("66-67", 0.65, 0.50, 0.52, 0.50)])
    ev["settled"] = True
    assert qualifying_signals([ev]) == []


def test_qualify_event_yes_spread_exactly_5c_passes(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    # spread = 0.52 - 0.47 = exactly 0.05 -> must PASS (not lost to float error)
    mkts = [_mkt("64-65", 0.05, 0.04, 0.06, 0.95),
            _mkt("66-67", 0.65, 0.47, 0.52, 0.50)]
    out = qualify_event(_event("KXHIGHNY", mkts))
    assert [t for t in out if t["side"] == "YES"], "5c-wide spread should qualify"


def test_build_report_shape(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    ev = _event("KXHIGHNY", [_mkt("64-65", 0.05, 0.04, 0.06, 0.95),
                             _mkt("66-67", 0.65, 0.50, 0.52, 0.50)])
    rep = signals.build_report([ev])
    assert rep["bar"] == "interim-2026-05-27"
    assert "generated_at" in rep and rep["n_open_events"] == 1
    assert isinstance(rep["picks"], list) and rep["picks"][0]["side"] == "YES"


def test_signals_payload_marks_stale(tmp_path):
    import json as _json, signals
    from datetime import datetime, timezone, timedelta
    p = tmp_path / "hourly_signals.json"
    old = (datetime.now(timezone.utc) - timedelta(minutes=120)).astimezone().isoformat()
    p.write_text(_json.dumps({"bar": "interim-2026-05-27", "generated_at": old,
                              "n_open_events": 3, "picks": []}))
    assert signals.read_report_payload(str(p), stale_after_min=90)["stale"] is True
    fresh = datetime.now(timezone.utc).astimezone().isoformat()
    p.write_text(_json.dumps({"bar": "interim-2026-05-27", "generated_at": fresh,
                              "n_open_events": 3, "picks": []}))
    assert signals.read_report_payload(str(p), stale_after_min=90)["stale"] is False


def test_signals_payload_missing_file():
    import signals
    payload = signals.read_report_payload("does_not_exist_xyz.json", stale_after_min=90)
    assert payload["stale"] is True and payload["picks"] == []


def test_events_from_payload_extracts_events():
    import signals
    assert signals.events_from_payload({"updated": "x", "events": [{"a": 1}]}) == [{"a": 1}]
    assert signals.events_from_payload({"updated": "x"}) == []
    assert signals.events_from_payload([{"a": 1}]) == [{"a": 1}]
    assert signals.events_from_payload(None) == []
