"""Sweep one ModelConfig parameter over a range and report both YES and NO
strategy performance per value. The cheapest iterative-tightening loop:

  python -m lab sweep --param decision_lead_hours --values 12,18,24,36 --days 60

Same machinery as compare/decompose but parameterised: a base config + a
field + a list of values produces N variants. Replays each over the same
event set so the comparison is apples-to-apples.
"""
from collections import defaultdict
from dataclasses import replace
from typing import Iterable, Any

from model import ModelConfig

from .replay import ReplayRecord, replay_many, summarize, summarize_both


def _coerce_value(field: str, raw: str) -> Any:
    """Best-effort string → typed value for known ModelConfig fields."""
    if field in ("nws_blend", "base_sigma",
                 "today_max_headroom", "today_max_push",
                 "sanity_no_yes_ask_min", "sanity_no_prob_max",
                 "decision_lead_hours"):
        return float(raw)
    return raw


def _per_series_breakdown(records: list[ReplayRecord], side: str) -> dict:
    by_series = defaultdict(list)
    for r in records:
        if r.series:
            by_series[r.series].append(r)
    return {s: summarize(rs, side=side) for s, rs in sorted(by_series.items())}


def sweep(events: Iterable[str], base_cfg: ModelConfig,
          param: str, values: list[Any],
          *, by_series: bool = False) -> dict:
    """Run replay for each (param=value) variant of base_cfg. Returns a dict
    mapping str(value) -> per-value report."""
    events = list(events)
    if not hasattr(base_cfg, param):
        raise ValueError(f"unknown ModelConfig field: {param}")
    per_value = {}
    for v in values:
        coerced = _coerce_value(param, v) if isinstance(v, str) else v
        variant = replace(base_cfg, name=f"{base_cfg.name}__{param}={coerced}",
                          **{param: coerced})
        records = replay_many(events, variant)
        entry = {
            "value": coerced,
            "config_name": variant.name,
            **summarize_both(records),
        }
        if by_series:
            entry["yes_by_series"] = _per_series_breakdown(records, "yes")
            entry["no_by_series"]  = _per_series_breakdown(records, "no")
        per_value[str(coerced)] = entry
    return {"base_config": base_cfg.name, "param": param,
            "n_events": len(events), "values": per_value}


def best_by_pnl(report: dict, side: str = "yes") -> tuple[str, dict] | None:
    candidates = [(v_str, entry) for v_str, entry in report["values"].items()
                  if entry[side]["bets"] > 0]
    if not candidates:
        return None
    return max(candidates, key=lambda kv: kv[1][side]["total_pnl"])
