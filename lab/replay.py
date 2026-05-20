"""Replay a ModelConfig over a list of settled event tickers.

For each event we evaluate BOTH strategies that the live dashboard surfaces:

  - YES strategy: pick the highest model-prob bucket; entry at yes_ask.
  - NO  strategy: pick the highest EV_NO bucket after the sanity cap (skip
    when the market is highly confident YES); entry at no_ask = 1 - yes_bid.

Constants and gating mirror kalshi_temp.py::_eval_yes_strategy /
_eval_no_strategy. The shared decision_ts is computed once from cfg's
decision_lead_hours so that the candle prices for both strategies come
from the same snapshot.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal

from model import ModelConfig, compute

from .data_cache import default as default_cache
from .inputs import build_historical_inputs, fetch_bid_ask_at, winner_of


# Same constants as kalshi_temp.py for parity.
SANITY_MARKET_CONFIDENT_YES = 0.85
SANITY_MODEL_LOW_PROB = 0.40
MIN_BEST_EV = 0.05


@dataclass
class BetOutcome:
    """One side of a bet (YES or NO) for one event."""
    pick_ticker: str | None
    pick_bucket: str | None
    pick_prob: float | None      # P_yes for yes; (1 - P_yes) for no
    entry_price: float | None    # yes_ask for yes; no_ask for no
    ev_at_entry: float | None    # only meaningful for NO strategy
    won: bool | None
    pnl: float | None
    skip: str | None


_EMPTY = BetOutcome(None, None, None, None, None, None, None, "no_eval")


@dataclass
class ReplayRecord:
    event_ticker: str
    series: str
    target_date: str
    mu: float | None
    sigma: float | None
    yes: BetOutcome
    no: BetOutcome
    skip: str | None             # event-level skip (no_inputs / not_settled / no_model)


def _fetch_prices(series: str, ticker: str, decision_ts: int, cache):
    """Cached fetch of (yes_ask, yes_bid) at decision_ts. One Kalshi candle
    request per (ticker, decision_ts), reused across YES and NO strategies."""
    return fetch_bid_ask_at(series, ticker, decision_ts, cache)


def _eval_yes(ranked, prices_by_ticker, winner_ticker) -> BetOutcome:
    """Pick highest model-prob bucket; entry at yes_ask. No threshold gate
    (the user can run a threshold filter at the sweep / preset layer)."""
    if not ranked:
        return BetOutcome(None, None, None, None, None, None, None, "no_probability")
    pick, p = max(ranked, key=lambda x: x[1])
    yes_ask, _ = prices_by_ticker.get(pick["ticker"], (None, None))
    pick_bucket = pick.get("subtitle") or "?"
    if yes_ask is None or yes_ask <= 0 or yes_ask >= 1:
        return BetOutcome(pick["ticker"], pick_bucket, p, None, None, None, None, "no_price")
    won = pick["ticker"] == winner_ticker
    pnl = (1.0 - yes_ask) if won else -yes_ask
    return BetOutcome(pick["ticker"], pick_bucket, p, yes_ask, None, won, pnl, None)


def _eval_no(ranked, prices_by_ticker, winner_ticker) -> BetOutcome:
    """Pick best-EV-NO bucket; sanity-cap against highly-confident YES
    markets; entry at no_ask = 1 - yes_bid. Mirrors the dashboard's
    'Best EV NO' suggestion."""
    candidates = []
    for m, p in ranked:
        yes_ask, yes_bid = prices_by_ticker.get(m["ticker"], (None, None))
        if yes_ask is None or yes_bid is None:
            continue
        if yes_ask >= SANITY_MARKET_CONFIDENT_YES and p <= SANITY_MODEL_LOW_PROB:
            continue
        no_ask = 1.0 - yes_bid
        if no_ask <= 0 or no_ask >= 1:
            continue
        ev_no = (1 - p) - no_ask
        if ev_no < MIN_BEST_EV:
            continue
        candidates.append((m, p, no_ask, ev_no))
    if not candidates:
        return BetOutcome(None, None, None, None, None, None, None, "no_qualifying_no_bet")
    pick, p_yes, no_ask, ev_no = max(candidates, key=lambda x: x[3])
    pick_bucket = pick.get("subtitle") or "?"
    won = pick["ticker"] != winner_ticker
    pnl = (1.0 - no_ask) if won else -no_ask
    return BetOutcome(pick["ticker"], pick_bucket, 1 - p_yes, no_ask, ev_no, won, pnl, None)


def replay_one(event_ticker: str, cfg: ModelConfig) -> ReplayRecord:
    cache = default_cache()
    inputs = build_historical_inputs(event_ticker, cache=cache)
    if inputs is None:
        return ReplayRecord(event_ticker, "", "", None, None, _EMPTY, _EMPTY, "no_inputs")

    winner = winner_of(list(inputs.markets))
    if winner is None:
        return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                            None, None, _EMPTY, _EMPTY, "not_settled")

    out = compute(inputs, cfg)
    if out.mu is None or not out.probs:
        return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                            None, None, _EMPTY, _EMPTY, "no_model")

    close_dt = datetime.fromisoformat(inputs.markets[0]["close_time"].replace("Z", "+00:00"))
    decision_ts = int(close_dt.timestamp() - cfg.decision_lead_hours * 3600)

    # Shared price snapshot for both strategies — both look at the same decision moment.
    ranked = [(m, out.probs[m["ticker"]])
              for m in inputs.markets if m["ticker"] in out.probs]
    prices = {m["ticker"]: _fetch_prices(inputs.series, m["ticker"], decision_ts, cache)
              for m, _ in ranked}

    yes = _eval_yes(ranked, prices, winner["ticker"])
    no  = _eval_no(ranked,  prices, winner["ticker"])

    return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                        out.mu, out.sigma, yes, no, None)


def replay_many(event_tickers: Iterable[str], cfg: ModelConfig) -> list[ReplayRecord]:
    return [replay_one(t, cfg) for t in event_tickers]


def summarize(records: list[ReplayRecord], *,
              side: Literal["yes", "no"] = "yes") -> dict:
    """Aggregate one side (yes or no) of a list of replay records."""
    outcomes = [(getattr(r, side)) for r in records]
    bets = [o for o in outcomes if o.pnl is not None]
    if not bets:
        skips = {}
        for o in outcomes:
            if o.skip:
                skips[o.skip] = skips.get(o.skip, 0) + 1
        return {"events": len(records), "bets": 0, "wins": 0, "win_rate": 0.0,
                "total_pnl": 0.0, "avg_pnl": 0.0, "max_drawdown": 0.0,
                "skips": skips}
    wins = sum(1 for o in bets if o.won)
    pnls = [o.pnl for o in bets]
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    skips = {}
    for o in outcomes:
        if o.skip:
            skips[o.skip] = skips.get(o.skip, 0) + 1
    return {
        "events": len(records), "bets": len(bets), "wins": wins,
        "win_rate": wins / len(bets),
        "total_pnl": sum(pnls),
        "avg_pnl": sum(pnls) / len(bets),
        "max_drawdown": max_dd,
        "skips": skips,
    }


def summarize_both(records: list[ReplayRecord]) -> dict:
    return {"yes": summarize(records, side="yes"),
            "no":  summarize(records, side="no")}
