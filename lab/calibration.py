"""Forecast calibration analysis for a ModelConfig over settled events.

Compares the model's PREDICTED probability of the picked bucket to the
REALIZED frequency at which the pick won. If the model says 70% on
average across N bets and the actual win rate on those bets is 65%, the
model is 5pp overconfident.

Also computes Brier score and per-decile calibration bins. Run separately
for YES and NO sides because they have different selection rules and
different "won" definitions.

YES bet:  pick = argmax(P_yes per bucket); won = pick == winner
NO bet:   pick = argmax(EV_NO per qualifying bucket); won = pick != winner;
          the relevant model probability is (1 - P_yes_pick), i.e. the
          model's confidence that the bucket WON'T win.
"""
import math
from dataclasses import dataclass
from typing import Iterable, Literal

from model import ModelConfig

from .replay import ReplayRecord, replay_many


@dataclass
class CalibrationBin:
    lo: float
    hi: float
    n: int
    mean_pred: float
    realized_rate: float

    @property
    def gap(self) -> float:
        return self.realized_rate - self.mean_pred


@dataclass
class CalibrationReport:
    side: Literal["yes", "no"]
    n_bets: int
    mean_pred: float
    realized_rate: float
    brier_score: float
    log_loss: float | None
    bins: list[CalibrationBin]


def _bins(pairs: list[tuple[float, int]], n_bins: int = 10) -> list[CalibrationBin]:
    """Bin (pred_prob, won_int) pairs by predicted probability decile."""
    bins: list[CalibrationBin] = []
    for i in range(n_bins):
        lo = i / n_bins
        hi = (i + 1) / n_bins
        # Last bin is inclusive of 1.0
        in_bin = [(p, w) for p, w in pairs
                  if (lo <= p < hi) or (i == n_bins - 1 and p == 1.0)]
        if not in_bin:
            bins.append(CalibrationBin(lo, hi, 0, 0.0, 0.0))
            continue
        mp = sum(p for p, _ in in_bin) / len(in_bin)
        rr = sum(w for _, w in in_bin) / len(in_bin)
        bins.append(CalibrationBin(lo, hi, len(in_bin), mp, rr))
    return bins


def calibrate(records: list[ReplayRecord], side: Literal["yes", "no"]) -> CalibrationReport:
    pairs: list[tuple[float, int]] = []
    for r in records:
        o = getattr(r, side)
        if o.pick_prob is None or o.won is None:
            continue
        pairs.append((o.pick_prob, 1 if o.won else 0))
    if not pairs:
        return CalibrationReport(side=side, n_bets=0, mean_pred=0.0,
                                 realized_rate=0.0, brier_score=0.0,
                                 log_loss=None, bins=_bins([]))
    n = len(pairs)
    mean_pred = sum(p for p, _ in pairs) / n
    realized = sum(w for _, w in pairs) / n
    brier = sum((p - w) ** 2 for p, w in pairs) / n
    # log loss is undefined at p=0 or p=1; clip for numerical safety
    eps = 1e-9
    log_loss = -sum(
        w * math.log(max(p, eps)) + (1 - w) * math.log(max(1 - p, eps))
        for p, w in pairs
    ) / n
    return CalibrationReport(
        side=side, n_bets=n, mean_pred=mean_pred, realized_rate=realized,
        brier_score=brier, log_loss=log_loss, bins=_bins(pairs),
    )


def calibrate_at_leads(events: Iterable[str], base_cfg: ModelConfig,
                        lead_hours: list[float]) -> dict[float, dict]:
    """Run calibration at each lead time. Returns {lead: {yes: CalibrationReport, no: CalibrationReport}}."""
    from dataclasses import replace
    events = list(events)
    out: dict[float, dict] = {}
    for h in lead_hours:
        cfg = replace(base_cfg, name=f"{base_cfg.name}__lead={h}",
                      decision_lead_hours=h)
        records = replay_many(events, cfg)
        out[h] = {
            "yes": calibrate(records, "yes"),
            "no":  calibrate(records, "no"),
        }
    return out
