"""argparse routing for the model lab."""
import argparse
import json
import sys

from . import configs as _configs
from .compare import compare as run_compare
from .decompose import decompose as run_decompose
from .replay import replay_many, summarize


_DEFAULT_VARIANTS = ["live-minus-nws", "live-minus-trunc", "live-minus-push"]


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


def cmd_compare(args):
    cfg_a = _configs.get(args.cfg_a)
    cfg_b = _configs.get(args.cfg_b)
    events = _resolve_events(args)
    if not events:
        sys.exit("no events resolved")
    result = run_compare(events, cfg_a, cfg_b, bootstrap=args.bootstrap)
    if args.json:
        import dataclasses
        print(json.dumps(dataclasses.asdict(result), indent=2))
        return
    print(f"A: {result.cfg_a}   B: {result.cfg_b}   Events: {len(events)}")
    print(f"  A: bets {result.summary_a['bets']}  WR {result.summary_a['win_rate']*100:.1f}%  PnL ${result.summary_a['total_pnl']:+.2f}  DD ${result.summary_a['max_drawdown']:.2f}")
    print(f"  B: bets {result.summary_b['bets']}  WR {result.summary_b['win_rate']*100:.1f}%  PnL ${result.summary_b['total_pnl']:+.2f}  DD ${result.summary_b['max_drawdown']:.2f}")
    lo, hi = result.pnl_delta_ci
    print(f"  PnL delta (B - A): ${result.pnl_delta:+.2f}   95% CI [${lo:+.2f}, ${hi:+.2f}]")
    print(f"  Agreement: {result.agreement_rate*100:.1f}%   Decision flips: {len(result.decision_flips)}")


def cmd_decompose(args):
    baseline = _configs.get(args.against)
    variant_names = (args.variants.split(",") if args.variants
                     else _DEFAULT_VARIANTS)
    variants = [_configs.get(n) for n in variant_names]
    events = _resolve_events(args)
    if not events:
        sys.exit("no events resolved")
    result = run_decompose(events, baseline, variants)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Decomposing {baseline.name} vs {len(variants)} variants over {len(events)} events")
    for v in result["variants"]:
        print(f"  {v['variant']:<22}  attrib ${v['pnl_delta_attrib']:+.2f}   flips {v['decision_flips']}")


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

    cp = sub.add_parser("compare", help="A/B between two configs")
    cp.add_argument("cfg_a")
    cp.add_argument("cfg_b")
    cp.add_argument("--days", type=int, default=30)
    cp.add_argument("--series")
    cp.add_argument("--events")
    cp.add_argument("--bootstrap", type=int, default=1000)
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=cmd_compare)

    dp = sub.add_parser("decompose", help="prob/PnL attribution across variants")
    dp.add_argument("--against", default="live-today")
    dp.add_argument("--variants", help="comma-separated names; default = minus-nws,minus-trunc,minus-push")
    dp.add_argument("--days", type=int, default=30)
    dp.add_argument("--series")
    dp.add_argument("--events")
    dp.add_argument("--json", action="store_true")
    dp.set_defaults(func=cmd_decompose)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
