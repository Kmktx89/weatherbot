"""Calibration of the LIVE formula (σ=1.0, NWS + today_max) from the snapshot log.

The replay-based `lab.calibration` cannot see the live formula: the Open-Meteo
archive returns frozen ECMWF/GFS only — no NWS overlay, no METAR today_max. To
confirm the σ=1.0 graduation behaves as the backtest promised (YES +8.8pp
underconfident, NO −3.5pp overconfident) we measure the deployed formula directly
from `live_picks_log.jsonl`, the hourly snapshot written by `snapshot.py`.

Per event:
  - settlement is the winning bucket's TICKER, resolved from the log's own
    `settled_bucket` (a subtitle, mapped to a ticker via the row's buckets) or,
    if no settled row was captured, from Kalshi via `winner_of`.
  - the prediction is the pred row (buckets with non-null prob) whose lead_hours
    is nearest a target lead (default 24h, matching the backtest decision lead).
    Note: build_event_data nulls out probs once an event settles, so the LATEST
    row for a settled event is unusable — we filter to pre-settlement rows.
  - YES pick = argmax(prob); won = pick.ticker == winner.
  - NO  pick = kalshi_temp.best_no_pick (argmax ev_no over buckets passing the
    sanity cap + MIN_BEST_EV + printed-NO floor); won = pick != winner.

`calibrate_live` gives the per-event headline (one obs per event at ~target lead);
`calibrate_by_lead` bins ALL pred rows by lead to expose timing degradation
(rows are not independent across an event — directional, not inferential).
"""
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import kalshi_temp as kt  # best_no_pick — the canonical deployed NO rule

from .calibration import CalibrationReport, report_from_pairs
from .data_cache import default as default_cache
from .inputs import fetch_event_markets, winner_of


LOG_PATH = "live_picks_log.jsonl"
LEAD_BINS: list[tuple[float, float]] = [(0, 12), (12, 24), (24, 36), (36, float("inf"))]


@dataclass
class LiveCalRecord:
    event_ticker: str
    lead_hours: float | None
    yes_pred: float | None
    yes_won: int | None
    no_pred: float | None
    no_won: int | None


def read_log(path: str = LOG_PATH, *, since_days: int | None = None) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if since_days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp()
        rows = [r for r in rows
                if datetime.fromisoformat(r["ts"]).timestamp() >= cutoff]
    return rows


def _has_pred(row: dict) -> bool:
    return any(b.get("prob") is not None for b in row.get("buckets", []))


def _yes_pick(buckets: list[dict]) -> dict | None:
    cand = [b for b in buckets if b.get("prob") is not None]
    return max(cand, key=lambda b: b["prob"]) if cand else None


def _no_pick(buckets: list[dict]) -> dict | None:
    """Delegate to the deployed canonical rule so the tool always measures what
    the bot actually surfaces (kalshi_temp.best_no_pick: MIN_BEST_EV + sanity cap
    + printed-NO floor). Re-scoring pre-fix logs under this rule reproduces the
    candidate-C result in 2026-05-26-no-selection-fix.md."""
    return kt.best_no_pick(buckets)


def _winner_ticker(rows: list[dict], event_ticker: str, cache) -> str | None:
    """Winning bucket ticker. Prefer the log's settled_bucket (subtitle -> ticker
    via the event's own bucket rows); fall back to Kalshi settlement."""
    sb = next((r["settled_bucket"] for r in rows if r.get("settled_bucket")), None)
    if sb:
        for r in rows:
            for b in r.get("buckets", []):
                if b.get("subtitle") == sb and b.get("ticker"):
                    return b["ticker"]
    cache = cache or default_cache()
    markets = fetch_event_markets(event_ticker, cache)
    w = winner_of(markets)
    return w["ticker"] if w else None


def _score_row(row: dict, winner_ticker: str) -> LiveCalRecord:
    buckets = row.get("buckets", [])
    yp = _yes_pick(buckets)
    npk = _no_pick(buckets)
    return LiveCalRecord(
        event_ticker=row.get("event_ticker"),
        lead_hours=row.get("lead_hours"),
        yes_pred=(yp["prob"] if yp else None),
        yes_won=((1 if yp["ticker"] == winner_ticker else 0) if yp else None),
        no_pred=((1.0 - npk["prob"]) if npk else None),
        no_won=((1 if npk["ticker"] != winner_ticker else 0) if npk else None),
    )


def _by_event(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        out[r["event_ticker"]].append(r)
    return out


def build_records(rows: list[dict], *, target_lead: float = 24.0,
                  cache=None) -> tuple[list[LiveCalRecord], dict, list[float]]:
    """One record per event: the pred row nearest target_lead, scored vs winner.

    Returns (records, skip_counts, actual_leads_used)."""
    skips: dict[str, int] = defaultdict(int)
    recs: list[LiveCalRecord] = []
    leads: list[float] = []
    for ev, rs in _by_event(rows).items():
        wt = _winner_ticker(rs, ev, cache)
        if wt is None:
            skips["not_settled"] += 1
            continue
        preds = [r for r in rs if _has_pred(r) and r.get("lead_hours") is not None]
        if not preds:
            skips["no_pred_row"] += 1
            continue
        row = min(preds, key=lambda r: abs(r["lead_hours"] - target_lead))
        recs.append(_score_row(row, wt))
        leads.append(row["lead_hours"])
    return recs, dict(skips), leads


def calibrate_live(records: list[LiveCalRecord], side: str) -> CalibrationReport:
    pairs: list[tuple[float, int]] = []
    for r in records:
        pred = r.yes_pred if side == "yes" else r.no_pred
        won = r.yes_won if side == "yes" else r.no_won
        if pred is None or won is None:
            continue
        pairs.append((pred, won))
    return report_from_pairs(side, pairs)


def calibrate_by_lead(rows: list[dict], *, cache=None) -> dict:
    """Bin ALL pred rows by lead_hours; one (pred, won) pair per row per side.

    Not per-event independent (an event contributes a row to several bins) —
    read as a directional degradation curve, not an inferential test."""
    by_ev = _by_event(rows)
    winners = {ev: _winner_ticker(rs, ev, cache) for ev, rs in by_ev.items()}
    out: dict = {}
    for lo, hi in LEAD_BINS:
        yes_pairs: list[tuple[float, int]] = []
        no_pairs: list[tuple[float, int]] = []
        ev_seen: set[str] = set()
        for ev, rs in by_ev.items():
            wt = winners[ev]
            if wt is None:
                continue
            for r in rs:
                L = r.get("lead_hours")
                if L is None or not (lo <= L < hi) or not _has_pred(r):
                    continue
                rec = _score_row(r, wt)
                if rec.yes_pred is not None and rec.yes_won is not None:
                    yes_pairs.append((rec.yes_pred, rec.yes_won))
                if rec.no_pred is not None and rec.no_won is not None:
                    no_pairs.append((rec.no_pred, rec.no_won))
                ev_seen.add(ev)
        out[(lo, hi)] = {
            "yes": report_from_pairs("yes", yes_pairs),
            "no": report_from_pairs("no", no_pairs),
            "n_events": len(ev_seen),
        }
    return out
