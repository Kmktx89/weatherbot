"""Head-to-head A/B between two ModelConfigs over a set of events.

Bootstrap CI on PnL delta because raw point-estimates over ~30-100 events
can swing a lot from noise.
"""
import random
from dataclasses import dataclass
from typing import Iterable

from model import ModelConfig

from .replay import replay_many, summarize, ReplayRecord


@dataclass
class ComparisonResult:
    cfg_a: str
    cfg_b: str
    summary_a: dict
    summary_b: dict
    pnl_delta: float
    pnl_delta_ci: tuple[float, float]
    agreement_rate: float
    decision_flips: list[dict]


def _records_by_event(records: list[ReplayRecord]) -> dict[str, ReplayRecord]:
    return {r.event_ticker: r for r in records}


def _bootstrap_pnl_delta(
    a_records: list[ReplayRecord], b_records: list[ReplayRecord],
    n: int = 1000, seed: int = 17,
) -> tuple[float, tuple[float, float]]:
    a_by = _records_by_event(a_records)
    b_by = _records_by_event(b_records)
    events = sorted(set(a_by) & set(b_by))
    pairs = []
    for e in events:
        ap = a_by[e].pnl if a_by[e].pnl is not None else 0.0
        bp = b_by[e].pnl if b_by[e].pnl is not None else 0.0
        pairs.append(bp - ap)  # delta = B - A
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


def compare(events: Iterable[str], cfg_a: ModelConfig, cfg_b: ModelConfig,
            *, bootstrap: int = 1000) -> ComparisonResult:
    events = list(events)
    a = replay_many(events, cfg_a)
    b = replay_many(events, cfg_b)

    sum_a, sum_b = summarize(a), summarize(b)

    a_by = _records_by_event(a)
    b_by = _records_by_event(b)
    common = sorted(set(a_by) & set(b_by))
    flips = []
    same = 0
    for e in common:
        if a_by[e].pick_ticker is None or b_by[e].pick_ticker is None:
            continue
        if a_by[e].pick_ticker == b_by[e].pick_ticker:
            same += 1
        else:
            flips.append({
                "event": e,
                "a_pick": a_by[e].pick_bucket, "a_prob": a_by[e].pick_prob,
                "a_mu": a_by[e].mu, "a_sigma": a_by[e].sigma,
                "b_pick": b_by[e].pick_bucket, "b_prob": b_by[e].pick_prob,
                "b_mu": b_by[e].mu, "b_sigma": b_by[e].sigma,
            })
    n_both_picked = same + len(flips)
    agreement = same / n_both_picked if n_both_picked else 0.0

    delta, ci = _bootstrap_pnl_delta(a, b, n=bootstrap)
    return ComparisonResult(
        cfg_a=cfg_a.name, cfg_b=cfg_b.name,
        summary_a=sum_a, summary_b=sum_b,
        pnl_delta=delta, pnl_delta_ci=ci,
        agreement_rate=agreement, decision_flips=flips,
    )
