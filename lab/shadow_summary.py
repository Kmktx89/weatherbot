"""Summarize shadow_picks.jsonl: join to settled outcomes and report A/B."""
import json
from collections import defaultdict
from pathlib import Path

from .inputs import winner_of, fetch_event_markets
from .data_cache import default as default_cache


def _read_shadow(path: Path, *, since_days: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if since_days is not None:
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        rows = [r for r in rows
                if datetime.fromisoformat(r["ts"]).timestamp() >= cutoff.timestamp()]
    return rows


def summarize(config_name: str, *, days: int = 14,
              shadow_path: str = "shadow_picks.jsonl") -> dict:
    """For each event that this config saw at least once, take the LATEST
    shadow row, look up the settled winner, compute pnl using the recorded
    pick_yes_ask. Aggregate."""
    rows = _read_shadow(Path(shadow_path), since_days=days)
    rows = [r for r in rows if r["config_name"] == config_name]
    by_event: dict[str, dict] = {}
    for r in rows:
        prev = by_event.get(r["event_ticker"])
        if prev is None or r["ts"] > prev["ts"]:
            by_event[r["event_ticker"]] = r
    cache = default_cache()
    bets, wins, pnls, skips = [], 0, [], defaultdict(int)
    for ev, r in by_event.items():
        markets = fetch_event_markets(ev, cache)
        winner = winner_of(markets)
        if winner is None:
            skips["not_settled"] += 1
            continue
        if r["pick_yes_ask"] is None or r["pick_yes_ask"] <= 0 or r["pick_yes_ask"] >= 1:
            skips["no_price"] += 1
            continue
        won = r["pick_ticker"] == winner["ticker"]
        if won:
            wins += 1
            pnls.append(1.0 - r["pick_yes_ask"])
        else:
            pnls.append(-r["pick_yes_ask"])
        bets.append({"event": ev, "won": won, "pnl": pnls[-1],
                     "pick": r["pick_ticker"], "entry": r["pick_yes_ask"]})
    summary = {
        "config": config_name, "events_seen": len(by_event),
        "bets": len(bets), "wins": wins,
        "win_rate": (wins / len(bets)) if bets else 0.0,
        "total_pnl": sum(pnls),
        "skips": dict(skips),
    }
    return {"summary": summary, "bets": bets}
