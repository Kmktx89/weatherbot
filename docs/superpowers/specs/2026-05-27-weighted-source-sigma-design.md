# Weighted-source σ — make dispersion respect the same source weights as μ

**Date:** 2026-05-27
**Branch:** `weighted-sigma` (off `model-lab`)
**Status:** design approved, pre-implementation

## Problem

`compute()` produces a Normal(μ, σ) over the daily-high. μ and σ treat the
forecast sources **inconsistently**:

- **μ** (`model/compute.py:_blend_sources`, lines 25–43) is a *weighted* mean.
  `weighted_mean` honors per-city `source_weights` (translated from
  `kalshi_temp.SOURCE_WEIGHTS`, `kalshi_temp.py:73–81`), then a separate
  `nws_blend` overlay (line 40–41) mixes in NWS at `nws_blend` weight.
- **σ** (`model/compute.py:_sigma_from`, lines 46–50) is
  `√(base_sigma² + pstdev(sources)²)` — an **equal-weight** `pstdev` over
  `cfg.sigma_sources`, ignoring `source_weights` and `nws_blend` entirely.

So a source that μ deliberately distrusts still contributes full weight to σ.

### Evidence (dispersion check, 2026-05-27, days=90, interior buckets)

Standardized residual `z = (actual − μ)/σ`; calibrated ⇒ `sd(z) ≈ 1`.
`k` = quantization-removed implied σ multiplier (`k>1` underdispersed,
`k<1` over-dispersed). Interior-bucket cut (clean quantization adjustment):

| series | ECMWF wt | interior k | model σ | realized err sd |
|---|---|---|---|---|
| NY   | .40 | 1.05 | 1.32 | 1.38 |
| CHI  | .40 | 0.81 | 1.34 | 1.21 |
| MIA  | .25 | 0.85 | 1.54 | 1.27 |
| **LAX** | **.10** | **0.36** | **3.83** | **0.98** |
| DEN  | .50 | 0.93 | 1.24 | 1.30 |
| AUS  | .30 | 0.76 | 1.41 | 1.05 |
| PHIL | .35 | 0.67 | 1.53 | 1.14 |
| pooled | — | 0.83 | 1.94 | 1.23 |

The model is **not** underdispersed (the original hypothesis is rejected;
pooled interior k=0.83). But **LAX is severely over-dispersed**: σ≈3.8 vs
realized error ≈1.0 — ~4× too wide. Arithmetic: σ=√(1+spread²) ⇒ the
ECMWF/GFS spread is ≈3.7°F. This is self-documented — the comment at
`kalshi_temp.py:71–72` reads *"LAX ECMWF is +6°F off — effectively
suppressed,"* and LAX's ECMWF weight is crushed to 0.10 in μ. μ is protected
(meanErr≈0); σ is not. The over-dispersion generalizes: every
weight-skewed city (k<1) is mildly over-dispersed for the same reason; LAX is
the extreme because its weight skew is extreme.

### Why it matters

LAX is the most-liquid book (2,000–5,500 contracts/bucket on a typical event).
An over-wide σ flattens the bucket distribution — under-rating the correct
bucket, over-rating the flanks/tails. That suppresses YES conviction and
manufactures spurious tail/flank EV, which is the exact shape that feeds NO
adverse-selection. Fixing σ sharpens the single most important market.

## Design

Replace the equal-weight `pstdev` in `_sigma_from` with a **weighted standard
deviation** using the *same effective weights μ implies*. Expanding μ:

```
μ = (1 − nws_blend)·[w_ecmwf·ecmwf + w_gfs·gfs] + nws_blend·nws
```

so each source's effective weight in μ is:

- `W_ecmwf = (1 − nws_blend) · source_weights[ecmwf]`
- `W_gfs   = (1 − nws_blend) · source_weights[gfs]`
- `W_nws   = nws_blend`

These sum to 1 (since `source_weights` sum to 1). The new σ:

```
x̄_w   = Σ Wᵢ·xᵢ / Σ Wᵢ          (over present sources)
spread = √( Σ Wᵢ·(xᵢ − x̄_w)² / Σ Wᵢ )
σ      = √( base_sigma² + spread² )
```

No new config and no arbitrary NWS weight — `nws_blend` already exists and
supplies it. σ becomes the weighted dispersion of the same sources under the
same trust μ assigns. For LAX live, ECMWF's σ-weight drops from ⅓ to
`0.7·0.10 = 0.07`, so its +6°F divergence is ~7% of the spread → spread
collapses → σ ≈ base_sigma ≈ 1.0, matching realized 0.98.

### Source of truth for the weights

`_sigma_from` already receives `(inputs, cfg)`. It reads:
- `cfg.source_weights.get(inputs.series, {})` — keys `"ecmwf"`, `"gfs"`
  (already translated in `lab/configs.py`).
- `cfg.nws_blend`.
- present values from `inputs.forecasts` for the names in `cfg.sigma_sources`.

`sigma_sources` is retained as the **eligibility allowlist** (which sources may
enter σ at all); the change is only that eligible sources are now *weighted*
rather than equal. For a source in `sigma_sources` with no entry in
`source_weights` and not `"nws"`, its weight is 0 (it drops out) — today
`sigma_sources` only ever contains `ecmwf`/`gfs`/`nws`, so this is a guard, not
a live path.

### Edge cases

1. **Missing source** → exclude it; renormalize remaining weights by `Σ Wᵢ`
   (mirrors `weighted_mean`'s `total_w` renormalization). Archive (NWS=None):
   weights reduce to `source_weights` over ECMWF/GFS → LAX σ≈1.0. ✓
2. **One source present** → spread = 0, σ = base_sigma. Unchanged from today
   (`pstdev` of a single value is 0).
3. **No source present** → μ is already None; σ returns None (unchanged; the
   `mu_raw is None` short-circuit at `compute.py:54–60` handles it).
4. **`nws_blend = 0`** (`BACKTEST_TODAY`, `LIVE_MINUS_NWS`) → NWS weight 0; σ =
   weighted ECMWF/GFS spread by `source_weights`. Fixes the backtest path too.
5. **Equal ECMWF/GFS weights** (DEN 0.50/0.50) → in the **archive/no-NWS** path
   (where the dispersion check runs) weighted std == `pstdev` exactly, so DEN σ
   is **exactly unchanged** — DEN is the control for gate 1. Live (nws_blend
   0.3) DEN weights become 0.35/0.35/0.30 vs the old equal ⅓ each, a negligible
   shift; DEN is ~unchanged live.

## Blast radius

Monotonic in weight-skew, self-targeting:

| | NY | CHI | MIA | LAX | DEN | AUS | PHIL |
|---|---|---|---|---|---|---|---|
| effect | ~none | small | moderate | **large** | **none** | moderate | moderate |

Shrinking σ raises k toward 1, so the fix moves every over-dispersed city
toward calibration and leaves balanced DEN exactly where it is. Shared
`compute()` is used by live (`build_event_data`), backtest
(`backtest_one_event`), `lab.replay`, and `shadow.runner` — all change
together, which is correct (they should agree).

## Validation gates (must pass before any deploy)

1. **Re-run the dispersion check** (job-dir `dispersion_check.py`) on the new
   code. Accept: per-city interior k converges toward 1.0 (LAX 0.36→~1.0); DEN
   unchanged. Reject/iterate if any city **overshoots** to k>1.3 (would mean σ
   collapsed too far and `base_sigma` needs a bump).
2. **Code-level before/after replay.** Because the change is inside `compute()`
   (not a config field), `lab compare` cannot A/B it — both configs would run
   the patched code. Instead run `python -m lab.cli replay --config live-today
   --json` in **both checkouts** — `model-lab` (old σ) and this worktree (new
   σ) — over the same event window, and diff YES/NO bets, win-rate, and PnL.
   Expect changes concentrated on LAX; confirm no material harm elsewhere. (If
   the two-checkout diff proves too noisy from separate caches, fall back to a
   temporary `weighted_sigma` bool on `ModelConfig` so `lab compare` can A/B
   with bootstrap CI — added only if needed, removed after validation.)
3. **Per-city live calibration** (`lab live-calibration`, not just pooled) —
   sharpening LAX makes its YES more confident; confirm this does not push
   pooled/﻿per-city YES into material overconfidence (the by-lead YES pooled gap
   was only −2.2pp at the T-24h leads; LAX may have been offsetting it).

## Risks

- **Over-shrink → under-dispersion** on moderate-skew cities (MIA/AUS/PHIL).
  Gate 1 catches it.
- **`base_sigma=1.0` becomes more load-bearing** once inflated spread is
  removed; several cities may then cluster at σ≈base_sigma below their realized
  sd. Follow-on `base_sigma` sweep may be warranted — **out of scope here**,
  noted for a separate pass.
- **Pooled-YES calibration interaction** (gate 3).

## Out of scope

- Re-tuning `base_sigma` or `source_weights` (separate pass; this change only
  makes σ *consistent* with the weights that already exist).
- The NWS fit-vs-apply bias gap (separate finding, separate decision).
- Regime-conditional σ (rejected this session as data-starved).

## Implementation note (module-fit)

This **extends an existing function** (`_sigma_from`) — no net-new module,
skill, or artifact. Pure change to the shared `compute()` pipeline plus tests.
