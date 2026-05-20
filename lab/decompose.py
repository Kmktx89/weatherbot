"""Per-event PnL attribution across multiple minus-one variants of LIVE_TODAY.

For each variant V and each side (YES, NO):
    delta_pnl(event) = pnl_LIVE - pnl_V
Aggregated across events, this attributes the live-vs-V gap to the
component that V removed.
"""
from typing import Iterable

from model import ModelConfig

from .replay import replay_many


def _side_attrib(base_records, var_records, side: str) -> dict:
    base_by = {r.event_ticker: r for r in base_records}
    var_by = {r.event_ticker: r for r in var_records}
    events = sorted(set(base_by) & set(var_by))
    rows = []
    total_pnl_delta = 0.0
    n_flips = 0
    for e in events:
        bo = getattr(base_by[e], side)
        vo = getattr(var_by[e], side)
        base_pnl = bo.pnl if bo.pnl is not None else 0.0
        var_pnl = vo.pnl if vo.pnl is not None else 0.0
        total_pnl_delta += base_pnl - var_pnl
        if bo.pick_ticker and vo.pick_ticker and bo.pick_ticker != vo.pick_ticker:
            n_flips += 1
        rows.append({
            "event": e,
            "base_pick": bo.pick_bucket, "base_prob": bo.pick_prob, "base_pnl": bo.pnl,
            "var_pick": vo.pick_bucket, "var_prob": vo.pick_prob, "var_pnl": vo.pnl,
        })
    return {"pnl_delta_attrib": total_pnl_delta,
            "decision_flips": n_flips, "rows": rows}


def decompose(events: Iterable[str], baseline: ModelConfig,
              variants: list[ModelConfig]) -> dict:
    events = list(events)
    base_records = replay_many(events, baseline)
    out_per_variant: list[dict] = []
    for v in variants:
        v_records = replay_many(events, v)
        out_per_variant.append({
            "variant": v.name,
            "yes": _side_attrib(base_records, v_records, "yes"),
            "no":  _side_attrib(base_records, v_records, "no"),
        })
    return {"baseline": baseline.name, "variants": out_per_variant}
