"""Refit the BIAS table for an arbitrary ModelConfig using settled events.

For each event:
    mu_pre_bias = compute(inputs, replace(cfg, bias_table={}))     # zero-bias model
    actual      = bucket_midpoint(winning_market)
    error       = mu_pre_bias - actual

Per-city bias = mean(error) over events for that city.

Print a paste-ready dict. Does NOT modify lab/configs.py.
"""
from dataclasses import replace
import statistics

from model import ModelConfig, compute

from .inputs import build_historical_inputs, winner_of


def actual_high_midpoint(winner: dict) -> float | None:
    st = winner.get("strike_type")
    floor, cap = winner.get("floor_strike"), winner.get("cap_strike")
    if st == "between" and floor is not None and cap is not None:
        return (floor + cap) / 2.0
    if st == "less" and cap is not None:
        return cap - 1.0
    if st == "greater" and floor is not None:
        return floor + 1.0
    return None


def refit(event_tickers: list[str], cfg: ModelConfig) -> dict[str, dict]:
    """Returns {series: {bias, n, sd}}."""
    zero_bias_cfg = replace(cfg, name=f"{cfg.name}__nobias", bias_table={})
    per_series: dict[str, list[float]] = {}
    for t in event_tickers:
        inputs = build_historical_inputs(t)
        if inputs is None:
            continue
        winner = winner_of(list(inputs.markets))
        if winner is None:
            continue
        actual = actual_high_midpoint(winner)
        if actual is None:
            continue
        out = compute(inputs, zero_bias_cfg)
        if out.mu is None:
            continue
        per_series.setdefault(inputs.series, []).append(out.mu - actual)

    table: dict[str, dict] = {}
    for s, errs in per_series.items():
        if not errs:
            continue
        bias = statistics.mean(errs)
        sd = statistics.pstdev(errs) if len(errs) > 1 else 0.0
        table[s] = {"bias": round(bias, 2), "n": len(errs), "sd": round(sd, 2)}
    return table
