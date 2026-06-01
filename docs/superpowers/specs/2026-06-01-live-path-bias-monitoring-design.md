# Live-path bias monitoring — design

**Date:** 2026-06-01 · **Status:** approved design, pending implementation plan · **Type:** model-health-loop (detection only)

## Problem

`MODEL_HEALTH.md`'s `bias_drift_<series>` reads as if it monitors the deployed
(live) model. It does not. `health._refit_bias` → `refit_bias.refit(events, LIVE_TODAY)`
→ `lab/inputs.py:build_historical_inputs`, which hard-codes `forecasts={"ecmwf":…,
"gfs":…, "nws": None}` and `metar=None, today_max=None`. NWS forecasts are not
archived, so the historical/replay path *cannot* reconstruct the live NWS-blended
μ. The metric therefore validates only the **no-NWS replay** bias and is
structurally blind to whether the live 30% NWS overlay biases μ. `lab refit-bias`
shares the blind spot for the same reason. **No in-repo metric measures live-path
bias.**

Evidence (2026-06-01, throwaway join of `live_picks_log` live μ @≈T-24 vs settled-
bucket midpoints, n=43): per-city residuals are noise at 19 days (CIs ≈ ±2°F);
pooled residual −0.50°F (sd 1.99, SE≈0.3) — a faint warm tilt. The signal exists
but is only meaningful in aggregate at current n. See `docs/MODEL_NOTES.md` →
"Live-formula BIAS refit".

## Goals

1. Stop the metric from overclaiming: make the replay-path scope explicit in the
   metric **name**, not just a note.
2. Add a metric that actually measures live-path bias, sourced from the forward
   log (the only place live NWS-blended μ exists).
3. Match the statistics: a pooled early-warning flag usable now; per-city readings
   that become actionable once n fills (≈ the 30-day window, ~2026-06-12).

## Non-goals (YAGNI)

- No bias **refit** and no auto-correction. This is detection only.
- No change to selection / live-pick / backtest paths.
- No attempt to put NWS into the replay path (impossible — not archived).
- No lead-banding of the residual, no per-lead breakdown. Single nearest-T24 read.
- Pooled stays a **detector, not a fit**.

## Architecture

Two changes, both confined to existing files (module-fit: extend the health loop;
do not create a sibling module):

- `wb_thresholds.py` — new shared constants (single source of truth per the
  `weatherbot-backtest-harness` contract).
- `lab/health.py` — rename Part A; new `bias_resid_live_readings` (Part B); one
  wiring line in `run_health_scan`.

No new network path: reuses `lab/live_calibration.py` log-ingestion helpers
(`read_log`, `_by_event`) and the existing `classify_abs` classifier — the same
ingestion already used by `calibration_readings` and `_opportunity_records`.

## Part A — relabel the replay metric (honesty)

In `bias_drift_readings` (`lab/health.py`):

- Rename the emitted metric `bias_drift_<series>` → **`bias_drift_replay_<series>`**.
- Change the note to: `"no-NWS replay refit (excl. live NWS overlay); deployed {d:+.2f} fresh {f:+.2f}"`.

Rationale: the name is where the overclaim lives; a note alone (the
`dispersion_k_*` precedent) is easy to miss. Threshold (`|Δ| ≤ 0.5°F`) and banding
unchanged — only the label changes.

Impact: update name pins in `tests/test_health.py`; one-time cosmetic key
discontinuity in `MODEL_HEALTH.md` history (acceptable — the old key was
misleading).

## Part B — `bias_resid_live_*` (new live-path metric)

New pure function `bias_resid_live_readings(days: int, log_path: str) ->
list[MetricReading]`, mirroring the shape of `bias_drift_readings(days)`.
Network-free (no `cache` needed — see step 3).

### Data flow

1. `rows = lc.read_log(log_path, since_days=max(days, 60))` — same wide window as
   `bias_drift_readings` / `dispersion_by_city`.
2. Group: `lc._by_event(rows)`.
3. Per event:
   - Realized high = **midpoint of the `settled_bucket` subtitle** (e.g.
     `"94° to 95°"` → 94.5), read directly from whichever row carries
     `settled_bucket`; skip the event if none does (not yet settled). Interior
     buckets only; **open-ended buckets (`"X° or above" / "or below"`) are
     excluded** — their midpoints are not true highs. Log-pure: no
     `_winner_ticker` / market re-fetch (`settled_bucket` is the winning subtitle
     and is sufficient), so the function is deterministic and network-free.
   - Live μ: nearest-T24 pred row — `preds = [r for r in rs if lc._has_pred(r)
     and r["lead_hours"] is not None]`; `row = min(preds, key=lambda r:
     abs(r["lead_hours"] - 24.0))`; **require `abs(lead − 24) ≤ 12`** else skip.
     `μ = row["model"]["mu"]`; skip if `None`.
   - `resid = actual − μ`. **Sign: negative ⇒ μ runs hot** (over-forecasts).
   - Accumulate `resid` under `series = ev.split("-")[0]`.
4. Emit:
   - Per series: `bias_resid_live_<series>` = mean(resid), n, status (§ classification).
   - One `bias_resid_live_pooled` = mean over **all** interior residuals across
     cities; note: `"early-warning aggregate; NOT a correction — see per-city before acting"`.

### Subtitle → midpoint parser

A shared helper (`bucket_midpoint(subtitle) -> (mid, kind)` in
`lab/live_calibration.py`, the module that owns log-row interpretation):

- `"<a>° to <b>°"` → `((a+b)/2, "interior")`
- `"<z>° or above"` → `(z+1, "open")` — excluded from residuals
- `"<z>° or below"` → `(z-1, "open")` — excluded from residuals
- unparseable → `(None, None)` — event skipped

(Equivalent to `refit_bias.actual_high_midpoint` on strikes, but reads the log's
own subtitle so it needs no market re-fetch.)

### Classification

Per-city reuses the existing `classify_abs(mean, n=n, watch=BIAS_RESID_WATCH,
alert=BIAS_RESID_ALERT)` — it already gates `n < MIN_N → INSUFFICIENT_DATA` and
bands on magnitude:

```
n < MIN_N                       → INSUFFICIENT_DATA   (value still shown)
|mean| ≤ BIAS_RESID_WATCH       → OK
|mean| ≤ BIAS_RESID_ALERT       → WATCH
else                            → ALERT
```

Pooled classifies inline with the same bands but gates on
`BIAS_RESID_POOLED_MIN_N` instead of `MIN_N`.

## Thresholds & constants

New in `wb_thresholds.py` (imported by `lab/health.py`; pinned by
`tests/test_thresholds.py`, which also asserts each consumer re-exports the value):

| Constant | Value | Meaning |
|---|---|---|
| `BIAS_RESID_WATCH` | `0.5` °F | within → OK |
| `BIAS_RESID_ALERT` | `1.5` °F | within → WATCH; beyond → ALERT (looser than replay metric — live path noisier) |
| `BIAS_RESID_POOLED_MIN_N` | `30` | pooled sufficiency gate |

Per-city sufficiency reuses the existing `MIN_N` (no new constant). Three new
constants total.

At current data (~19 days): per-city mostly `INSUFFICIENT_DATA`; pooled `OK` at
≈ −0.5°F.

## Wiring

In `run_health_scan`, after `bias_drift_readings`:

```python
readings += bias_resid_live_readings(days=max(days, 60), log_path=log_path)
```

Uses the scan's existing `log_path`. (The function re-reads the log with its own
window rather than depending on the scan's `rows`, consistent with
`bias_drift_readings` / `dispersion_by_city`.)

## Error handling

Skip-not-crash on every gap: missing winner, no pred row, missing `model.mu`,
open-ended winning bucket, unparseable subtitle. No rebinding of loop variables
(the class of bug that took the loop down 2026-05-28, per `MODEL_CHANGES.md`
2026-06-01) — pure function, fresh names. Network-free: reads only the log.

## Testing (TDD — RED first)

`tests/test_health.py`:
- Synthetic log rows with known μ and known winner subtitle → assert per-city
  mean residual = actual − μ.
- Open-ended winning bucket → event excluded from residuals.
- Nearest-T24 selection picks the right row; a row with `|lead−24| > 12` is skipped.
- n-gate: `< MIN_N` → `INSUFFICIENT_DATA`; `≥ MIN_N` → correct OK/WATCH/ALERT band.
- Pooled = mean across cities' interior residuals; pooled n-gate honored.
- **Honesty pin:** assert the replay metric name is `bias_drift_replay_<series>`
  and its note contains "no-NWS replay" — so the relabel cannot silently regress.

`tests/test_thresholds.py`:
- Pin the four new constants; assert `lab/health.py` re-exports the same values
  (harness re-export invariant).

Validation gate (beyond units): run the **actual** `run_health_scan` end-to-end and
confirm it regenerates `MODEL_HEALTH.md` with the renamed metric + new readings and
no crash. (Units alone are insufficient — the 05-28 crash shipped green because the
scan monkeypatches `_opportunity_records` away; the validation must exercise the
real scan.)

## Deploy

Gated pipeline: TDD → spec/code review → validation → human-gated deploy. Held on
the lab branch (`worktree-model-notes-live-bias-finding` / a `wb-*` branch) until
human approval, consistent with the other 2026-06-01 `MODEL_CHANGES.md` entries.
On deploy, append a `MODEL_CHANGES.md` entry (what/why/validation/deployed).

## Follow-ups (out of scope here)

- Revisit an actual live-path bias **refit** only if the pooled / CHI / NY warm
  tilt survives to ~2026-06-12 with tighter CIs. Separate, gated change.
- If per-city n stays thin long-term, consider a variance-weighted pooled estimate
  rather than a simple mean. Deferred — simple mean is honest enough as a detector.
