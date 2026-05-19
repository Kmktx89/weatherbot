"""LIVE_TODAY and BACKTEST_TODAY must reproduce the current kalshi_temp.py
constants exactly. If you change kalshi_temp's SOURCE_WEIGHTS, BIAS, or
BASE_SIGMA, mirror it here and re-record baselines."""

import kalshi_temp as kt

from lab.configs import LIVE_TODAY, BACKTEST_TODAY


def test_live_today_mirrors_source_weights():
    assert LIVE_TODAY.source_weights == {
        k: {"ecmwf": v["ecmwf_ifs025"], "gfs": v["gfs_seamless"]}
        for k, v in kt.SOURCE_WEIGHTS.items()
    }

def test_live_today_mirrors_bias_table():
    assert LIVE_TODAY.bias_table == kt.BIAS

def test_live_today_base_sigma_matches_kt():
    assert LIVE_TODAY.base_sigma == kt.BASE_SIGMA

def test_live_today_decision_lead_matches_kt():
    assert LIVE_TODAY.decision_lead_hours == kt.DEFAULT_LEAD_HOURS

def test_live_today_uses_nws_overlay_and_both_today_max():
    assert LIVE_TODAY.nws_blend == 0.3
    assert LIVE_TODAY.today_max_mode == "both"
    assert LIVE_TODAY.sigma_sources == ("ecmwf", "gfs", "nws")

def test_backtest_today_skips_nws_and_today_max():
    assert BACKTEST_TODAY.nws_blend == 0.0
    assert BACKTEST_TODAY.today_max_mode == "off"
    assert BACKTEST_TODAY.sigma_sources == ("ecmwf", "gfs")
    # Same bias table, same source weights, same base sigma.
    assert BACKTEST_TODAY.source_weights == LIVE_TODAY.source_weights
    assert BACKTEST_TODAY.bias_table == LIVE_TODAY.bias_table
    assert BACKTEST_TODAY.base_sigma == LIVE_TODAY.base_sigma
