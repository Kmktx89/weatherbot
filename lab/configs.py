"""Named ModelConfig instances. LIVE_TODAY reproduces current production
exactly; BACKTEST_TODAY reproduces the current cmd_backtest formula. New
variants are added here as additional named constants."""
from types import MappingProxyType

import kalshi_temp as _kt

from model import ModelConfig


# Translate kalshi_temp's "ecmwf_ifs025" / "gfs_seamless" keys to the short
# names compute() expects in inputs.forecasts ("ecmwf", "gfs", "nws").
_KT_SOURCE_WEIGHTS = MappingProxyType({
    series: MappingProxyType({"ecmwf": w["ecmwf_ifs025"], "gfs": w["gfs_seamless"]})
    for series, w in _kt.SOURCE_WEIGHTS.items()
})


LIVE_TODAY = ModelConfig(
    name="live-today",
    source_weights=_KT_SOURCE_WEIGHTS,
    nws_blend=0.3,
    bias_table=MappingProxyType(dict(_kt.BIAS)),
    base_sigma=_kt.BASE_SIGMA,
    sigma_sources=("ecmwf", "gfs", "nws"),
    today_max_mode="both",
    today_max_headroom=0.5,
    today_max_push=0.3,
    sanity_no_yes_ask_min=0.85,
    sanity_no_prob_max=0.40,
    decision_lead_hours=_kt.DEFAULT_LEAD_HOURS,
)


BACKTEST_TODAY = ModelConfig(
    name="backtest-today",
    source_weights=LIVE_TODAY.source_weights,
    nws_blend=0.0,
    bias_table=LIVE_TODAY.bias_table,
    base_sigma=LIVE_TODAY.base_sigma,
    sigma_sources=("ecmwf", "gfs"),
    today_max_mode="off",
    today_max_headroom=0.5,
    today_max_push=0.3,
    sanity_no_yes_ask_min=0.85,
    sanity_no_prob_max=0.40,
    decision_lead_hours=LIVE_TODAY.decision_lead_hours,
)


_BY_NAME = {c.name: c for c in (LIVE_TODAY, BACKTEST_TODAY)}


def get(name: str) -> ModelConfig:
    if name not in _BY_NAME:
        raise KeyError(f"unknown config: {name!r}. Known: {sorted(_BY_NAME)}")
    return _BY_NAME[name]


def all_configs() -> list[ModelConfig]:
    return list(_BY_NAME.values())
