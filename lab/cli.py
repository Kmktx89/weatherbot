"""argparse routing for the model lab."""
import argparse
import json
import sys

from . import configs as _configs
from .compare import compare as run_compare
from .data_cache import default as default_cache
from .decompose import decompose as run_decompose
from .refit_bias import refit as run_refit
from .replay import replay_many, summarize, summarize_both
from .shadow_summary import summarize as run_shadow_summary
from .sweep import sweep as run_sweep, best_by_pnl


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


def _fmt_side(label: str, s: dict) -> str:
    if not s["bets"]:
        return f"  {label}: no bets"
    return (f"  {label}: bets {s['bets']:>3}  WR {s['win_rate']*100:>5.1f}%  "
            f"PnL ${s['total_pnl']:+7.2f}  avg ${s['avg_pnl']:+.3f}  "
            f"DD ${s['max_drawdown']:.2f}")


def cmd_replay(args):
    cfg = _configs.get(args.config)
    events = _resolve_events(args)
    if not events:
        sys.exit("no events resolved — pass --events, --series, or use the default all-series with --days")
    records = replay_many(events, cfg)
    if args.json:
        import dataclasses
        print(json.dumps({
            "config": cfg.name, "n_events": len(events),
            **summarize_both(records),
            "records": [dataclasses.asdict(r) for r in records],
        }, indent=2))
        return
    both = summarize_both(records)
    print(f"Config:  {cfg.name}   Events:  {len(events)}")
    print(_fmt_side("YES (high temp)", both["yes"]))
    print(_fmt_side("NO  (best EV) ", both["no"]))


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
    for side_name, side in (("YES", result.yes), ("NO ", result.no)):
        print(f"--- {side_name} side ---")
        print(_fmt_side("A", side.summary_a))
        print(_fmt_side("B", side.summary_b))
        lo, hi = side.pnl_delta_ci
        print(f"  PnL delta (B - A): ${side.pnl_delta:+.2f}   "
              f"95% CI [${lo:+.2f}, ${hi:+.2f}]")
        print(f"  Agreement: {side.agreement_rate*100:.1f}%   "
              f"Decision flips: {len(side.decision_flips)}")


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
    print(f"Decomposing {baseline.name} vs {len(variants)} variants "
          f"over {len(events)} events")
    print(f"  {'variant':<22}  {'YES attrib':>11}  {'flips':>5}  "
          f"{'NO attrib':>10}  {'flips':>5}")
    for v in result["variants"]:
        print(f"  {v['variant']:<22}  "
              f"${v['yes']['pnl_delta_attrib']:+8.2f}   {v['yes']['decision_flips']:>5}  "
              f"${v['no']['pnl_delta_attrib']:+8.2f}   {v['no']['decision_flips']:>5}")


def cmd_refit_bias(args):
    cfg = _configs.get(args.config)
    events = _resolve_events(args)
    table = run_refit(events, cfg)
    if args.json:
        print(json.dumps(table, indent=2))
        return
    print(f"# refit-bias output for config={cfg.name}, events={len(events)}")
    print("BIAS = {")
    for s, row in sorted(table.items()):
        print(f"    {s!r:<14}: {row['bias']:+6.2f},  # n={row['n']}, sd={row['sd']:.2f}")
    print("}")


def cmd_cache(args):
    cache = default_cache()
    if args.action == "stats":
        s = cache.stats()
        if args.json:
            print(json.dumps(s, indent=2))
        else:
            print(f"Total rows: {s['total']}   Oldest: {s['oldest']}")
            for src, n in sorted(s["by_source"].items()):
                print(f"  {src:<28} {n}")
    elif args.action == "purge":
        if not args.before:
            sys.exit("purge requires --before YYYY-MM-DD")
        n = cache.purge_before(args.before)
        print(f"Purged {n} rows with target_date < {args.before}")
    elif args.action == "warm":
        sys.exit("warm is not yet implemented (follow-up)")


def cmd_shadow_summary(args):
    result = run_shadow_summary(args.config, days=args.days)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    s = result["summary"]
    print(f"Config: {s['config']}   Days: {args.days}")
    print(f"  Events seen: {s['events_seen']}   Bets: {s['bets']}   "
          f"WR: {s['win_rate']*100:.1f}%   PnL: ${s['total_pnl']:+.2f}")
    if s["skips"]:
        print(f"  Skips: " + ", ".join(f"{n}× {k}" for k, n in s["skips"].items()))


def cmd_sweep(args):
    base = _configs.get(args.config)
    events = _resolve_events(args)
    if not events:
        sys.exit("no events resolved")
    values = [v.strip() for v in args.values.split(",") if v.strip()]
    report = run_sweep(events, base, args.param, values, by_series=args.by_series)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return
    print(f"Sweep:   base={base.name}   param={args.param}   "
          f"events={report['n_events']}")
    print(f"  {'value':>10}  {'YES bets':>8}  {'YES WR':>7}  {'YES PnL':>9}  "
          f"{'NO bets':>7}  {'NO WR':>7}  {'NO PnL':>9}")
    for v_str, entry in report["values"].items():
        y = entry["yes"]; n = entry["no"]
        print(f"  {v_str:>10}  "
              f"{y['bets']:>8}  {y['win_rate']*100:>6.1f}%  ${y['total_pnl']:+7.2f}  "
              f"{n['bets']:>7}  {n['win_rate']*100:>6.1f}%  ${n['total_pnl']:+7.2f}")
    best_y = best_by_pnl(report, "yes")
    best_n = best_by_pnl(report, "no")
    if best_y:
        print(f"  Best YES PnL: {args.param}={best_y[0]} → ${best_y[1]['yes']['total_pnl']:+.2f}")
    if best_n:
        print(f"  Best NO  PnL: {args.param}={best_n[0]} → ${best_n[1]['no']['total_pnl']:+.2f}")
    if args.by_series:
        print()
        print(f"  Per-series YES PnL by {args.param}:")
        series = sorted({s for entry in report["values"].values()
                          for s in entry["yes_by_series"].keys()})
        header = f"  {'series':<14}" + "".join(f" {v:>10}" for v in report["values"])
        print(header)
        for s in series:
            row = f"  {s:<14}"
            for v_str, entry in report["values"].items():
                pnl = entry["yes_by_series"].get(s, {}).get("total_pnl", 0.0)
                row += f"  ${pnl:>+8.2f}"
            print(row)


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

    cp = sub.add_parser("compare", help="A/B between two configs (YES + NO)")
    cp.add_argument("cfg_a")
    cp.add_argument("cfg_b")
    cp.add_argument("--days", type=int, default=30)
    cp.add_argument("--series")
    cp.add_argument("--events")
    cp.add_argument("--bootstrap", type=int, default=1000)
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=cmd_compare)

    dp = sub.add_parser("decompose", help="PnL attribution across variants (YES + NO)")
    dp.add_argument("--against", default="live-today")
    dp.add_argument("--variants", help="comma-separated names; default = minus-nws,minus-trunc,minus-push")
    dp.add_argument("--days", type=int, default=30)
    dp.add_argument("--series")
    dp.add_argument("--events")
    dp.add_argument("--json", action="store_true")
    dp.set_defaults(func=cmd_decompose)

    rb = sub.add_parser("refit-bias", help="refit BIAS table for a config")
    rb.add_argument("--config", required=True)
    rb.add_argument("--days", type=int, default=60)
    rb.add_argument("--series")
    rb.add_argument("--events")
    rb.add_argument("--json", action="store_true")
    rb.set_defaults(func=cmd_refit_bias)

    cc = sub.add_parser("cache", help="manage the lab data cache")
    cc.add_argument("action", choices=["stats", "purge", "warm"])
    cc.add_argument("--before", help="for purge: target_date cutoff (YYYY-MM-DD)")
    cc.add_argument("--json", action="store_true")
    cc.set_defaults(func=cmd_cache)

    ss = sub.add_parser("shadow-summary", help="A/B report from shadow_picks.jsonl")
    ss.add_argument("--config", required=True)
    ss.add_argument("--days", type=int, default=14)
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_shadow_summary)

    sw = sub.add_parser("sweep", help="sweep a ModelConfig parameter (YES + NO)")
    sw.add_argument("--config", default="live-today",
                    help="base config to vary (default live-today)")
    sw.add_argument("--param", required=True,
                    help="ModelConfig field to sweep (e.g. decision_lead_hours, "
                         "base_sigma, today_max_headroom, today_max_push, nws_blend)")
    sw.add_argument("--values", required=True,
                    help="comma-separated values, e.g. '12,18,24,36,48'")
    sw.add_argument("--days", type=int, default=60)
    sw.add_argument("--series")
    sw.add_argument("--events")
    sw.add_argument("--by-series", action="store_true",
                    help="emit per-series PnL grid")
    sw.add_argument("--json", action="store_true")
    sw.set_defaults(func=cmd_sweep)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
