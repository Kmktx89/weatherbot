"""Characterization tests for the shared threshold module (Problem 3).

Two guarantees:
1. Every shared value equals its pre-consolidation number (silent drift = the
   failure mode; this pins the numbers).
2. Every consumer (kalshi_temp, lab.replay, lab.health) reads the SAME value from
   wb_thresholds — no module redefines a threshold literal. Backtest == live ==
   health-"taken" share one definition.
"""
import wb_thresholds as T


def test_canonical_values_pinned():
    assert T.MIN_BEST_EV == 0.05
    assert T.SANITY_MARKET_CONFIDENT_YES == 0.85
    assert T.SANITY_MODEL_LOW_PROB == 0.40
    assert T.MIN_PRINTED_NO == 0.80
    assert T.BASE_SIGMA == 1.0
    assert T.NO_HAIRCUT == 0.11
    assert T.MIN_N == 30
    assert T.CAL_WATCH_PP == 5.0
    assert T.CAL_ALERT_PP == 10.0
    assert T.K_OK == (0.8, 1.25)
    assert T.K_WATCH == (0.65, 1.4)
    assert T.BIAS_WATCH == 0.5
    assert T.BIAS_ALERT == 1.0
    assert T.OPP_EDGE_MIN == 0.05


def test_kalshi_temp_reexports_shared():
    import kalshi_temp as kt
    assert kt.MIN_BEST_EV == T.MIN_BEST_EV
    assert kt.SANITY_MARKET_CONFIDENT_YES == T.SANITY_MARKET_CONFIDENT_YES
    assert kt.SANITY_MODEL_LOW_PROB == T.SANITY_MODEL_LOW_PROB
    assert kt.MIN_PRINTED_NO == T.MIN_PRINTED_NO
    assert kt.BASE_SIGMA == T.BASE_SIGMA
    # NO haircut band wired to the shared constants (no magic 0.80 / 0.11)
    no_band = kt.DEFAULT_CAL_PARAMS["no"][0]
    assert no_band["lo"] == T.MIN_PRINTED_NO
    assert no_band["h"] == T.NO_HAIRCUT


def test_lab_replay_reexports_shared():
    import lab.replay as r
    assert r.MIN_BEST_EV == T.MIN_BEST_EV
    assert r.SANITY_MARKET_CONFIDENT_YES == T.SANITY_MARKET_CONFIDENT_YES
    assert r.SANITY_MODEL_LOW_PROB == T.SANITY_MODEL_LOW_PROB


def test_lab_health_reexports_shared():
    import lab.health as h
    assert h.MIN_N == T.MIN_N
    assert h.CAL_WATCH_PP == T.CAL_WATCH_PP
    assert h.CAL_ALERT_PP == T.CAL_ALERT_PP
    assert h.K_OK == T.K_OK
    assert h.K_WATCH == T.K_WATCH
    assert h.BIAS_WATCH == T.BIAS_WATCH
    assert h.BIAS_ALERT == T.BIAS_ALERT
    assert h.OPP_EDGE_MIN == T.OPP_EDGE_MIN


def test_lab_configs_base_sigma_is_shared():
    """lab.configs inherits BASE_SIGMA via kt — confirm it tracks the shared value."""
    from lab.configs import LIVE_TODAY, BACKTEST_TODAY
    assert LIVE_TODAY.base_sigma == T.BASE_SIGMA
    assert BACKTEST_TODAY.base_sigma == T.BASE_SIGMA
