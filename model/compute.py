"""compute(inputs, cfg) — the model pipeline.

This is the single function called by:
    - kalshi_temp.build_event_data         (live)
    - kalshi_temp.backtest_one_event       (backtest)
    - lab.replay                            (experimentation)
    - shadow.runner                         (live A/B)

Pure with respect to inputs+cfg: no I/O, no global state.
"""
import math

from . import CODE_VERSION
from .config import ModelConfig
from .primitives import (
    apply_today_max,
    bucket_bounds,
    bucket_probability,
    weighted_mean,
    weighted_std,
)
from .types import ModelInputs, ModelOutput


def _blend_sources(inputs: ModelInputs, cfg: ModelConfig) -> float | None:
    """ECMWF/GFS weighted blend per cfg, then optional NWS overlay."""
    weights = cfg.source_weights.get(inputs.series, {})
    blend = weighted_mean(inputs.forecasts, weights) if weights else None

    if blend is None:
        # Fall back to a simple mean of all available forecasts. Matches the
        # current live behaviour when both ECMWF and GFS are missing but NWS
        # (or any other source) is present.
        present = [v for v in inputs.forecasts.values() if v is not None]
        if not present:
            return None
        blend = sum(present) / len(present)

    nws = inputs.forecasts.get("nws")
    if cfg.nws_blend > 0 and nws is not None:
        blend = (1.0 - cfg.nws_blend) * blend + cfg.nws_blend * nws

    return blend


def _sigma_from(inputs: ModelInputs, cfg: ModelConfig) -> float:
    """Spread of the forecast sources, weighted the same way mu weights them.

    Each source's effective weight in mu is (1 - nws_blend) * source_weight
    for ecmwf/gfs and nws_blend for nws. Using those same weights here keeps
    sigma consistent with mu: a source mu distrusts (e.g. LAX ECMWF at 0.10)
    contributes proportionally little to the dispersion instead of the full
    equal-weight share it got before. cfg.sigma_sources stays the eligibility
    allowlist. Equal weights reduce exactly to the prior statistics.pstdev.
    """
    src_w = cfg.source_weights.get(inputs.series, {})
    eff_weights: dict[str, float] = {}
    for name in cfg.sigma_sources:
        w = cfg.nws_blend if name == "nws" else (1.0 - cfg.nws_blend) * src_w.get(name, 0.0)
        if w > 0:
            eff_weights[name] = w
    spread = weighted_std(inputs.forecasts, eff_weights) or 0.0
    return math.sqrt(cfg.base_sigma ** 2 + spread ** 2)


def compute(inputs: ModelInputs, cfg: ModelConfig) -> ModelOutput:
    mu_raw = _blend_sources(inputs, cfg)
    if mu_raw is None:
        return ModelOutput(
            mu=None, sigma=None, mu_raw=None, bias_applied=0.0,
            truncation=None, today_max_active=False, probs={},
            config_name=cfg.name, code_version=CODE_VERSION,
        )

    bias = cfg.bias_table.get(inputs.series, 0.0)
    mu = mu_raw - bias
    sigma = _sigma_from(inputs, cfg)

    truncation: float | None = None
    today_max_active = False
    if cfg.today_max_mode != "off" and inputs.today_max is not None:
        truncation, mu, today_max_active = apply_today_max(mu, inputs.today_max, cfg)

    probs: dict[str, float] = {}
    for m in inputs.markets:
        bounds = bucket_bounds(m)
        if bounds is None:
            continue
        p = bucket_probability(bounds, mu, sigma, lower_truncation=truncation)
        if p is not None:
            probs[m["ticker"]] = p

    return ModelOutput(
        mu=mu, sigma=sigma, mu_raw=mu_raw, bias_applied=bias,
        truncation=truncation, today_max_active=today_max_active,
        probs=probs, config_name=cfg.name, code_version=CODE_VERSION,
    )
