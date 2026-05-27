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
