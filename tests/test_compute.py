import math
import statistics
import time

import pytest

from model import compute, ModelConfig, ModelInputs


def _basic_cfg(**overrides) -> ModelConfig:
    base = dict(
        name="test",
        source_weights={"KXHIGHNY": {"ecmwf": 0.5, "gfs": 0.5}},
        nws_blend=0.0,
        bias_table={},
        base_sigma=2.0,
        sigma_sources=("ecmwf", "gfs"),
        today_max_mode="off",
        today_max_headroom=0.5,
        today_max_push=0.3,
        sanity_no_yes_ask_min=0.85,
        sanity_no_prob_max=0.40,
        decision_lead_hours=24.0,
    )
    base.update(overrides)
    return ModelConfig(**base)


def _basic_inputs(**overrides) -> ModelInputs:
    base = dict(
        series="KXHIGHNY",
        event_ticker="KXHIGHNY-26MAY20",
        target_date="2026-05-20",
        forecasts={"ecmwf": 72.0, "gfs": 70.0, "nws": None},
        metar_current=None, today_max=None,
        markets=[
            {"ticker": "T-LOW", "strike_type": "less", "cap_strike": 70},
            {"ticker": "T-MID", "strike_type": "between", "floor_strike": 70, "cap_strike": 72},
            {"ticker": "T-HI",  "strike_type": "greater", "floor_strike": 72},
        ],
        decision_ts=int(time.time()),
        fetched_at=int(time.time()),
    )
    base.update(overrides)
    return ModelInputs(**base)


def test_compute_basic_no_nws_no_truncation():
    out = compute(_basic_inputs(), _basic_cfg())
    # mu = 0.5*72 + 0.5*70 = 71, sigma = sqrt(4 + pstdev([72,70])^2) = sqrt(4+1) = sqrt(5)
    assert out.mu == pytest.approx(71.0, abs=1e-9)
    assert out.sigma == pytest.approx(math.sqrt(5.0), abs=1e-9)
    assert out.mu_raw == pytest.approx(71.0, abs=1e-9)
    assert out.bias_applied == 0.0
    assert out.truncation is None and out.today_max_active is False
    assert set(out.probs.keys()) == {"T-LOW", "T-MID", "T-HI"}
    assert sum(out.probs.values()) == pytest.approx(1.0, abs=1e-9)


def test_compute_with_nws_overlay():
    inputs = _basic_inputs(forecasts={"ecmwf": 72.0, "gfs": 70.0, "nws": 75.0})
    cfg = _basic_cfg(nws_blend=0.3, sigma_sources=("ecmwf", "gfs", "nws"))
    out = compute(inputs, cfg)
    # mu_blend = 71, then 0.7*71 + 0.3*75 = 72.2
    assert out.mu_raw == pytest.approx(72.2, abs=1e-9)


def test_compute_applies_bias():
    cfg = _basic_cfg(bias_table={"KXHIGHNY": -0.44})
    out = compute(_basic_inputs(), cfg)
    # mu_raw 71, bias -0.44, subtract -> mu 71 - (-0.44) = 71.44
    assert out.mu == pytest.approx(71.44, abs=1e-9)
    assert out.bias_applied == -0.44


def test_compute_today_max_truncate_only():
    inputs = _basic_inputs(today_max=72.0)
    cfg = _basic_cfg(today_max_mode="truncate")
    out = compute(inputs, cfg)
    # truncation = 72 - 0.5 = 71.5. mu unchanged at 71. truncation > mu so active.
    assert out.truncation == pytest.approx(71.5, abs=1e-9)
    assert out.today_max_active is True
    assert out.mu == pytest.approx(71.0, abs=1e-9)


def test_compute_today_max_both_doubles_correction():
    """Reproduces the current production bug exactly."""
    inputs = _basic_inputs(today_max=72.0)
    cfg = _basic_cfg(today_max_mode="both")
    out = compute(inputs, cfg)
    # truncation = 71.5, but mu also pushed to 71.5 + 0.3 = 71.8
    assert out.truncation == pytest.approx(71.5, abs=1e-9)
    assert out.mu == pytest.approx(71.8, abs=1e-9)
    assert out.today_max_active is True


def test_compute_no_sources_returns_empty():
    inputs = _basic_inputs(forecasts={"ecmwf": None, "gfs": None, "nws": None})
    out = compute(inputs, _basic_cfg())
    assert out.mu is None and out.sigma is None and out.probs == {}


def test_compute_carries_config_name_and_code_version():
    out = compute(_basic_inputs(), _basic_cfg(name="my-config"))
    assert out.config_name == "my-config"
    from model import CODE_VERSION
    assert out.code_version == CODE_VERSION


# --- partial-source fallback paths ----------------------------------------
# All ten baseline fixtures (captured 2026-05-19) have ECMWF + GFS + NWS
# present, so they never exercise the _blend_sources fallback cascade.
# These unit tests pin the math on the three branches that survive when
# inputs are partial. Hand-derived values, so any drift in blend / sigma
# logic flips them.

def test_compute_gfs_only_renormalises_weighted_mean():
    """ECMWF=None, GFS=present: weighted_mean returns GFS unchanged
    (parts=[(75.0, 0.6)], total_w=0.6, 75*0.6/0.6 = 75.0)."""
    inputs = _basic_inputs(forecasts={"ecmwf": None, "gfs": 75.0, "nws": None})
    cfg = _basic_cfg(
        source_weights={"KXHIGHNY": {"ecmwf": 0.40, "gfs": 0.60}},
        sigma_sources=("ecmwf", "gfs", "nws"),
    )
    out = compute(inputs, cfg)
    assert out.mu_raw == pytest.approx(75.0, abs=1e-9)
    assert out.mu == pytest.approx(75.0, abs=1e-9)
    # sigma_sources = (ec, gfs, nws); only gfs present -> pstdev of single
    # element is 0; sigma = sqrt(BASE_SIGMA^2 + 0) = BASE_SIGMA = 2.0.
    assert out.sigma == pytest.approx(2.0, abs=1e-9)


def test_compute_ecmwf_only_with_nws_overlay():
    """ECMWF=present, GFS=None, NWS=present, ECMWF weight 0.10.
    mu unchanged; sigma now uses mu's effective weights, so the
    0.10-weight ECMWF barely contributes to spread."""
    inputs = _basic_inputs(forecasts={"ecmwf": 70.0, "gfs": None, "nws": 72.0})
    cfg = _basic_cfg(
        source_weights={"KXHIGHNY": {"ecmwf": 0.10, "gfs": 0.90}},
        nws_blend=0.3,
        sigma_sources=("ecmwf", "gfs", "nws"),
    )
    out = compute(inputs, cfg)
    # mu_raw unchanged: weighted_mean -> 70.0, then 0.7*70 + 0.3*72 = 70.6
    assert out.mu_raw == pytest.approx(70.6, abs=1e-9)
    # weighted sigma: eff weights ecmwf=(1-.3)*.10=.07, nws=.3 (gfs None dropped)
    w_ec, w_nws = 0.7 * 0.10, 0.3
    tw = w_ec + w_nws
    mean_w = (70.0 * w_ec + 72.0 * w_nws) / tw
    var_w = (w_ec * (70.0 - mean_w) ** 2 + w_nws * (72.0 - mean_w) ** 2) / tw
    assert out.sigma == pytest.approx(math.sqrt(2.0 ** 2 + var_w), abs=1e-9)


def test_compute_nws_only_takes_simple_mean_fallback():
    """ECMWF=None, GFS=None, NWS=present: weighted_mean returns None
    (no values in source_weights are present); fallback to simple mean
    of all present forecasts ([NWS]) then NWS overlay (no-op since
    blend == NWS already)."""
    inputs = _basic_inputs(forecasts={"ecmwf": None, "gfs": None, "nws": 82.0})
    cfg = _basic_cfg(
        source_weights={"KXHIGHNY": {"ecmwf": 0.40, "gfs": 0.60}},
        nws_blend=0.3,
        sigma_sources=("ecmwf", "gfs", "nws"),
    )
    out = compute(inputs, cfg)
    # fallback mean of [82.0] = 82.0; NWS overlay: 0.7*82 + 0.3*82 = 82.0
    assert out.mu_raw == pytest.approx(82.0, abs=1e-9)
    # sigma_sources present = [82.0]; weighted_std single survivor = 0; sigma = 2.0
    assert out.sigma == pytest.approx(2.0, abs=1e-9)


def test_compute_partial_source_with_real_bias_table():
    """Sanity check that bias_table still applies on a partial-source path."""
    inputs = _basic_inputs(forecasts={"ecmwf": None, "gfs": 75.0, "nws": None})
    cfg = _basic_cfg(
        bias_table={"KXHIGHNY": -0.44},
        source_weights={"KXHIGHNY": {"ecmwf": 0.40, "gfs": 0.60}},
    )
    out = compute(inputs, cfg)
    # mu_raw 75.0, bias -0.44, mu = 75.0 - (-0.44) = 75.44
    assert out.mu == pytest.approx(75.44, abs=1e-9)
    assert out.bias_applied == -0.44


def test_compute_ignores_metar_current():
    """metar_current is informational only; compute() must not read it.

    If this fails, the today/tomorrow separation breaks: a tomorrow-event
    snapshot carries today's current METAR temperature in its inputs, and any
    leak would pull mu toward today's temperature on a forecast that resolves
    tomorrow.
    """
    cfg = _basic_cfg(
        nws_blend=0.3,
        sigma_sources=("ecmwf", "gfs", "nws"),
        bias_table={"KXHIGHNY": -0.44},
        today_max_mode="both",
    )
    inputs_kwargs = dict(forecasts={"ecmwf": 72.0, "gfs": 70.0, "nws": 71.5})
    baseline = compute(_basic_inputs(metar_current=None, **inputs_kwargs), cfg)

    for metar in (-50.0, 0.0, 70.0, 999.0):
        out = compute(_basic_inputs(metar_current=metar, **inputs_kwargs), cfg)
        assert out.mu == baseline.mu, f"mu changed at metar_current={metar}"
        assert out.sigma == baseline.sigma, f"sigma changed at metar_current={metar}"
        assert out.mu_raw == baseline.mu_raw, f"mu_raw changed at metar_current={metar}"
        assert out.probs == baseline.probs, f"probs changed at metar_current={metar}"
        assert out.truncation == baseline.truncation
        assert out.today_max_active == baseline.today_max_active


def test_compute_downweighted_outlier_barely_moves_sigma():
    """The fix: a heavily down-weighted, far-off source (LAX-shaped:
    ECMWF +10 off at weight 0.10) must not blow up sigma the way the old
    equal-weight pstdev did."""
    inputs = _basic_inputs(forecasts={"ecmwf": 80.0, "gfs": 70.0, "nws": 70.0})
    cfg = _basic_cfg(
        base_sigma=1.0,
        source_weights={"KXHIGHNY": {"ecmwf": 0.10, "gfs": 0.90}},
        nws_blend=0.3,
        sigma_sources=("ecmwf", "gfs", "nws"),
    )
    out = compute(inputs, cfg)
    # effective weights: ecmwf .07, gfs .63, nws .30 (sum 1.0)
    w = {"ecmwf": 0.07, "gfs": 0.63, "nws": 0.30}
    mean_w = sum(v * w[k] for k, v in {"ecmwf": 80.0, "gfs": 70.0, "nws": 70.0}.items())
    var_w = sum(w[k] * (v - mean_w) ** 2
                for k, v in {"ecmwf": 80.0, "gfs": 70.0, "nws": 70.0}.items())
    expected_sigma = math.sqrt(1.0 ** 2 + var_w)
    assert out.sigma == pytest.approx(expected_sigma, abs=1e-9)
    # And it must be far below the OLD unweighted formula sqrt(1 + pstdev^2):
    old_sigma = math.sqrt(1.0 ** 2 + statistics.pstdev([80.0, 70.0, 70.0]) ** 2)
    assert out.sigma < old_sigma - 1.0   # ~2.74 vs ~4.82


def test_compute_equal_weights_sigma_unchanged():
    """DEN control: equal ECMWF/GFS weights, no NWS -> weighted std == pstdev,
    so sigma is exactly the old value."""
    inputs = _basic_inputs(forecasts={"ecmwf": 72.0, "gfs": 68.0, "nws": None})
    cfg = _basic_cfg(
        base_sigma=1.0,
        source_weights={"KXHIGHNY": {"ecmwf": 0.5, "gfs": 0.5}},
        nws_blend=0.0,
        sigma_sources=("ecmwf", "gfs"),
    )
    out = compute(inputs, cfg)
    old_sigma = math.sqrt(1.0 ** 2 + statistics.pstdev([72.0, 68.0]) ** 2)
    assert out.sigma == pytest.approx(old_sigma, abs=1e-9)  # == sqrt(1+4)
