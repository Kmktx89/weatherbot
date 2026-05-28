# Weatherbot model notes — consult before placing a bet

**Last updated:** 2026-05-27 · **Current config:** `LIVE_TODAY`, `BASE_SIGMA=1.0`, calibration layer live (NO haircut applied upstream of selection)

> **2026-05-27 — NO calibration shipped & deployed.** The NO adverse-selection fix is live. Two things now protect the NO side: the **printed-NO ≥ 0.80 selection floor** AND a **calibration haircut applied upstream of pick selection** — `kalshi_temp.best_no_pick` ranks on `cal_ev_no` (calibrated), not raw `ev_no`, so structurally negative-EV NO picks no longer surface anywhere. Every surface (dashboard, Predict tool, T-24 card, alerts, CLI) now **displays the calibrated `cal_prob_no` / `cal_ev_no` directly** — read the printed number at face value instead of applying a mental haircut. Merged to `model-lab` (`d18f855`), dashboard restarted 2026-05-27. Design: `docs/superpowers/specs/2026-05-27-no-calibrated-ev-selection-design.md`.
>
> **The haircut magnitude is PROVISIONAL.** NO defaults to **0.11** (code default `DEFAULT_CAL_PARAMS` in `kalshi_temp.py`), superseded by `calibration_params.json` when present. The authoritative value comes from the **06-03 two-week cut**: `python -m lab.cli live-calibration --days 14 --emit-params` then restart. A current floored-subset cut (n=15, 05-27) measures ~0.205, but n is too thin to commit. **Size NO conservatively until 06-03.** Validation context: `docs/superpowers/reports/2026-05-26-sigma-validation.md`.

This is the canonical quick-reference. Open it next to the dashboard.
For the full analysis behind any rule below, see the linked report.

---

## Current model state

| Knob | Value | Notes |
|---|---|---|
| `BASE_SIGMA` | **1.0 °F** | Graduated from 2.0 on 2026-05-19. +20% backtest PnL. |
| `nws_blend` | 0.3 | 30% NWS overlay on top of ECMWF/GFS blend |
| `today_max_mode` | `both` | Both raises mu AND truncates CDF when today_max binds |
| `today_max_headroom` | 0.5 °F | METAR-rounding allowance under TODAY-MAX |
| `today_max_push` | 0.3 °F | Mu push above truncation when today_max binds |
| `sanity_no_yes_ask_min` | 0.85 | Skip NO bet if market is this confident YES |
| `sanity_no_prob_max` | 0.40 | …and model thinks the bucket is this unlikely |
| `decision_lead_hours` | 24.0 | Used by `cmd_backtest` only; live dashboard recomputes per refresh |
| `MIN_PRINTED_NO` | 0.80 | Printed-NO floor: only surface a NO pick when model's `1 − P_yes` ≥ this |
| NO haircut | **0.11** (provisional) | Probability haircut applied upstream of selection (`apply_calibration`). From `calibration_params.json` (lab-emitted) or `DEFAULT_CAL_PARAMS`. Refit at 06-03 via `lab ... --emit-params`. |
| YES haircut | 0.0 (identity) | Machinery present but off — YES is ~unbiased; exact passthrough so YES picks are unchanged. |

Per-city `BIAS` and `SOURCE_WEIGHTS` in `kalshi_temp.py`. Were refit 2026-05-19, deltas within 0.3 °F of prior values — already correct for the backtest formula.

---

## How to read a YES pick (highest-probability bucket)

The dashboard shows the model's probability mass for that bucket. **It is NOT the model's confidence that the prediction is right.** Buckets are narrow (~1 °F), so even an accurate forecast puts only 30-60% mass on the peak.

**Calibrated adjustment table:**

| Printed prob | What to actually believe |
|---|---|
| ≥ 80% | Take at face value (model is well-calibrated at the top) |
| 60-80% | Add ~5 pp |
| 40-60% | Add ~8-10 pp |
| < 40% | Add ~10-15 pp |

**Decision:** True EV = (calibrated prob − `yes_ask`). Take if ≥ 5¢, skip if < 5¢.

YES side is consistently a touch underconfident even after the σ=1.0 graduation. Source: `2026-05-19-prediction-calibration.md`.

**Note on the calibration layer:** YES runs at **identity haircut (0)**, so the dashboard's displayed YES `cal_prob_yes`/`cal_ev_yes` equal the raw values — this table is still a *mental* adjustment you apply. (NO, by contrast, has the haircut baked into the displayed number — see below.) Turning the YES haircut on to fold this table into the display is deferred; YES is mild and well-behaved.

---

## How to read a NO pick (best-EV-NO bucket)

**As of 2026-05-27 the calibration is in-product — the displayed NO number is already calibrated.** The dashboard / Predict tool / T-24 card / alerts now show `cal_prob_no` and `cal_ev_no`, i.e. the model's NO confidence **after** the provisional 0.11 haircut. So read the printed NO EV/prob roughly at face value — you are no longer applying a mental adjustment. The old "pulled band table" is gone: the haircut lives in the number you see, not in your head.

**What `best_no_pick` enforces before a NO pick surfaces** (all three; structurally negative-EV picks are dropped):
- `cal_ev_no ≥ 0.05` — calibrated edge ≥ 5¢ (this is the haircut-adjusted EV, so a raw +7¢ that calibrates to −4¢ never appears)
- `(1 − P_yes) ≥ 0.80` — printed-NO floor (model's own pre-haircut conviction)
- sanity cap — not fighting a market at `yes_ask ≥ 0.85` while the model rates the bucket `≤ 0.40`

**Decision:** if a NO pick is shown, its displayed `cal_ev_no` is the edge — take if it's comfortably positive, **size conservatively** (the 0.11 magnitude is provisional, n=15, wide CI — see banner). If no NO pick is shown, there is no qualifying NO bet; do not hunt for one.

**Why the haircut matters (the original failure):** the NO strategy picks `argmax ev_no = (1 − P_yes) − no_ask`, largest exactly when `no_ask` is cheapest — i.e. when the *market* is most confident YES. So it systematically bets against the market's strongest convictions (live n=49: NO realized 46.5% vs 76.3% claimed, −29.8pp). The floor + upstream haircut together remove those structurally-bad picks. Full history: `docs/superpowers/reports/2026-05-26-sigma-validation.md`; design: `docs/superpowers/specs/2026-05-27-no-calibrated-ev-selection-design.md`.

**Caveat — the haircut is lead-anchored at T-24h.** The 0.11 (and the 06-03 refit) measure the gap at ~T-24h. NO miscalibration is worse closer to close; make the NO decision in the T-36h–T-24h window and treat a NO pick read at T-12h-and-in as a different, worse regime.

---

## Timing — when to place positions

| Window | What it gives you | Caveats |
|---|---|---|
| **T-36h to T-30h** (noon-1pm local day before resolution) | **Best price entry** (markets thinly traded → wider spreads → better fills for the model's edge). NO calibration is best here. | Volume is 3% of peak — small bets only, larger size may slip |
| T-24h | Default; balanced liquidity and edge | Status quo |
| T-12h | Most liquidity, market is fully formed | NO miscalibration blows out to −26 pp; **skip NO bets entirely** in this window unless printed prob ≥ 90% |
| T-1h | Peak open interest | Edge is mostly gone; market has converged |

**Important:** the model's PROBABILITY output doesn't change with lead time in the lab (Open-Meteo archive returns frozen historical forecasts). What changes is market prices. Once shadow-mode data accumulates we'll be able to measure how live forecasts evolve. Source: `2026-05-19-leadtime-sweep.md`.

---

## Hard skip rules (override any "good" EV)

| Condition | Why |
|---|---|
| NO bet with printed prob between 60% and 75% | Danger zone — 30 pp overconfident in this bin |
| NO bet with `yes_ask ≥ 0.85` AND model prob ≤ 0.40 | Sanity cap — market knows something the model doesn't |
| `yes_bid` and `yes_ask` more than 5¢ apart | Spread too wide; edge gets eaten by execution |
| Within 1h of close, sitting at laptop | Stale read; today_max may have just fired and shifted mu |
| Settled bucket has yes_bid ≥ 0.95 on dashboard | Event resolved; do not bet |

---

## Sizing

Quarter-Kelly:

```
bet_size = bankroll × 0.25 × (calibrated_true_EV / entry_price)
```

Where entry_price is `yes_ask` for YES bets, `no_ask = 1 − yes_bid` for NO bets.

Example: True EV 10¢ on a 30¢ YES → 8.3% of bankroll. True EV 10¢ on a 80¢ NO → 3.1% of bankroll.

The lab's PnL is at per-$1-unit notional and does not reflect Kelly sizing. Multiply lab PnL by `bankroll × 0.05` for a rough dollar conversion at quarter-Kelly.

---

## Quick mental model of how the model works

1. ECMWF and GFS historical forecasts blended per-city with `SOURCE_WEIGHTS`
2. Plus a 30% overlay from NWS (only in live, not in historical replay)
3. Minus a per-city `BIAS` correction (fit from 60-day error history)
4. → `mu` (the model's expected temperature)
5. `sigma` = √(BASE_SIGMA² + spread²) where BASE_SIGMA=1.0 and spread = **weighted** std of the sources, using the same effective weights μ assigns them ((1−nws_blend)·source_weight for ecmwf/gfs, nws_blend for nws). (Was an unweighted pstdev before the weighted-σ branch — that over-weighted suppressed sources, esp. LAX ECMWF.)
6. Each bucket gets `P = N(upper; mu, sigma) − N(lower; mu, sigma)`
7. If TODAY-MAX has been observed mid-afternoon, mu may be pushed up and the CDF truncated below TODAY-MAX − 0.5 °F (today_max_mode="both")
8. YES bet picks argmax(P_yes); NO bet picks argmax(EV_NO) with sanity cap

---

## Reports referenced

All in `docs/superpowers/reports/`:

- **2026-05-19-divergence-attribution.md** — why historical replay can't see the live-vs-backtest gap; recommends forward shadow trial
- **2026-05-19-leadtime-sweep.md** — T-36h price-entry advantage (+22% YES PnL vs T-24h), volume profile by lead time
- **2026-05-19-prediction-calibration.md** — how to read probabilities; YES underconfidence vs NO overconfidence framework
- **2026-05-19-sigma-sweep-and-reading-guide.md** — the σ=1.0 graduation rationale and reading rules (deeper version of this page)

Plus the spec at `docs/superpowers/specs/2026-05-19-weatherbot-model-lab-design.md` and the plan at `docs/superpowers/plans/2026-05-19-weatherbot-model-lab.md`.

---

## What's still TODO

- **NO adverse-selection — ARCHITECTURE DONE & DEPLOYED 2026-05-27.** Calibration now runs upstream of selection: one `apply_calibration` adds `cal_*` fields, `best_no_pick`/`best_yes_pick`/`predict_summary` rank on calibrated EV, the three duplicated selection copies are collapsed into the canonical `kalshi_temp` functions, and every surface displays `cal_*`. Haircut sourced from `calibration_params.json` + code default (no magic numbers committed). Merged `d18f855`, dashboard restarted. Spec: `2026-05-27-no-calibrated-ev-selection-design.md`; plan: `docs/superpowers/plans/2026-05-27-no-calibrated-ev-selection.md`. **Remaining (magnitude only):** (a) **06-03 refit** — `python -m lab.cli live-calibration --days 14 --emit-params` to replace the provisional 0.11 NO default with the two-week cut, then restart; (b) consider banding (0.80-0.90 vs ≥0.90) and a lead dimension once n supports it; (c) test `today_max_mode="truncate"` (drop the +0.3 push — `live-minus-push` had the best variant WR).
- **σ=1.0 forward validation — first cut DONE 2026-05-26** (`lab live-calibration`, n=49): YES validated (+0.6pp), NO failed the backtest promise (−29.8pp). **06-03 decision cut still pending** — re-run `python -m lab.cli live-calibration --days 14` (+ `--by-lead`, + `--emit-params` to deploy the refit) for tighter CIs. The NO "take" numbers are now data (`calibration_params.json`), not a doc table — the refit is a one-command emit + restart, no code edit.
- **Hourly LIVE_TODAY snapshotter** (`snapshot.py` → `live_picks_log.jsonl`) started 2026-05-20. Registered as Windows scheduled task `Weatherbot-Snapshot` (hourly, battery-tolerant, 5-min timeout). Writes one row per open event per fire with μ/σ, all bucket probs/EVs, market prices, and lead_hours against close. First calibration cut (live formula, lead-binned) usable after ~5 days of accrual (≈2026-05-25); two-week cut around 2026-06-03. The replay-based leadtime calibration in `prediction-calibration.md` measures only price-snapshot effects (forecasts are frozen in archive); this log measures live forecast drift too.
- **Weighted-σ — VALIDATED ON BRANCH \`weighted-sigma\`, PENDING MERGE/DEPLOY (2026-05-27).** σ now uses a *weighted* std honoring μ's effective source weights instead of an unweighted pstdev (fixes σ over-weighting sources μ suppresses). Dispersion re-run (interior cut, days=90), per-city k before→after: NY 1.05→1.06, CHI 0.81→0.82, MIA 0.85→0.88, **LAX 0.36→0.46 (modelSig 3.83→2.43, −37%)**, DEN 0.93→0.93 (exact control — equal weights, unchanged), AUS 0.76→0.77, PHIL 0.67→0.68; pooled 0.83→0.85. Mechanism correct, no regressions, μ unchanged (baselines regenerated). **LAX still over-dispersed (k 0.46):** even at 0.10 weight the chronically +6°F ECMWF still adds ~1.8°F weighted spread, flooring LAX σ ~2.4. **Follow-on (separate branch):** fully calibrate LAX by dropping the suppressed ECMWF from σ entirely or refitting \`base_sigma\`. Spec: \`docs/superpowers/specs/2026-05-27-weighted-source-sigma-design.md\`; plan: \`docs/superpowers/plans/2026-05-27-weighted-source-sigma.md\`.
- **Live-formula BIAS refit** — current BIAS is calibrated for the no-NWS replay formula. With NWS overlay added live, residual bias may exist. Refit after 30 days of `nws_log.jsonl`.
- **Closer-to-Platt-scaling calibration** — the YES gap is +8.8 pp at σ=1.0; not zero. A learned monotonic recalibration could close most of it. Eventually.
- **Lower the today_max_push** — `today_max_mode="both"` double-counts TODAY-MAX (raises mu AND truncates). Try `"truncate"` (truncation only) in a follow-up sweep.
- **Drop the `cache warm` CLI stub** or implement it.
