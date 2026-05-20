# `BASE_SIGMA` sweep + practical reading guide for the dashboard

- **Date:** 2026-05-19
- **Branch:** `model-lab`
- **Method:** `lab.replay` + `lab.calibration` over 207 settled events (30d, 7 cities) at six `base_sigma` values
- **Decision lead time:** 24h (default; same across all sigma values so this is purely a sigma comparison)

## TL;DR

**Drop `BASE_SIGMA` from 2.0 to 1.0.** It's the single biggest model improvement in the lab so far:

- Total PnL goes from $74.49 → $89.33 over 30 days (**+20%**) at per-$1-unit notional
- NO win rate goes from 70.1% → 84.2% (**+14.1 pp**)
- YES underconfidence gap closes from +20.8 pp to +8.8 pp
- NO overconfidence gap closes from −8.1 pp to −3.5 pp
- Zero downside in this sample — every metric improves
- One-line change in `lab/configs.py:LIVE_TODAY` after forward-shadow validation

The rest of this report:
1. Why σ=1.0 — the data and the intuition
2. Calibration tables (so you can see the gap close)
3. **Practical reading guide for the live dashboard at the new sigma**, with risk-sizing rules

## 1. Why σ=1.0

### The sweep

```
   sigma  YES bets   YES WR    YES PnL  NO bets    NO WR     NO PnL
    1.00       207    63.8%  $ +55.02      196    84.2%  $ +34.31
    1.25       207    64.7%  $ +57.10      193    80.3%  $ +30.20
    1.50       207    64.3%  $ +56.26      194    77.3%  $ +29.13
    1.75       207    62.8%  $ +53.16      197    72.6%  $ +23.48
    2.00       207    61.8%  $ +52.45      194    70.1%  $ +22.04   <- deployed
    2.25       207    57.0%  $ +45.72      197    67.0%  $ +17.75
```

| sigma | Total PnL (YES + NO) | vs deployed |
|---|---|---|
| 1.00 | **$89.33** | **+$14.84 (+20%)** |
| 1.25 | $87.30 | +$12.81 (+17%) |
| 1.50 | $85.39 | +$10.90 (+15%) |
| 1.75 | $76.64 | +$2.15 |
| 2.00 | $74.49 | — (deployed) |
| 2.25 | $63.47 | −$11.02 |

### The intuition

`sigma` is the model's stated forecast uncertainty in °F. With BASE_SIGMA=2.0, the model says: "the actual high will land within ±2°F of my point estimate about 68% of the time." With BASE_SIGMA=1.0: within ±1°F.

The 60-day fit of weighted_mean(ECMWF, GFS) residuals across cities (from `kalshi_temp.py:55-63` comments) shows actual forecast standard deviations of **1.23-2.06°F**, averaging ≈1.55°F per city. The deployed 2.0°F overstates forecast uncertainty — particularly for MIA (sd=1.23), AUS (sd=1.40), PHIL (sd=1.38), NY (sd=1.44).

**The reason wider σ hurts PnL:** when the model is uncertain, probability mass spreads across more buckets. Marginal "high-confidence NO" buckets (the deep tails) get inflated mass that doesn't reflect reality. The NO strategy then picks buckets where it thinks `P_no = 85%` but reality is closer to `70%`, and you lose more often than the printed EV suggests.

**Lowering σ to 1.0** sharpens the distribution: top bucket gets more mass (helps YES side modestly), tail buckets get less mass (drops bad NO bets entirely). The NO pickset moves to deeper tails where the model is right almost all the time.

### What it costs

Not much. The YES strategy is robust to sigma changes because it always picks the peak bucket — only the magnitude of the printed probability changes, not the pick. NO strategy is more sensitive (and benefits more).

The risk is that σ=1.0 is **too** sharp for cities with genuinely wider forecast distributions (e.g. KXHIGHDEN at sd=2.06°F). A future improvement is per-city σ calibration, but the global σ=1.0 already wins.

## 2. Calibration with σ=1.0

### YES side

```
   sigma     n   mean_pred    realized       gap    brier
    1.00   207       55.0%       63.8%     +8.8pp   0.2291   <- best
    1.25   207       50.6%       64.7%    +14.1pp   0.2405
    1.50   207       46.9%       64.3%    +17.4pp   0.2528
    1.75   207       43.6%       62.8%    +19.2pp   0.2638
    2.00   207       41.1%       61.8%    +20.8pp   0.2730   <- deployed
    2.25   207       39.0%       57.0%    +18.0pp   0.2646
```

At σ=1.0, the model says 55% on its top pick and reality is 64%. **Still slightly underconfident but only by 8.8 pp** instead of 20.8 pp deployed.

### NO side

```
   sigma     n   mean_pred    realized       gap    brier
    1.00   196       87.6%       84.2%     -3.5pp   0.1050   <- best
    1.25   193       84.6%       80.3%     -4.2pp   0.1200
    1.50   194       81.4%       77.3%     -4.1pp   0.1356
    1.75   197       79.5%       72.6%     -7.0pp   0.1574
    2.00   194       78.2%       70.1%     -8.1pp   0.1770   <- deployed
    2.25   197       77.9%       67.0%    -10.9pp   0.1982
```

At σ=1.0 the model says 88% on its NO picks and 84% actually lose. **Only 3.5 pp overconfident** — almost calibrated.

## 3. Practical reading guide (after the σ=1.0 cutover)

This is the section to consult when you're looking at the live dashboard deciding whether to take a position.

### Step 1 — Identify which side the suggestion is

The dashboard surfaces three suggestions per event:

- **Highest probability** (the YES strategy): the model's top-pick bucket. The argmax of the probability column.
- **Best EV YES**: same as highest-probability unless the highest-prob has a poor `yes_ask`. Usually the same row.
- **Best EV NO**: the bucket with the largest `(1 − P_yes) − no_ask`, gated by the sanity cap (`yes_ask ≥ 0.85 AND p_yes ≤ 0.40` → skip).

### Step 2 — Adjust the printed probability for residual miscalibration

Even at σ=1.0, the model has small residual gaps. Use these mental adjustments:

| Side | Printed prob | Calibrated prob (what to actually believe) |
|---|---|---|
| YES | ≥ 80% | Take printed value at face |
| YES | 60-80% | Add ~5 pp |
| YES | 40-60% | Add ~8-10 pp |
| YES | < 40% | Add ~10-15 pp (small sample, treat warily) |
| NO  | ≥ 90% | Subtract ~3 pp |
| NO  | 75-90% | Subtract ~3-5 pp |
| NO  | 60-75% | **Skip the bet** (still danger zone — sigma fix shrank but didn't eliminate it) |
| NO  | < 60% | **Skip the bet** (model is unreliable here) |

### Step 3 — Decide using calibrated EV

The dashboard shows EV = (printed_prob − price). Recompute mentally using the calibrated prob from Step 2:

```
True EV (YES) = (printed_prob + adjustment) − yes_ask
True EV (NO)  = (printed_prob − adjustment) − no_ask
```

| True EV | Action |
|---|---|
| ≥ 8¢ | Take the bet |
| 5-8¢ | Take, but smaller size |
| 2-5¢ | Skip (margin too thin for execution risk + spread) |
| ≤ 2¢ | Skip |

### Step 4 — Time-of-day rules

Lead time doesn't change the model's output in our replay (Open-Meteo archive returns frozen historical forecasts), but it changes **prices** and **NO-side calibration in live trading**:

- **YES side**: Earlier in the market's life (T-24h to T-36h) gets you wider spreads → better entry prices but thinner volume. Smaller bets fine; larger bets may slip.
- **NO side**: Strongly favor T-24h or later. NO calibration was much worse at T-12h in the earlier report (gap −26 pp); even after σ=1.0, the NO danger zone is widest when the market is least mature.

Recommended window for placing positions: **noon to 1pm local on the day before resolution** (T-36h to T-30h). The leadtime sweep showed this captures the best price-tightening edge.

### Step 5 — Sizing by edge

Once you have a calibrated True EV and you've decided to take the bet, size relative to your bankroll:

```
Bet size = bankroll × fractional_kelly × (true_EV / yes_ask_or_no_ask)
```

Where `fractional_kelly` is typically 0.25 (quarter-Kelly) for safety. So:

- True EV 10¢ on a 30¢ YES: bet = bankroll × 0.25 × (0.10 / 0.30) ≈ 8.3% of bankroll
- True EV 10¢ on a 80¢ NO: bet = bankroll × 0.25 × (0.10 / 0.80) ≈ 3.1% of bankroll

The NO side is cheaper to bet (high prob, low edge per dollar) but smaller per-unit returns. The YES side is the opposite. The lab's PnL numbers above are at per-$1-unit notional and don't reflect Kelly sizing — actual dollar PnL with quarter-Kelly is roughly the lab figure × your bankroll × 0.05.

### Step 6 — Hard skip rules (regardless of EV)

| Condition | Why |
|---|---|
| NO bet with printed prob between 60% and 75% | Danger zone — historically 30 pp overconfident in this bin |
| NO bet with `yes_ask ≥ 0.85` AND model `prob ≤ 0.40` | Sanity cap — the market knows something the model doesn't |
| Any bet with `yes_bid` and `yes_ask` more than 5¢ apart | Spread is too wide; calibration gain gets eaten by execution |
| Any bet within 1h of close on the laptop | The model's mu can drift as METAR comes in (today_max push fires); your read is stale |
| Bet on an event you don't have prices for in the cache | Replay said "no_price"; live the entry might be impossible |

## What to ship and when

### Now

- **One-line change:** in `lab/configs.py`, swap `base_sigma=_kt.BASE_SIGMA` (= 2.0) to `base_sigma=1.0` on `LIVE_TODAY`. That makes the lab + shadow runner immediately use σ=1.0 on every fresh shadow row.
- The live dashboard (`kalshi_temp.py`) still uses `BASE_SIGMA = 2.0` because `model.compute(inputs, LIVE_TODAY)` now reads from `LIVE_TODAY.base_sigma`. Wait — actually yes, the live path picks up the new `LIVE_TODAY.base_sigma=1.0` automatically because `build_event_data` calls `compute(inputs, LIVE_TODAY)`. **Just restart the server after committing.**
- **All four `LIVE_MINUS_*` variants inherit `LIVE_TODAY.base_sigma`** via `dataclasses.replace`, so they'll also flip to 1.0 — that's fine, intentional.

### After 2 weeks of shadow data

- Re-run `lab shadow-summary --config <variant>` to confirm σ=1.0 holds up under live NWS + today_max
- Per-city sigma calibration (e.g. DEN at 1.5, MIA at 1.0) once we have enough events per city to refit
- Recompute the calibration table on shadow-mode output; expect the YES gap to widen slightly because live NWS overlay adds noise that backtest doesn't see

### Don't ship

- Per-event mu override based on observed today_max trajectory (premature; needs shadow to validate)
- Asymmetric distribution model (some cities exhibit upper-tail bias)
- Sigma scaling by forecast lead-from-issue-time (depends on shadow data)

## Limitations

- 207-event sample, 30 days. Bootstrap CI not run; the +$14.84 PnL improvement is point estimate
- Replay uses no NWS overlay and no today_max (Open-Meteo archive limitation) — production formula has both. Direction of sigma improvement should hold but absolute PnL differs
- σ=1.0 might be too sharp for KXHIGHDEN specifically (per-city sd 2.06°F vs global 1.55°F average). Watch the per-city PnL after cutover; if DEN regresses, consider per-city σ table
- Calibration bins beyond [0.6, 0.7) still have noticeable miscalibration. Closer-to-Platt-scaling recalibration is the eventual right answer
