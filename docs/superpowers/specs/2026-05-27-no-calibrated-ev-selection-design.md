# Spec — calibrated-EV selection (one source of truth)

- **Date:** 2026-05-27 · **Branch:** `model-lab`
- **Motivated by:** `docs/superpowers/specs/2026-05-26-no-selection-fix.md` (its "secondary defect" + "do NOT rewrite the numbers" deferral) and the 2026-05-27 review session.
- **Type:** architecture-only. **Commits zero haircut magnitudes.**

## Problem

Two structural defects, both inherited from the 05-26 floor fix:

1. **Calibration is applied downstream of selection.** `kalshi_temp.best_no_pick` filters on `ev_no >= MIN_BEST_EV` and ranks by `ev_no` — the **raw, un-haircut** printed EV. The ~11pp NO overconfidence haircut exists only in `alerts.calibrate_no`, which runs *after* the pick is chosen and only adjusts *display text*. So a NO bucket with printed `+5¢` EV but true `≈ −6¢` (after the ~11¢ ≈ 11pp haircut) is selected, ranked, and surfaced; the haircut never gets a vote in whether it should appear. The `MIN_BEST_EV = 5¢` floor is symmetric across sides, but only the NO side is biased — so it lets structurally negative-EV NO picks through while being correctly strict on YES.

2. **NO selection is triplicated.** Three independent implementations of the same filter/rank/floor/sanity:
   - `kalshi_temp.best_no_pick` — claims in its docstring to be "the single source of truth across the dashboard, CLI, T-24 card, and alerts."
   - `alerts.best_ev_no` — re-implements it, with its own `MIN_PRINTED_NO` constant.
   - `generate_t24_card.predict_summary` — a third copy, with its own `MIN_BEST_EV` / `SANITY_*` / `MIN_PRINTED_NO` constants (lines 63–98).

   The **T-24 card applies no haircut at all** — it renders raw `p_no` / `ev_no`. The report you would actually trade off therefore shows the *most* inflated NO numbers in the system.

The root cause of the predecessor bug (a stale `−3/−4` adjustment table drifting from the measured `−9.4` / `−11pp` live reality) is a **hardcoded number diverging from what the lab measured.** This spec fixes that class of bug structurally, not by editing the number.

## Decision

Introduce **one calibrated-EV layer**, route all selection through it, collapse the three copies into the canonical functions, and source the haircut from a **lab-emitted artifact with a code fallback** so the measure→apply loop is closed.

Scope boundary (explicit, non-negotiable for this revision):

- **No haircut magnitudes are asserted.** The code default is a provisional placeholder (NO ≈ 11pp, YES = 0); the authoritative value is owned by `calibration_params.json`, refit at the 06-03 cut.
- **YES is identity today.** The layer is symmetric machinery, but `h_yes = 0`, so YES behavior is byte-for-byte unchanged. YES can be turned on later without re-plumbing.
- **Lead-dependence is not modeled.** The single T-24h-measured haircut is applied at all leads. The schema can grow a lead dimension at 06-03.

## Changes

### 1. `kalshi_temp.apply_calibration(market)` — the one transform

Computes four fields from the raw market: `cal_prob_yes`, `cal_prob_no`, `cal_ev_yes`, `cal_ev_no`. **Probability-space**, so the same numbers serve display (`cal_prob_*`) and selection (`cal_ev_*`).

**Sign convention (defined once, here):** `haircut = printed − realized`, so a **positive** haircut means **overconfident** (shrink the model's stated probability by subtracting). For NO:

```
cal_prob_no  = clamp01(printed_no − h_no)         # printed_no = 1 − prob
cal_ev_no    = cal_prob_no − no_ask
cal_prob_yes = clamp01(prob − h_yes)              # same subtractive form
cal_ev_yes   = cal_prob_yes − yes_ask
```

Both sides use the **same subtractive formula**; the sign of `h` carries the direction. NO is overconfident → `h_no > 0` (shrinks confidence). YES is *under*confident → if it is ever turned on, `h_yes < 0`, and subtracting a negative *raises* the probability. Today `h_yes = 0` (identity).

Worked example (NO): printed_no = 0.89, `h_no = 0.11` → `cal_prob_no = 0.78`. With `no_ask = 0.82`: raw `ev_no = 0.89 − 0.82 = +0.07`; `cal_ev_no = 0.78 − 0.82 = −0.04`. The pick that printed `+7¢` is really `−4¢` and now fails the `+5¢` floor.

**Exact-identity guarantee (refinement folded in):** when a side's haircut is exactly 0, that side's calibrated fields must equal the raw fields **bit-for-bit** — no clamp, no rounding, no float drift. Concretely, `h == 0 ⇒ cal_prob = prob` and `cal_ev = ev` by passthrough, not recomputation. This protects the YES side (and any future zero-haircut band) from the symmetric machinery silently flipping a borderline pick.

`apply_calibration` is **idempotent** and depends only on `prob`, `yes_ask`, `no_ask` (all present on every market and every historical log row).

### 2. Selection routes through calibrated EV

`best_no_pick` (and a symmetric `best_yes_pick`, replacing the inline `best_ev_yes` max) filter on `cal_ev_* >= MIN_BEST_EV`, rank by `cal_ev_*`, and keep the printed-NO floor + sanity cap. **Negative-calibrated-EV picks stop surfacing automatically** — that is the behavior fix, and it falls out of the routing rather than being a special case.

Back-compat: `best_no_pick` calls `apply_calibration` lazily on any market missing `cal_*` fields, so it operates identically on live markets (fields pre-computed in the pipeline) and on historical log rows (fields computed on read). See §6.

### 3. Collapse the three copies (refinement folded in)

- **Delete** `alerts.best_ev_no` and `generate_t24_card.predict_summary` and their duplicated constants; both re-point to the canonical `kalshi_temp` functions. The docstring's "single source of truth" claim becomes true. Shims are *not* retained — a wrapper just preserves a second place for logic to diverge, and §7's regression guard makes deletion safe.
- **Strip the `−3/−4` table** from `alerts.calibrate_no` / `calibrate_yes`. Display reads `cal_prob_*` off the market. If a thin formatter remains, it carries **no independent haircut number**. Binding invariant: *after this change there is exactly one place a haircut value can be set* — `calibration_params.json` (with the code fallback).

### 4. `calibration_params.json` + loader

`kalshi_temp` loads it once at startup. **Schema:** per-side, band-keyed map of printed-prob band → haircut (pp). Defaults now to a single `≥0.80` flat band per side; can grow more bands (and a lead dimension) later without a code change.

```json
{ "no": [{"lo": 0.80, "hi": 1.01, "h": 0.11}],
  "yes": [{"lo": 0.00, "hi": 1.01, "h": 0.00}] }
```

If the file is **absent or malformed**, fall back to a code-default constant (NO ≈ 0.11 provisional, YES = 0.0) and **log that the fallback fired**. The provisional default is flagged in-code as awaiting 06-03.

### 5. `lab` emits the artifact

`lab.cli` gains a way (e.g. `live-calibration --emit-params`) to write `calibration_params.json` from the live-calibration measurement: the printed-prob-vs-realized gap on the floored-pick set, per band. **One-pass** — it does *not* iterate the selection↔haircut fixed point (see §6 circularity note). 06-03 refit = re-run the command → overwrite the JSON → restart. No code edit.

### 6. `snapshot.py` and historical re-scoring

- `snapshot.py` serializes `cal_prob_*` / `cal_ev_*` into **new** log rows going forward (falls out of `apply_calibration` running in the prediction pipeline). New rows are self-describing.
- **Old rows (686, 05-20→05-27) lack `cal_*`.** They carry `prob`, `yes_ask`, `no_ask`, `ev_yes`, `ev_no` — sufficient for `apply_calibration` to compute `cal_*` on read (§2 lazy path). The 06-03 cut, which re-scores this exact log via `kt.best_no_pick`, therefore **keeps working** rather than breaking on its own historical data.
- **Measurement vs. selection circularity (resolved):** the params emitter measures `printed_prob` vs realized (the gap that *defines* the haircut) — it does **not** measure `cal_prob` vs realized. The haircut affects which picks are *selected*, but the measured quantity is the pre-haircut printed gap, so emission does not feed on its own output. One-pass is sufficient; fixed-point iteration is out of scope (§"Deferred").

## Testing (TDD)

- `apply_calibration`: prob-space math; `clamp01`; idempotency; **exact YES identity** — `h=0 ⇒ cal == raw` bit-for-bit on a fixture.
- `best_no_pick`: ranks/filters on `cal_ev_no`; a printed-`+7¢` / true-`−4¢` bucket is suppressed; floor + sanity preserved; **lazy-compute path** on rows lacking `cal_*`.
- Artifact: load; fallback-on-missing; fallback-on-malformed; sign-convention round-trip (emit → load → apply reproduces the measured gap).
- **Consolidation regression guard:** `alerts` pick == card pick == `best_no_pick` on shared fixtures (prevents the three copies re-diverging).
- **YES-unchanged regression:** YES picks identical pre/post on a fixture (guards the symmetric-machinery decision).
- Existing `test_baseline` / `test_compute` / `test_t24_card` / `test_live_calibration` stay green.

## What explicitly does NOT change

- No haircut *magnitudes* are committed; the default is provisional and owned by the artifact.
- YES surfacing is unchanged (`h_yes = 0`, exact identity).
- The running server is unaffected until restart — consistent with the existing deploy model.

## Deferred to the 06-03 cut (out of scope here)

- Exact pp values; flat vs banded NO magnitudes (`0.80–0.90` vs `≥0.90`).
- Lead-dependent haircut (worse near close; the schema can grow a lead dimension).
- Fixed-point iteration of selection↔haircut.
- Turning the YES haircut on (it is underconfident; identity for now).
