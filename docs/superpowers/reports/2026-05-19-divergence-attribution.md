# Weatherbot live-vs-backtest divergence attribution

- **Date:** 2026-05-19
- **Branch:** `model-lab`
- **Horizon:** last 60 days, all 7 KXHIGH series (420 events)
- **Code version under test:** `live-today` (mirrors current production formula in `kalshi_temp.py`)

## Summary

**The five known live/backtest mismatches all measure as $0 contribution on historical replay** — because Open-Meteo's historical-forecast archive doesn't include NWS forecasts and the bot doesn't record observed `today_max` per past event, so MINUS_NWS / MINUS_TRUNC / MINUS_PUSH all collapse to the same code path as LIVE_TODAY when those inputs are None. The deployed BIAS table is essentially correct for the backtest formula (largest delta is NYC at +0.26°F). **The live-vs-backtest gap you're feeling is concentrated in the components this report cannot observe**: NWS overlay impact, today_max truncation/push, and decision-time drift. Forward shadow data is the only path to quantify them.

## Method

- Replayed every settled event in the last 60 days under four configs: `live-today`, `live-minus-nws`, `live-minus-trunc`, `live-minus-push`.
- Pairwise compared each minus-X variant against `live-today` with 2000-sample bootstrap CIs on PnL delta.
- Re-fitted the per-city BIAS table against `live-today` and compared to the deployed table at `kalshi_temp.py:55-63`.

### Caveats stated up front

- **The replay environment is effectively a backtest environment.** `lab.inputs.build_historical_inputs()` sets `forecasts["nws"] = None` and `today_max = None` because Open-Meteo's historical-forecast API doesn't archive NWS, and the bot never persisted `today_max` per event. As a result, `live-today` AS REPLAYED is mathematically identical to `backtest-today` for the great majority of events. The PnL of `live-today` reported below is therefore the **backtest PnL**, not what the live bot actually achieved.
- 420 events ≈ 60 per city. Bootstrap CIs are reasonable for the per-pair compare, but the per-city slices are thin.
- All P/L is per-$1-unit notional. Live position sizing is separate.
- "Decision-time mismatch" (lead-hour drift between live picks and the T-24h backtest lock) is not separately quantified here.

## Backtest baseline

`live-today` over 420 events:

| metric | value |
| --- | --- |
| bets placed | 418 |
| wins | 246 |
| win rate | 58.9% |
| total PnL | +$97.57 |
| avg PnL/bet | +$0.233 |
| max drawdown | $2.02 |

That's a strong edge by the numbers. If the live bot is significantly underperforming this, the entire gap must live in the components below.

## Per-variant comparison

### live-today vs live-minus-nws

```
A: live-today   B: live-minus-nws   Events: 420
  A: bets 418  WR 58.9%  PnL $+97.57  DD $2.02
  B: bets 418  WR 58.9%  PnL $+97.57  DD $2.02
  PnL delta (B - A): $+0.00   95% CI [$+0.00, $+0.00]
  Agreement: 100.0%   Decision flips: 0
```

### live-today vs live-minus-trunc

```
A: live-today   B: live-minus-trunc   Events: 420
  A: bets 418  WR 58.9%  PnL $+97.57  DD $2.02
  B: bets 418  WR 58.9%  PnL $+97.57  DD $2.02
  PnL delta (B - A): $+0.00   95% CI [$+0.00, $+0.00]
  Agreement: 100.0%   Decision flips: 0
```

### live-today vs live-minus-push

```
A: live-today   B: live-minus-push   Events: 420
  A: bets 418  WR 58.9%  PnL $+97.57  DD $2.02
  B: bets 418  WR 58.9%  PnL $+97.57  DD $2.02
  PnL delta (B - A): $+0.00   95% CI [$+0.00, $+0.00]
  Agreement: 100.0%   Decision flips: 0
```

All three: $0 delta, 100% agreement, 0 decision flips. Not bootstrap noise — actual zeros because the variants are mathematically identical to LIVE_TODAY on inputs with `nws=None` and `today_max=None`.

## Decomposition table

```
Decomposing live-today vs 3 variants over 420 events
  live-minus-nws          attrib $+0.00   flips 0
  live-minus-trunc        attrib $+0.00   flips 0
  live-minus-push         attrib $+0.00   flips 0
```

Read: positive `attrib $` means `live-today` did better than the variant (so removing the component HURT). $0 across the board means the components don't differentiate at all in this replay environment — which is the same finding as the compare table above.

## BIAS refit: deployed vs live-formula-calibrated

The refit uses `live-today` with its bias table zeroed, then averages `(mu - actual_high_midpoint)` per series. Result is the optimal bias to subtract from the model's `mu` to match settled outcomes.

| Series | Deployed BIAS | Refit BIAS (n=60) | Δ = refit − deployed |
| --- | ---: | ---: | ---: |
| KXHIGHNY | -0.44 | -0.18 | **+0.26** |
| KXHIGHCHI | -1.04 | -0.95 | +0.09 |
| KXHIGHMIA | -1.52 | -1.56 | -0.04 |
| KXHIGHLAX | -0.55 | -0.59 | -0.04 |
| KXHIGHDEN | -0.81 | -0.82 | -0.01 |
| KXHIGHAUS | -1.81 | -1.91 | -0.10 |
| KXHIGHPHIL | -1.22 | -1.35 | -0.13 |

All seven cities are within ±0.3°F of the deployed value. None cross the 0.5°F threshold for "materially miscalibrated." The deployed BIAS table is in good shape **for the backtest formula** (which is what this refit measures, since replay has nws=None).

What this DOESN'T tell you: whether the deployed table is also correct for the LIVE formula (which adds a 30% NWS overlay before applying BIAS). That refit can't be done from replay data; it requires the `nws_log.jsonl` the running bot is now accumulating, joined to settled outcomes. Plan a follow-up refit in ~30 days once the log has enough rows.

## Findings

1. **The lab can't see the gap.** Backtest = live-today-as-replayed = +$97.57 / 58.9% WR over 60 days. If your real live performance is below this, the delta is entirely in the components the historical replay cannot reconstruct: NWS overlay, today_max truncation/push, and any decision-time drift away from T-24h.

2. **Deployed BIAS is correct for the backtest formula.** Refit deltas are all <0.3°F, smallest at DEN/MIA/LAX (<0.1°F). NYC is the largest delta (+0.26°F overcorrection) — meaning the current BIAS pushes NYC mu about a quarter-degree higher than optimal against settled no-NWS outcomes. Not big enough to warrant a refit ship by itself.

3. **The interesting refit is the one we can't do yet.** A refit of BIAS for the LIVE formula (with the 30% NWS overlay) requires forward NWS-log data joined to settled outcomes. Today's shadow-mode wiring (Task 23) starts collecting it. Recommend re-running this report at +30 days with the live-formula refit included.

4. **All four configs picked identically on 100% of events.** This means: even when today_max would normally be available (mid-afternoon snapshots), it isn't in the historical inputs, so `today_max_mode="both"` vs `"truncate"` vs `"off"` makes no difference for replay. The push-vs-truncate-vs-off question can only be answered with live snapshots — i.e. shadow data — collected at decision time.

5. **No variant emerges as a graduation candidate from this report.** Zero PnL delta means zero evidence that any of the named alternatives would have done better than LIVE_TODAY on the 60-day replay window. There is also zero evidence they would have done worse. The variants are unmeasured, not invalidated.

## Recommendation

**Hold for 2 weeks of shadow data, then rerun this report with a `lab shadow-summary` join.** Justification:

- Historical replay has shown its ceiling: it cannot observe the gap, so no production decision can be made from it alone.
- Shadow mode (wired up today in Task 23) starts logging the live decisions of LIVE_MINUS_NWS, LIVE_MINUS_PUSH, LIVE_MINUS_TRUNC on every dashboard refresh. After ~14 days × ~24 refreshes × 7 cities, the shadow log will have meaningful sample size on real-world picks where today_max IS observed and NWS IS overlaid.
- Run `python -m lab shadow-summary --config live-minus-push --days 14` (and similar for the other two) in two weeks. That command joins shadow picks to settled outcomes and produces the same A/B summary on REAL live data.

If a shadow config shows stat-sig PnL improvement after the shadow window, graduate by changing the single `LIVE_TODAY` reference inside `kalshi_temp.py::build_event_data` to the winner. One-line change, baseline-test gated.

**Do NOT graduate any config based on this report alone.** The replay environment doesn't differentiate them.

## What is NOT in this report

- The NWS-overlay component is unobservable from historical replay (Open-Meteo doesn't archive NWS). Shadow mode is the only forward path.
- "Decision time mismatch" (lead-hour drift between live picks and T-24h backtest) is not separately quantified.
- Position sizing, slippage, fees.
- A live-formula BIAS refit (requires `nws_log.jsonl` + settled outcomes; defer 30 days).
- Rate-limit race condition in `kalshi_get` (called out in the spec as a secondary bug; not addressed by this lab).
- Inconsistency between `_sanity_keep_no` applied in the dashboard API but bypassed in `cmd_predict` (separate cleanup pass).
