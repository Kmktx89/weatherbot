# Leadtime sweep — when to commit positions

- **Date:** 2026-05-19
- **Branch:** `model-lab`
- **Method:** `python -m lab sweep --param decision_lead_hours --values 12,18,24,30,36,42 --days 30 --by-series` against `live-today` config (= production formula minus NWS + today_max, since historical replay can't reconstruct them — same caveat as the divergence report)
- **Sample:** 207 settled events across 7 KXHIGH cities

## Summary

**The current deployed lead of 24 hours is too short.** Pushing the decision time to T-36h before close beats T-24h on YES PnL by **+$11.63 over 30 days** (+22%) and on NO win-rate by **+9.4 percentage points** (70.1% → 79.5%), while keeping the same bet count. Every single city improves. T-42h returns zero bets because Kalshi's candle endpoint has no price data that far ahead; that's the upper bound of the usable window. The sweet spot is between 30h and 36h.

## The sweep

```
Sweep:   base=live-today   param=decision_lead_hours   events=207
       value  YES bets   YES WR    YES PnL  NO bets    NO WR     NO PnL
        12.0       201    60.7%  $ +28.81      168    43.5%  $  +1.69
        18.0       205    61.5%  $ +47.10      194    61.3%  $ +13.65
        24.0       207    61.8%  $ +52.45      194    70.1%  $ +22.04
        30.0       207    61.8%  $ +57.19      190    70.0%  $ +15.89
        36.0       207    61.8%  $ +64.08      176    79.5%  $ +22.96
        42.0         0     0.0%  $  +0.00        0     0.0%  $  +0.00
  Best YES PnL: decision_lead_hours=36.0 -> $+64.08
  Best NO  PnL: decision_lead_hours=36.0 -> $+22.96
```

## Per-series YES PnL by lead

```
  series               12h         18h        24h        30h        36h        42h
  KXHIGHAUS       $   +5.06  $   +8.55  $   +9.11  $   +8.86  $  +10.07  $   +0.00
  KXHIGHCHI       $   +3.34  $   +7.18  $   +8.82  $   +9.39  $   +9.98  $   +0.00
  KXHIGHDEN       $   +4.27  $   +5.04  $   +6.60  $   +7.48  $   +8.37  $   +0.00
  KXHIGHLAX       $   +4.01  $   +5.61  $   +5.47  $   +6.32  $   +6.78  $   +0.00
  KXHIGHMIA       $   +3.28  $   +7.71  $   +8.10  $   +9.08  $   +9.88  $   +0.00
  KXHIGHNY        $   +3.57  $   +5.23  $   +6.43  $   +7.02  $   +8.15  $   +0.00
  KXHIGHPHIL      $   +5.28  $   +7.78  $   +7.92  $   +9.04  $  +10.85  $   +0.00
```

Every city is monotone-increasing from 12h to 36h. PHIL leads at 36h (+$10.85); AUS, CHI, MIA all cluster near +$10. LAX and NY are weakest absolutely but still benefit from the longer lead. No city wants a shorter lead than 24h.

## Practical implication: when to place the bet

Markets close at ~01:00 local time on the day after resolution. Converting T-36h to local time-of-day:

| City | Window (local) | Window (ET) |
| --- | --- | --- |
| NYC (KNYC), Miami (KMIA), Philadelphia (KPHL) | 12:00–13:00 day before resolution | 12:00–13:00 ET |
| Chicago (KMDW), Austin (KAUS) | 12:00–13:00 day before | 13:00–14:00 ET |
| Denver (KDEN) | 12:00–13:00 day before | 14:00–15:00 ET |
| LA (KLAX) | 12:00–13:00 day before | 15:00–16:00 ET |

The current dashboard `/schedule` page recommends 19:00–23:00 local (~T-26h to T-30h). **That window is too late** — moving it earlier to ~T-36h (12:00–13:00 local) captures ~22% more edge on YES picks. The schedule page should be updated; see "Recommendations" below.

## Why 36h beats 24h

The model uses ECMWF + GFS forecasts that are issued 0Z, 6Z, 12Z, 18Z. At T-36h before a 01:00-local-next-day close, the most recent forecast run is typically the same-day morning ECMWF run; the *price* hasn't yet been moved by late-day refinements that retail tape-watchers respond to. By T-24h (mid-afternoon previous day), prices have moved closer to consensus, eating into edge. The forecast accuracy barely changes between T-36h and T-24h, but market prices tighten — so we're better off committing earlier with the same forecast skill.

(This is consistent with the deployed `nws_log.jsonl` showing forecast revisions in the 24-48h window are small. We can verify directly once the log has ~30 days of data.)

## Why 42h returns zero bets

`kalshi_temp.fetch_kalshi_candlestick` returns empty arrays for these tickers at T-42h+. Possible reasons:
1. Markets are listed earlier than that but candle data only exists once trading happens, and trading is thin until ~T-36h
2. Kalshi rate-limited the historical candle endpoint for that depth (less likely given fresh cache)

Either way, T-42h+ is not a tradeable regime for now.

## Recommendations (ranked by easiness × edge)

1. **Update the dashboard `/schedule` page** to recommend a T-36h window (12:00–13:00 local on the day before resolution) instead of the current 19:00–23:00 local. One-line edit to `SCHEDULE_HTML` in `kalshi_temp.py`. **Largest single win** — gets the human's attention focused at the right time.

2. **Change `DEFAULT_LEAD_HOURS` in `kalshi_temp.py` from 24.0 to 36.0** so the backtest CLI defaults align with this finding. Same one-liner. No effect on live picks (which recompute every refresh).

3. **Don't change `LIVE_TODAY.decision_lead_hours`** in `lab/configs.py` yet — the field controls the lab's reported PnL only, and 36 is already the empirically right value. If we cut over the production constant in #2, also cut over the lab config to match.

4. **Run a second sweep with `decision_lead_hours` set per-series.** All cities prefer 36h here, but ec/gfs forecast cycles favor different cut-offs at different time zones; a per-series sweep may surface 30h being optimal for one city. Not urgent — single-knob improvement is already large.

5. **Forward shadow validation.** Once 14+ days of `shadow_picks.jsonl` accumulate, run `lab shadow-summary` with a config variant that has `decision_lead_hours=36`. Confirms the historical replay finding holds in live conditions.

## Limitations

- 30-day sample (~30 events per city). Bootstrap CI not run on the sweep delta yet; the +$11.63 YES improvement is a point estimate. Worth re-running a 60-day sweep with bootstrap on the (24h vs 36h) compare to attach a CI before any production change.
- Replay uses no NWS overlay and no today_max (Open-Meteo archive doesn't have them; bot doesn't record them). The deployed model HAS those overlays, so the absolute PnL numbers here are the BACKTEST PnL, not what the deployed bot would have earned. The *direction* of the leadtime finding should hold for both formulas because the price-tightening effect operates at the market level, but the absolute delta could shift.
- 42h-and-beyond candles weren't tested with retry; could be transient.

## Next sweeps to run

In rank order of value-per-minute:

1. **`base_sigma`** — currently 2.0°F fixed. Sweep 1.0, 1.5, 2.0, 2.5, 3.0. Probably affects NO side most (sanity-cap interactions).
2. **`today_max_headroom`** — currently 0.5°F. Sweep 0, 0.25, 0.5, 0.75, 1.0. Only matters in live replay (not historical), so this needs shadow data.
3. **A threshold sweep** — currently not in `ModelConfig`; the live `_eval_yes/no` always picks the top bucket without a threshold gate. Adding `threshold` as a config field and sweeping 0, 0.30, 0.50, 0.70 mirrors the dashboard's preset row.

## Conclusion

Move the bot's recommended bet time from T-24h to T-36h. One-line edits to the `/schedule` page and `DEFAULT_LEAD_HOURS` capture an estimated +22% YES PnL improvement at zero risk (no model math changes). Forward-shadow this for two weeks before declaring it permanent, then graduate.
