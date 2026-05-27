# Spec — fix the NO adverse-selection

- **Date:** 2026-05-26 · **Branch:** `model-lab`
- **Motivated by:** `docs/superpowers/reports/2026-05-26-sigma-validation.md`

## Problem

The "Best EV NO" suggestion is selected as `argmax ev_no = (1 − p_yes) − no_ask`. That objective peaks exactly when `no_ask` is cheapest — i.e. when the market is most confident the bucket is YES. So the strategy systematically bets against the market's strongest convictions. Live validation (n=49): NO realized 46.5% vs the model's claimed 76.3% (−29.8pp), with the damage concentrated in the moderate printed-prob band.

The current sanity cap (`yes_ask ≥ 0.85 AND p_yes ≤ 0.40`) is too narrow to catch this — it misses NO bets on buckets the model itself rates p_yes 0.5–0.6.

Secondary defect (flagged in the model-lab spec §4): the NO selection is **duplicated and inconsistent** across four surfaces — `kalshi_temp.predict_event` applies the sanity cap + `MIN_BEST_EV`; the two `cmd_predict` copies apply *neither*; `alerts.py` and `generate_t24_card.py` each carry their own copy.

## Decision (empirically chosen)

Add a **printed-NO floor**: only surface a NO pick when `printed_no = 1 − p_yes ≥ MIN_PRINTED_NO`, with `MIN_PRINTED_NO = 0.80`. Candidate sweep on the live log (per-event nearest T-24):

| Rule | n | realized | gap | PnL/unit |
|---|---|---|---|---|
| baseline (current) | 43 | 46.5% | −29.8pp | −2.13 |
| +no_ask ≥ 0.20 | 39 | 51.3% | −27.6pp | −1.61 |
| +p_yes ≤ 0.40 | 38 | 55.3% | −25.8pp | −0.78 |
| **+printed-NO ≥ 0.80** | **27** | **77.8%** | **−11.1pp** | **+2.87** |
| p_yes≤0.40 & no_ask≥0.20 | 37 | 56.8% | −24.7pp | −0.59 |

Only the printed-NO floor flips PnL positive and roughly halves the gap. It keeps the 27 highest-conviction bets and drops the 16 worst. It is strictly more conservative than today's rule (a subset), so worst case it surfaces fewer NO picks.

**Do NOT rewrite the calibration adjustment numbers** (MODEL_NOTES table, `alerts.calibrate_no/calibrate_yes`). Those await the 06-03 decision cut (n too small to assert new pp values — advisor guidance). The selection floor alone removes the dangerous bands, so the stale adjustment numbers only ever apply to the ≥0.80 survivors and carry the existing stand-down warning.

## Changes

1. **`kalshi_temp.py`** — add `MIN_PRINTED_NO = 0.80` near `MIN_BEST_EV`, and a canonical:
   ```python
   def best_no_pick(markets):
       cands = [m for m in markets
                if m.get("ev_no") is not None and m["ev_no"] >= MIN_BEST_EV
                and m.get("prob") is not None and _sanity_keep_no(m)
                and (1 - m["prob"]) >= MIN_PRINTED_NO]
       return max(cands, key=lambda m: m["ev_no"], default=None)
   ```
   Replace all three inline selections (`predict_event` ~1730, `cmd_predict --json` ~1763, `cmd_predict` text ~1795) with `best_no_pick(data["markets"])`. This also fixes the two CLI copies that currently apply no gating at all.

2. **`generate_t24_card.py`** — mirror `MIN_PRINTED_NO = 0.80`; add the floor to `predict_summary`'s `bn` selection.

3. **`alerts.py`** — add the floor to `best_ev_no` selection. Leave `calibrate_no/calibrate_yes` numbers unchanged (pending 06-03); the floor means nothing < 0.80 surfaces.

4. **`lab/live_calibration.py`** — `_no_pick` imports/calls `kt.best_no_pick` so the validation tool stays in lockstep with the deployed bot. (Re-scoring old logs under the new rule reproduces candidate C above; new logs are scored by the same rule.)

## Tests

- `tests/test_live_calibration.py` — update NO fixtures for the new floor (buckets meant to qualify need printed-NO ≥ 0.80, i.e. p_yes ≤ 0.20); add explicit floor tests (a 0.75-printed bucket is excluded; a 0.85-printed bucket is kept).
- New `kalshi_temp` test for `best_no_pick`: sanity cap + MIN_BEST_EV + printed floor, argmax, `default=None`.
- `tests/test_t24_card.py` — update any `best_ev_no` expectation affected by the floor.
- Full suite green.

## Out of scope / deferred

- Recalibrating the adjustment pp values (06-03 cut).
- `today_max_mode="truncate"` (drop the +0.3 push) — separate sweep; `live-minus-push` had the best variant WR but YES-only and CI-overlapping.
- Per-city σ refit.

## Deploy

Inert until the live server restarts (HTML/Python loaded at process start). **Do not restart** as part of this change — the user deploys. After restart, re-run `python -m lab.cli live-calibration --days 7` to confirm the deployed NO picks match candidate C.
