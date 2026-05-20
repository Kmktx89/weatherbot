"""Head-to-head A/B between two ModelConfigs over a set of events.

Reports both YES and NO strategy results. Bootstrap CI on PnL delta
because raw point-estimates over ~30-100 events can swing a lot from
noise.
"""
import random
from dataclasses import dataclass
from typing import Iterable, Literal

from model import ModelConfig

from .replay import replay_many, summarize, summarize_both, ReplayRecord


@dataclass
class SideComparison:
    summary_a: dict
    summary_b: dict
    pnl_delta: float
    pnl_delta_ci: tuple[float, float]
    agreement_rate: float
    decision_flips: list[dict]


@dataclass
class ComparisonResult:
    cfg_a: str
    cfg_b: str
    yes: SideComparison
    no: SideComparison


def _records_by_event(records: list[ReplayRecord]) -> dict[str, ReplayRecord]:
    return {r.event_ticker: r for r in records}


def _bootstrap_pnl_delta(
    a_records: list[ReplayRecord], b_records: list[ReplayRecord],
    side: Literal["yes", "no"],
    n: int = 1000, seed: int = 17,
) -> tuple[float, tuple[float, float]]:
    a_by = _records_by_event(a_records)
    b_by = _records_by_event(b_records)
    events = sorted(set(a_by) & set(b_by))
    pairs = []
    for e in events:
        ao = getattr(a_by[e], side)
        bo = getattr(b_by[e], side)
        ap = ao.pnl if ao.pnl is not None else 0.0
        bp = bo.pnl if bo.pnl is not None else 0.0
        pairs.append(bp - ap)
    if not pairs:
        return 0.0, (0.0, 0.0)
    mean = sum(pairs) / len(pairs)
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        s = sum(rng.choice(pairs) for _ in range(len(pairs))) / len(pairs)
        samples.append(s)
    samples.sort()
    lo = samples[int(0.025 * n)]
    hi = samples[int(0.975 * n)]
    return mean * len(pairs), (lo * len(pairs), hi * len(pairs))


def _side_comparison(a_records, b_records, side: Literal["yes", "no"],
                      bootstrap: int) -> SideComparison:
    sum_a = summarize(a_records, side=side)
    sum_b = summarize(b_records, side=side)
    a_by = _records_by_event(a_records)
    b_by = _records_by_event(b_records)
    common = sorted(set(a_by) & set(b_by))
    flips = []
    same = 0
    for e in common:
        ao = getattr(a_by[e], side)
        bo = getattr(b_by[e], side)
        if ao.pick_ticker is None or bo.pick_ticker is None:
            continue
        if ao.pick_ticker == bo.pick_ticker:
            same += 1
        else:
            flips.append({
                "event": e,
                "a_pick": ao.pick_bucket, "a_prob": ao.pick_prob,
                "a_mu": a_by[e].mu, "a_sigma": a_by[e].sigma,
                "b_pick": bo.pick_bucket, "b_prob": bo.pick_prob,
                "b_mu": b_by[e].mu, "b_sigma": b_by[e].sigma,
            })
    n_both_picked = same + len(flips)
    agreement = same / n_both_picked if n_both_picked else 0.0
    delta, ci = _bootstrap_pnl_delta(a_records, b_records, side, n=bootstrap)
    return SideComparison(
        summary_a=sum_a, summary_b=sum_b,
        pnl_delta=delta, pnl_delta_ci=ci,
        agreement_rate=agreement, decision_flips=flips,
    )


def compare(events: Iterable[str], cfg_a: ModelConfig, cfg_b: ModelConfig,
            *, bootstrap: int = 1000) -> ComparisonResult:
    events = list(events)
    a = replay_many(events, cfg_a)
    b = replay_many(events, cfg_b)
    return ComparisonResult(
        cfg_a=cfg_a.name, cfg_b=cfg_b.name,
        yes=_side_comparison(a, b, "yes", bootstrap),
        no=_side_comparison(a, b, "no", bootstrap),
    )
