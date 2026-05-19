"""Per-event prob/PnL attribution across multiple minus-one variants of LIVE_TODAY.

For each variant V:
    delta_prob(event)  = P_LIVE(winner) - P_V(winner)
    delta_pnl(event)   = pnl_LIVE - pnl_V
Aggregated across events, this attributes the live-vs-V gap to the component
that V removed.
"""
from typing import Iterable

from model import ModelConfig

from .replay import replay_many


def decompose(events: Iterable[str], baseline: ModelConfig,
              variants: list[ModelConfig]) -> dict:
    events = list(events)
    base_records = {r.event_ticker: r for r in replay_many(events, baseline)}
    out_per_variant: list[dict] = []
    for v in variants:
        v_records = {r.event_ticker: r for r in replay_many(events, v)}
        rows = []
        total_pnl_delta = 0.0
        n_flips = 0
        for e in events:
            br = base_records.get(e)
            vr = v_records.get(e)
            if br is None or vr is None:
                continue
            base_pnl = br.pnl if br.pnl is not None else 0.0
            v_pnl = vr.pnl if vr.pnl is not None else 0.0
            total_pnl_delta += base_pnl - v_pnl
            if br.pick_ticker and vr.pick_ticker and br.pick_ticker != vr.pick_ticker:
                n_flips += 1
            rows.append({
                "event": e,
                "base_pick": br.pick_bucket, "base_prob": br.pick_prob, "base_pnl": br.pnl,
                "var_pick": vr.pick_bucket, "var_prob": vr.pick_prob, "var_pnl": vr.pnl,
            })
        out_per_variant.append({
            "variant": v.name, "pnl_delta_attrib": total_pnl_delta,
            "decision_flips": n_flips, "rows": rows,
        })
    return {"baseline": baseline.name, "variants": out_per_variant}
