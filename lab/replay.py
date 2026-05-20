"""Replay a ModelConfig over a list of settled event tickers.

For each event:
    - build inputs from cached/fresh sources
    - run compute(inputs, cfg)
    - pick the highest-prob bucket (the YES strategy)
    - look up the entry price at T-cfg.decision_lead_hours
    - resolve win/loss against the settled winner
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from model import ModelConfig, compute

from .data_cache import default as default_cache
from .inputs import build_historical_inputs, fetch_yes_ask_at, winner_of


@dataclass
class ReplayRecord:
    event_ticker: str
    series: str
    target_date: str
    mu: float | None
    sigma: float | None
    pick_ticker: str | None
    pick_prob: float | None
    pick_bucket: str | None
    entry_price: float | None
    won: bool | None
    pnl: float | None
    skip: str | None


def replay_one(event_ticker: str, cfg: ModelConfig) -> ReplayRecord:
    inputs = build_historical_inputs(event_ticker)
    if inputs is None:
        return ReplayRecord(event_ticker, "", "", None, None, None, None, None, None, None, None, "no_inputs")

    winner = winner_of(list(inputs.markets))
    if winner is None:
        return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                            None, None, None, None, None, None, None, None, "not_settled")

    out = compute(inputs, cfg)
    if out.mu is None or not out.probs:
        return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                            None, None, None, None, None, None, None, None, "no_model")

    # decision_ts = close - lead_hours
    close_dt = datetime.fromisoformat(inputs.markets[0]["close_time"].replace("Z", "+00:00"))
    decision_ts = int(close_dt.timestamp() - cfg.decision_lead_hours * 3600)

    pick_ticker = max(out.probs, key=lambda t: out.probs[t])
    pick_prob = out.probs[pick_ticker]
    pick_market = next((m for m in inputs.markets if m["ticker"] == pick_ticker), None)
    pick_bucket = (pick_market.get("subtitle") if pick_market else None) or "?"

    yes_ask = fetch_yes_ask_at(inputs.series, pick_ticker, decision_ts, default_cache())
    if yes_ask is None or yes_ask <= 0 or yes_ask >= 1:
        return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                            out.mu, out.sigma, pick_ticker, pick_prob, pick_bucket,
                            None, None, None, "no_price")

    won = pick_ticker == winner["ticker"]
    pnl = (1.0 - yes_ask) if won else -yes_ask
    return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                        out.mu, out.sigma, pick_ticker, pick_prob, pick_bucket,
                        yes_ask, won, pnl, None)


def replay_many(event_tickers: Iterable[str], cfg: ModelConfig) -> list[ReplayRecord]:
    return [replay_one(t, cfg) for t in event_tickers]


def summarize(records: list[ReplayRecord]) -> dict:
    bets = [r for r in records if r.pnl is not None]
    if not bets:
        return {"events": len(records), "bets": 0, "wins": 0, "win_rate": 0.0,
                "total_pnl": 0.0, "avg_pnl": 0.0, "max_drawdown": 0.0}
    wins = sum(1 for r in bets if r.won)
    pnls = [r.pnl for r in bets]
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "events": len(records), "bets": len(bets), "wins": wins,
        "win_rate": wins / len(bets),
        "total_pnl": sum(pnls),
        "avg_pnl": sum(pnls) / len(bets),
        "max_drawdown": max_dd,
    }
