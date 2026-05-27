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
