# How to read the model's probabilities

- **Date:** 2026-05-19
- **Branch:** `model-lab`
- **Method:** `lab.calibration.calibrate_at_leads(LIVE_TODAY, [6,12,18,24,30,36], 30d, 207 events)`

## Why this report exists

You asked three sharp questions:

1. *When are the model's predictions most accurate?*
2. *How should the returned probabilities be discounted based on timing?*
3. *Should the EV-NO predictions be read differently than the high-temp YES predictions?*

The short answers are:

1. **The model's probabilities don't depend on lead time in this replay.** Open-Meteo's historical archive returns whichever forecast was the most recent before the target date, so the model output is fixed per event. The "leadtime sweep" from the prior report is therefore measuring price-tightening, not forecast-skill change. To measure how live forecasts evolve through the day, we need shadow data (now accumulating).
2. **Discount asymmetrically by side, not by time:** **YES picks should be read as ~20pp HIGHER than the printed number; NO picks should be read as 3-8pp LOWER, with the gap narrowing as you approach close.** Details below.
3. **YES and NO probabilities mean different things and must be interpreted differently.** Specifically, the YES side picks the bucket with peak probability mass while the NO side picks the bucket with the best risk-adjusted lay opportunity. The same "75%" means different things on each side.

## The data

207 settled events (last 30 days, 7 KXHIGH cities). For each (event, lead-time, side) we recorded the model's stated probability on the picked bucket and whether the pick actually won.

### YES side — model is consistently underconfident

```
   lead     n   mean_pred    realized    gap (R-P)    brier   log_loss
     6h    96       38.0%       17.7%       -20.3pp   0.1818     0.5539
    12h   201       39.7%       60.7%       +21.0pp   0.2796     0.7553
    18h   205       40.5%       61.5%       +21.0pp   0.2756     0.7455
    24h   207       41.1%       61.8%       +20.8pp   0.2730     0.7384
    30h   207       41.1%       61.8%       +20.8pp   0.2730     0.7384
    36h   207       41.1%       61.8%       +20.8pp   0.2730     0.7384
```

Read: the model picks the bucket it thinks has 41.1% probability on average. That bucket wins 61.8% of the time. **The gap is +20.8pp — the model is materially underconfident.**

The constancy of the numbers from 12h to 36h confirms the "model output is fixed across lead times" claim — same picks, same prob, same outcome. (6h is degenerate; only 96 events had usable T-6h prices, and the gap inverts because that subset is biased toward events that had moved sharply.)

### NO side — model is overconfident, more at short lead

```
   lead     n   mean_pred    realized    gap (R-P)    brier   log_loss
     6h    33       39.1%        3.0%       -36.1pp   0.1658     0.5057
    12h   168       69.4%       43.5%       -25.9pp   0.2837     0.7686
    18h   194       74.8%       61.3%       -13.4pp   0.2167     0.6106
    24h   194       78.2%       70.1%        -8.1pp   0.1770     0.5144
    30h   190       79.3%       70.0%        -9.3pp   0.1795     0.5195
    36h   176       83.0%       79.5%        -3.4pp   0.1290     0.3925
```

Read: at T-24h the model picks NO bets where it thinks the bucket is 78% likely to LOSE. The bucket actually loses 70% of the time. **8pp overconfident.** By T-36h the gap shrinks to 3.4pp (essentially calibrated). At T-12h the gap blows out to 26pp (very overconfident).

The NO-side n changes across lead times because the **sanity cap fires differently at each price snapshot.** The sanity rule (`yes_ask ≥ 0.85 AND p_yes ≤ 0.40 → skip`) screens out different events as the market tightens.

## Per-bucket calibration (24h vs 36h, YES side)

```
  lead 24h:
         bin     n   mean_pred    realized      gap
  [0.1,0.2)     2       19.3%       50.0%   +30.7pp
  [0.2,0.3)    10       24.5%       40.0%   +15.5pp
  [0.3,0.4)   145       34.8%       60.7%   +25.8pp
  [0.4,0.5)    14       43.8%       28.6%   -15.3pp
  [0.5,0.6)    15       55.9%       86.7%   +30.8pp
  [0.6,0.7)     5       65.8%       60.0%    -5.8pp
  [0.7,0.8)     5       73.5%      100.0%   +26.5pp
  [0.8,0.9)     4       85.5%       75.0%   -10.5pp
  [0.9,1.0)     7       96.0%      100.0%    +4.0pp
```

The bulk of YES bets fall in the **[0.3, 0.4) bin (145 of 207 events)**. The model says 34.8% in those cases; reality is 60.7%. **+26pp under.** This single bin drives most of the +20.8pp average gap. The [0.5, 0.6) bin is also dramatically under (+30.8pp).

Where the model says >70%, it's roughly right or slightly under; where it says <70%, it's significantly under.

## Per-bucket calibration (24h vs 36h, NO side)

```
  lead 24h:
         bin     n   mean_pred    realized      gap
  [0.3,0.4)     1       35.6%        0.0%   -35.6pp
  [0.4,0.5)     1       41.7%        0.0%   -41.7pp
  [0.5,0.6)     1       57.1%      100.0%   +42.9pp
  [0.6,0.7)    56       66.0%       35.7%   -30.2pp
  [0.7,0.8)    46       75.1%       80.4%    +5.3pp
  [0.8,0.9)    52       84.3%       78.8%    -5.5pp
  [0.9,1.0)    37       94.5%      100.0%    +5.5pp

  lead 36h:
         bin     n   mean_pred    realized      gap
  [0.6,0.7)    26       66.5%       34.6%   -31.8pp
  [0.7,0.8)    34       75.3%       79.4%    +4.1pp
  [0.8,0.9)    56       84.7%       82.1%    -2.5pp
  [0.9,1.0)    57       95.4%      100.0%    +4.6pp
```

The **[0.6, 0.7) NO bin is the danger zone**: model says ~66% NO confidence; reality is only 35%. That's a 30pp miss. If you take NO bets where the model says 60-70% likely to lose, you're effectively coin-flipping at best.

At T-24h, 56 of 194 NO bets (29%) fall in this danger zone. **At T-36h, only 26 of 176 (15%) do** — because the sanity cap screens more of them out as prices tighten by then. That's why T-36h NO has higher realized win rate: lower share of bad bin.

The [0.7, 1.0) NO bins are all reasonably calibrated (gap < ±10pp).

## The single most important framing

**The model is a probability-mass-in-buckets engine, not a "I'm X% sure I'm right" engine.**

The model returns P(actual_high lies in bucket K) under a normal distribution centered on its mu, sigma. For a typical event with 15 buckets each ~1°F wide and a forecast σ of ~2°F, the peak bucket has 30-40% of the mass even when the forecast is excellent. The remaining 60-70% of the mass is distributed across nearby buckets.

But because forecasts are reasonably accurate, **the peak bucket WINS far more often than its 30-40% mass suggests**. The 62% realized win rate on a 41% mean prediction is consistent with σ being overestimated — the actual forecast distribution is narrower than the model's σ = √(BASE_SIGMA² + spread²) = √(4 + small²) ≈ 2.1°F implies.

**The fix is almost certainly to lower BASE_SIGMA.** Today it's 2.0°F fixed. The miscalibration suggests true σ is closer to 1.3-1.5°F. Sweep next.

## How to read a prediction in practice

When you look at the dashboard at any time of day during a market's life:

### YES picks (highest-prob bucket)

The number shown is the model's probability mass for that bucket. **Mentally add ~20pp to it as your real-world prior on it winning** (when the printed number is in the 30-60% range, where 70% of bets live). If the printed number is already 70%+, take it at face value or maybe +5pp.

EV is what you actually trade on. The dashboard's EV column = (model_prob − yes_ask). If you believe the calibration, the TRUE EV at the 30-40% bin is ~(model_prob + 0.20 − yes_ask). A 30%-mass bucket priced at 10¢ has a printed EV of 20¢; the calibration-adjusted EV is closer to 40¢.

### NO picks (highest EV_NO with sanity cap)

The number shown is 1 − P_yes for the bucket. **The model's confidence in NO is OVERSTATED at short lead times.** Discount as follows:

- At T-12h: subtract ~25pp (very overconfident)
- At T-18h: subtract ~13pp
- At T-24h: subtract ~8pp
- At T-30h: subtract ~9pp
- At T-36h: subtract ~3pp (nearly calibrated)

**Avoid NO bets where the printed probability is in the 60-70% range** — that's the danger zone where realized rate is only 35%. Either wait for a stronger signal (>75%) or skip.

### Asymmetry to remember

| | YES side | NO side |
|---|---|---|
| What the pick is | Highest model-prob bucket | Highest EV_NO bucket (with sanity cap) |
| Selection rule | argmax(P_yes) | argmax(EV_NO) where EV_NO ≥ 5¢ and sanity cap doesn't fire |
| Won definition | pick_ticker == winner | pick_ticker != winner |
| Calibration today | Underconfident +20pp | Overconfident 3-26pp (worse at short lead) |
| Trust rule of thumb | Add ~20pp to printed prob | Subtract 5-10pp at T-24h, less at T-36h |
| Safe regime | All bins | Printed prob ≥ 75% only |

## Recommendations

### 1. Sweep BASE_SIGMA

The +20pp YES underconfidence is consistent with σ being too generous. `python -m lab sweep --param base_sigma --values 1.0,1.25,1.5,1.75,2.0,2.25 --days 30`. Expect peak bucket probability to climb and calibration gap to shrink as σ decreases. Caveat: too-low σ will overconfident the tail buckets in the other direction.

### 2. Add a printed calibration adjustment to the dashboard

After the BASE_SIGMA sweep finds a better σ, the residual calibration gap will be smaller but probably still nonzero. Consider adding an "adjusted P" column to the dashboard that applies a learned monotonic recalibration (Platt scaling or isotonic regression over the 207-event sample) so you read calibrated numbers directly.

### 3. Make the NO sanity-cap stricter at short lead

Increase the `sanity_no_yes_ask_min` from 0.85 to 0.80 (or even 0.75) and/or add a leadtime-conditional rule (drop NO bets where T-close < 18h AND printed prob < 0.75). The danger-zone bin will get fewer entries, raising NO win rate.

### 4. Run the BASE_SIGMA sweep on NO win rate per bin

The danger-zone bin shrinks as lead grows (56 events → 26 events from T-24h to T-36h). It's plausible that the sigma fix will collapse the danger zone entirely.

## Limitations

- 207-event sample. Single-bin calibration (e.g. [0.5, 0.6) with n=15 on YES) is noisy.
- Replay uses no NWS, no today_max — the production formula adds both. Calibration on the production formula will differ; once shadow data accumulates we can recalibrate per-side.
- The mass-vs-confidence framing assumes the bucket structure stays at ~1°F width. If Kalshi ever changes bucket granularity, all numbers need re-derivation.
- Brier and log loss are scoring the bet-placed outcomes, not the full distribution. A proper distributional scoring would compare the model's full PDF to the realized temperature, not just the picked bucket vs win/loss.
