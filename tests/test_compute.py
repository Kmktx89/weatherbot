import math
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
