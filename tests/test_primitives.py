import math

from model.primitives import normal_cdf, weighted_mean


def test_normal_cdf_at_mean_is_half():
    assert normal_cdf(0.0, 0.0, 1.0) == 0.5

def test_normal_cdf_monotonic():
    samples = [normal_cdf(x, 0.0, 1.0) for x in (-3, -1, 0, 1, 3)]
    assert all(a <= b for a, b in zip(samples, samples[1:]))

def test_normal_cdf_extremes():
    assert normal_cdf(-10.0, 0.0, 1.0) < 1e-15
    assert normal_cdf(10.0, 0.0, 1.0) > 1 - 1e-15

def test_normal_cdf_sigma_zero_degenerate():
    # σ=0 is a point mass at μ; CDF is a step function.
    assert normal_cdf(5.0, 5.0, 0.0) == 1.0
    assert normal_cdf(4.9, 5.0, 0.0) == 0.0
    assert normal_cdf(5.1, 5.0, 0.0) == 1.0


def test_weighted_mean_all_present():
    out = weighted_mean({"a": 10.0, "b": 20.0}, {"a": 0.3, "b": 0.7})
    assert out == pytest_approx(17.0)

def test_weighted_mean_one_missing():
    out = weighted_mean({"a": 10.0, "b": None}, {"a": 0.3, "b": 0.7})
    assert out == pytest_approx(10.0)  # renormalised to weight 'a' alone

def test_weighted_mean_all_missing_returns_none():
    assert weighted_mean({"a": None, "b": None}, {"a": 0.3, "b": 0.7}) is None

def test_weighted_mean_zero_total_weight_returns_none():
    assert weighted_mean({"a": 10.0}, {"a": 0.0}) is None

def test_weighted_mean_ignores_unknown_keys():
    out = weighted_mean({"a": 1.0, "z": 99.0}, {"a": 1.0})
    assert out == pytest_approx(1.0)


def pytest_approx(v, tol=1e-9):
    from pytest import approx
    return approx(v, abs=tol)


from model.primitives import bucket_bounds, bucket_probability


# --- bucket_bounds ----------------------------------------------------

def test_bucket_bounds_less():
    assert bucket_bounds({"strike_type": "less", "cap_strike": 50}) == (
        float("-inf"), 49.5
    )

def test_bucket_bounds_greater():
    assert bucket_bounds({"strike_type": "greater", "floor_strike": 80}) == (
        80.5, float("inf")
    )

def test_bucket_bounds_between():
    assert bucket_bounds({"strike_type": "between", "floor_strike": 70,
                          "cap_strike": 72}) == (69.5, 72.5)

def test_bucket_bounds_missing_strike_returns_none():
    assert bucket_bounds({"strike_type": "less"}) is None
    assert bucket_bounds({"strike_type": "between", "floor_strike": 70}) is None
    assert bucket_bounds({"strike_type": "weird"}) is None


# --- bucket_probability ----------------------------------------------

def test_bucket_probability_untruncated_normal():
    p = bucket_probability((69.5, 72.5), mu=71.0, sigma=2.0)
    # P(69.5 < X < 72.5) where X ~ N(71, 4)
    assert 0.4 < p < 0.6

def test_bucket_probability_truncated_below_mu():
    # truncation 67 means condition on X >= 67. Bucket above truncation.
    p_t = bucket_probability((69.5, 72.5), mu=71.0, sigma=2.0, lower_truncation=67.0)
    p_u = bucket_probability((69.5, 72.5), mu=71.0, sigma=2.0)
    # Conditional probability should be higher than unconditional.
    assert p_t > p_u

def test_bucket_probability_bucket_entirely_below_truncation():
    p = bucket_probability((60.0, 65.0), mu=71.0, sigma=2.0, lower_truncation=68.0)
    assert p == 0.0

def test_bucket_probability_truncation_above_bucket_upper():
    # Bucket lives wholly below the truncation -> 0
    assert bucket_probability((60.0, 65.0), mu=71.0, sigma=2.0,
                              lower_truncation=66.0) == 0.0

def test_bucket_probability_sigma_zero_point_mass():
    # At sigma=0, prob is 1.0 if mu falls in the bucket, else 0.
    assert bucket_probability((69.5, 72.5), mu=71.0, sigma=0.0) == 1.0
    assert bucket_probability((60.0, 65.0), mu=71.0, sigma=0.0) == 0.0


from model.primitives import apply_today_max, event_local_date


# --- apply_today_max --------------------------------------------------

def _mode(name, headroom=0.5, push=0.3):
    from model.config import ModelConfig
    return ModelConfig(
        name="t", source_weights={}, nws_blend=0.0, bias_table={},
        base_sigma=2.0, sigma_sources=("ecmwf", "gfs"),
        today_max_mode=name, today_max_headroom=headroom, today_max_push=push,
        sanity_no_yes_ask_min=0.85, sanity_no_prob_max=0.40,
        decision_lead_hours=24.0,
    )

def test_apply_today_max_off():
    trunc, mu, active = apply_today_max(mu=71.0, today_max=78.0, cfg=_mode("off"))
    assert trunc is None and mu == 71.0 and active is False

def test_apply_today_max_truncate_above_mu():
    # today_max=78, headroom=0.5 -> truncation=77.5. mu=71, so truncation > mu.
    trunc, mu, active = apply_today_max(71.0, 78.0, _mode("truncate"))
    assert trunc == 77.5 and mu == 71.0 and active is True

def test_apply_today_max_truncate_below_mu():
    trunc, mu, active = apply_today_max(80.0, 70.0, _mode("truncate"))
    assert trunc == 69.5 and mu == 80.0 and active is False

def test_apply_today_max_push_above_mu():
    trunc, mu, active = apply_today_max(71.0, 78.0, _mode("push"))
    assert trunc is None and mu == 77.5 + 0.3 and active is True

def test_apply_today_max_push_below_mu():
    trunc, mu, active = apply_today_max(80.0, 70.0, _mode("push"))
    assert trunc is None and mu == 80.0 and active is False

def test_apply_today_max_both_above_mu():
    # current production behaviour (the double-counting case)
    trunc, mu, active = apply_today_max(71.0, 78.0, _mode("both"))
    assert trunc == 77.5 and mu == 77.5 + 0.3 and active is True

def test_apply_today_max_both_below_mu():
    trunc, mu, active = apply_today_max(80.0, 70.0, _mode("both"))
    assert trunc == 69.5 and mu == 80.0 and active is False


# --- event_local_date -------------------------------------------------

def test_event_local_date_ticker_suffix():
    assert event_local_date({"event_ticker": "KXHIGHNY-26MAY13"}) == "2026-05-13"
    assert event_local_date({"event_ticker": "KXHIGHCHI-26JAN05"}) == "2026-01-05"

def test_event_local_date_strike_fallback():
    ev = {"event_ticker": "KXHIGHNY-BAD", "strike_date": "2026-05-14T05:00:00Z"}
    # strike_date 05:00Z = 01:00 ET = day-after close, so target should be prior day
    assert event_local_date(ev) == "2026-05-13"

def test_event_local_date_unparseable_returns_today_utc():
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    assert event_local_date({"event_ticker": "GARBAGE"}) == today
