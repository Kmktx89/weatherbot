"""Tests for the calibrated-EV selection layer in kalshi_temp.py."""
import json

import kalshi_temp as kt


# ---------- params loader ----------

def test_load_params_missing_file_returns_default(tmp_path):
    p = tmp_path / "nope.json"
    assert kt.load_calibration_params(str(p)) == kt.DEFAULT_CAL_PARAMS


def test_load_params_malformed_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert kt.load_calibration_params(str(p)) == kt.DEFAULT_CAL_PARAMS


def test_load_params_valid_file_parsed(tmp_path):
    p = tmp_path / "cal.json"
    payload = {"no": [{"lo": 0.80, "hi": 1.01, "h": 0.09}],
               "yes": [{"lo": 0.0, "hi": 1.01, "h": 0.0}]}
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert kt.load_calibration_params(str(p)) == payload


def test_haircut_for_band_match_and_miss():
    params = {"no": [{"lo": 0.80, "hi": 1.01, "h": 0.11}],
              "yes": [{"lo": 0.0, "hi": 1.01, "h": 0.0}]}
    assert kt.haircut_for("no", 0.89, params) == 0.11   # in band
    assert kt.haircut_for("no", 0.50, params) == 0.0    # below band -> no match
    assert kt.haircut_for("yes", 0.42, params) == 0.0   # identity band


# ---------- apply_calibration ----------

PARAMS = {"no": [{"lo": 0.80, "hi": 1.01, "h": 0.11}],
          "yes": [{"lo": 0.0, "hi": 1.01, "h": 0.0}]}


def test_apply_calibration_no_haircut_shrinks_no_prob_and_ev():
    m = {"prob": 0.11, "yes_ask": 0.13, "no_ask": 0.82}
    kt.apply_calibration(m, PARAMS)
    # printed_no = 0.89; cal_prob_no = 0.89 - 0.11 = 0.78
    assert abs(m["cal_prob_no"] - 0.78) < 1e-9
    # cal_ev_no = 0.78 - 0.82 = -0.04  (raw ev_no was +0.07)
    assert abs(m["cal_ev_no"] - (-0.04)) < 1e-9


def test_apply_calibration_yes_identity_is_exact():
    m = {"prob": 0.576, "yes_ask": 0.88, "no_ask": 0.14}
    kt.apply_calibration(m, PARAMS)
    # h_yes = 0 -> cal must equal raw bit-for-bit
    assert m["cal_prob_yes"] == 0.576
    assert m["cal_ev_yes"] == 0.576 - 0.88


def test_apply_calibration_none_prob_yields_none_fields():
    m = {"prob": None, "yes_ask": None, "no_ask": None}
    kt.apply_calibration(m, PARAMS)
    assert m["cal_prob_no"] is None and m["cal_ev_no"] is None
    assert m["cal_prob_yes"] is None and m["cal_ev_yes"] is None


def test_apply_calibration_is_idempotent():
    m = {"prob": 0.11, "yes_ask": 0.13, "no_ask": 0.82}
    kt.apply_calibration(m, PARAMS)
    first = dict(m)
    kt.apply_calibration(m, PARAMS)
    assert m == first


def test_apply_calibration_clamps_to_unit_interval():
    # huge haircut would push below 0; must clamp
    params = {"no": [{"lo": 0.0, "hi": 1.01, "h": 5.0}], "yes": [{"lo": 0.0, "hi": 1.01, "h": 0.0}]}
    m = {"prob": 0.50, "yes_ask": 0.50, "no_ask": 0.50}
    kt.apply_calibration(m, params)
    assert m["cal_prob_no"] == 0.0


# ---------- selection routes through calibrated EV ----------

def _mkt(ticker, prob, yes_ask, no_ask):
    return {"ticker": ticker, "subtitle": ticker, "prob": prob,
            "yes_ask": yes_ask, "no_ask": no_ask,
            "ev_yes": (prob - yes_ask), "ev_no": ((1 - prob) - no_ask)}


def test_best_no_pick_suppresses_negative_calibrated_ev(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    # raw ev_no = +7c but cal_ev_no = -4c -> must be dropped by the 5c floor
    losing = _mkt("A", prob=0.11, yes_ask=0.13, no_ask=0.82)
    # raw ev_no = +17c, cal_ev_no = 0.84 - 0.78 = +6c -> survives
    winning = _mkt("B", prob=0.05, yes_ask=0.06, no_ask=0.78)
    pick = kt.best_no_pick([losing, winning])
    assert pick is not None and pick["ticker"] == "B"


def test_best_no_pick_lazy_computes_cal_fields_on_raw_rows(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    row = _mkt("B", prob=0.05, yes_ask=0.06, no_ask=0.78)  # no cal_* keys
    assert "cal_ev_no" not in row
    pick = kt.best_no_pick([row])
    assert pick is not None and "cal_ev_no" in pick


def test_best_no_pick_keeps_printed_no_floor(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    # printed_no = 0.79 < 0.80 floor -> never surfaces regardless of EV
    below = _mkt("C", prob=0.21, yes_ask=0.05, no_ask=0.05)
    assert kt.best_no_pick([below]) is None


def test_best_yes_pick_matches_raw_ev_yes_under_identity(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    a = _mkt("A", prob=0.60, yes_ask=0.50, no_ask=0.45)  # ev_yes = +10c
    b = _mkt("B", prob=0.30, yes_ask=0.28, no_ask=0.70)  # ev_yes = +2c
    pick = kt.best_yes_pick([a, b])
    assert pick is not None and pick["ticker"] == "A"


# ---------- predict_summary ----------

def test_predict_summary_shape_and_picks(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    markets = [
        _mkt("TOP", prob=0.60, yes_ask=0.50, no_ask=0.45),   # highest prob + best YES
        _mkt("NO", prob=0.05, yes_ask=0.06, no_ask=0.78),    # survives NO floor+cal
    ]
    s = kt.predict_summary(markets)
    assert set(s) == {"highest_probability", "best_ev_yes", "best_ev_no"}
    assert s["highest_probability"]["ticker"] == "TOP"
    assert s["best_ev_yes"]["ticker"] == "TOP"
    assert s["best_ev_no"]["ticker"] == "NO"


def test_predict_summary_empty_markets():
    s = kt.predict_summary([])
    assert s == {"highest_probability": None, "best_ev_yes": None, "best_ev_no": None}


# ---------- finalize maps apply_calibration over a markets list ----------

def test_finalize_markets_adds_cal_fields(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    markets = [_mkt("A", 0.11, 0.13, 0.82), {"ticker": "S", "prob": None,
               "yes_ask": None, "no_ask": None}]
    kt.finalize_markets(markets)
    assert "cal_ev_no" in markets[0] and markets[0]["cal_ev_no"] is not None
    assert markets[1]["cal_ev_no"] is None  # settled/None-prob row tolerated
