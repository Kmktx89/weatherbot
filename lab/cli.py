"""argparse routing for the model lab."""
import argparse
import json
import sys

from . import configs as _configs
from .replay import replay_many, summarize


def _resolve_events(args) -> list[str]:
    import kalshi_temp as kt
    if args.events:
        return [t.strip() for t in args.events.split(",") if t.strip()]
    if args.series:
        return [e["event_ticker"]
                for e in kt.list_events_for_series(args.series, args.days)]
    # all series
    tickers: list[str] = []
    for s in kt.CITIES:
        tickers.extend(e["event_ticker"]
                        for e in kt.list_events_for_series(s, args.days))
    return tickers


def cmd_replay(args):
    cfg = _configs.get(args.config)
    events = _resolve_events(args)
    if not events:
        sys.exit("no events resolved — pass --events, --series, or use the default all-series with --days")
    records = replay_many(events, cfg)
    if args.json:
        print(json.dumps({
            "config": cfg.name, "n_events": len(events),
            "summary": summarize(records),
            "records": [r.__dict__ for r in records],
        }, indent=2))
        return
    s = summarize(records)
    print(f"Config:  {cfg.name}")
    print(f"Events:  {len(events)}   Bets: {s['bets']}   Wins: {s['wins']}")
    if s["bets"]:
        print(f"WinRate: {s['win_rate']*100:.1f}%   Total PnL: ${s['total_pnl']:+.2f}   "
              f"Avg PnL: ${s['avg_pnl']:+.3f}   Max DD: ${s['max_drawdown']:.2f}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lab", description="weatherbot model lab")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("replay", help="replay a config over events")
    pr.add_argument("--config", required=True, help="config name (e.g. live-today)")
    pr.add_argument("--days", type=int, default=30)
    pr.add_argument("--series", help="restrict to one series")
    pr.add_argument("--events", help="comma-separated event tickers (overrides --series)")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_replay)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
