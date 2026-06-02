# Model change journal

Append-only. One entry per deployed/held model change. Newest at the bottom.
The daily health scan appends a stub here when it detects the deployed model
changed; fill the stub in. Template:

## YYYY-MM-DD — <short title>
- change: <what changed>
- why: <motivation / the finding it addresses>
- validation: <gates run + results>
- deployed-or-held: <deployed (commit) | held (reason)>
- commit: <sha>

---

## 2026-05-27 — weighted-source sigma
- change: `_sigma_from` now weights the source spread by mu's effective weights instead of an unweighted pstdev.
- why: sigma over-weighted sources mu suppresses (LAX ECMWF +6F at weight 0.10) -> LAX ~4x over-dispersed.
- validation: dispersion re-run (LAX interior k 0.36->0.46, sigma 3.83->2.43; DEN control exact; pooled 0.83->0.85); mu unchanged (baselines regenerated); full suite green; opus final review.
- deployed-or-held: deployed; live LAX sigma=1.236 confirmed = weighted formula.
- commit: c60d193 (merged to model-lab a06d3d0)

## 2026-06-01 — daily-loop unblock + live-path forecast resilience (Problem 2)
- change:
  1. **Health-scan crash fix** (`lab/health.py:_opportunity_records`): the loop var
     `ev` (event ticker) was rebound to the EV float before `ev.split("-")`, raising
     `AttributeError: 'float' object has no attribute 'split'`. Renamed the float to
     `ev_val`. This had been crashing the daily WeatherbotHealth task on every run
     since ~2026-05-28, so MODEL_HEALTH.md was frozen 3 days stale and the detection
     loop was blind. Added regression test `test_opportunity_records_handles_taken_pick`
     — the one code path the suite never exercised (run_health_scan monkeypatches
     `_opportunity_records` away, which is why the bug shipped green).
  2. **Forecast retry + on-disk cache** (`kalshi_temp.py`): open-meteo / NWS / METAR
     calls had a single 15s-timeout attempt and no caching. Added `_get_json_retry`
     (8s/attempt, 3 attempts, 0.6s linear backoff — transient ConnectionReset/Timeout
     only) and a small sqlite cache (`forecast_cache.sqlite`, reusing `lab.data_cache`):
     forecasts TTL 30 min, METAR TTL 5 min. Failed fetches are never cached (so an
     outage retries next call); the historical/backtest path bypasses this cache
     (lab.inputs owns its own). Live Kalshi prices are NOT cached.
- why: the daily health loop was hard-down (crash); and `get_dashboard_data(force)`
  wall-clock ranged 7.7s–107s, the spikes driven by forecast sockets hanging the full
  15s with no retry. Resilience + cache attack the variance, not the model.
- validation:
  - full pytest suite green (incl. 4 new `tests/test_forecast_cache.py` + the health
    regression); RED-confirmed both new tests before fixing.
  - daily health scan now runs end-to-end and regenerated MODEL_HEALTH.md
    (2026-05-28 stale → 2026-06-01T04:05; 2 ALERT / 2 WATCH / 0 opp, no crash).
  - perf (same process, back-to-back, network degraded today so wall-clock is noisy):
    COLD dashboard build 23.0s / 51 forecast round-trips → WARM 6.2s / 31 round-trips.
    Successful forecasts are cached (eliminated on warm); the 31 residual are failed
    fetches correctly re-tried (not cached) on a bad-network day — approaches ~0 warm
    round-trips on a healthy network. `fetch_kalshi_events` left unchanged: baseline
    is a stable 5.2s = 21 calls × 0.25s rate-limit gap, i.e. entirely gap-bound;
    threading re-serialises on that lock (RTT is negligible) so it was measured to buy
    nothing. Real Kalshi lever (collapse 21→7 calls via `with_nested_markets`) deferred
    — it rewrites the live pick payload shape, too much live-money risk for this pass.
- suspects checked explicitly (per the brief): PostToolUse `pytest --co -q` hook —
  ABSENT (not configured anywhere; only present as an allow-list entry, never as a
  hook), so nothing to debounce. Synchronous subagent chaining — NONE in the runtime
  (the scheduled tasks are plain python; "subagents" exist only in the dev/gated
  pipeline). Permission/deny friction — the active PostToolUse hook
  (`kmk-knowledge-drift-guard.sh`) cheap-skips (`exit 0`) on any payload without
  "kmk-os", so it adds ~one bash spawn per weatherbot save: negligible.
- deployed-or-held: HELD on model-lab (branch `wb-autonomous-fixes`) pending
  human-gated deploy. Detection-only doc/code on the lab branch; not merged to deploy.
- commit: _set on commit_

## 2026-06-01 — shared threshold module (Problem 3, consolidation only)
- change: new `wb_thresholds.py` (pure leaf, zero imports) is the single source of
  truth for every selection / calibration / health threshold. `kalshi_temp.py`
  (backtest model + live picks), `lab/replay.py` (backtest replay), and
  `lab/health.py` (daily scan) now import from it; the duplicated literals in
  `lab/replay.py` (SANITY_* / MIN_BEST_EV) and the standalone copies in
  `kalshi_temp` / `lab.health` are gone. `DEFAULT_CAL_PARAMS["no"]` band wired to
  `MIN_PRINTED_NO` / `NO_HAIRCUT` (no more magic 0.80 / 0.11).
- why: backtest, live, and health must share ONE definition so a threshold can't
  silently drift between them. Per the new `weatherbot-backtest-harness` skill.
- validation: NO VALUE CHANGED — `tests/test_thresholds.py` pins every shared value
  to its pre-consolidation number and asserts each consumer re-exports the same
  value; the existing health classification tests (which characterize scan output)
  remain green, i.e. the scan classifies identically before/after. Full suite green;
  run_backtest smoke OK with the shared constants.
- deployed-or-held: HELD on model-lab (`wb-autonomous-fixes`) pending human-gated
  deploy. Pure refactor (no behavior change), but it touches kalshi_temp.py so the
  deployed-model fingerprint shifts — expected.
- commit: _set on commit_

## 2026-06-01 — deployed model changed (stub)
- change: _fill in_
- why: _fill in_
- validation: _fill in_
- deployed-or-held: _fill in_
- commit: _fill in_

## 2026-06-01 — live-path bias monitoring (fix bias_drift blind spot)
- change: renamed health metric `bias_drift_<series>` -> `bias_drift_replay_<series>`
  (+ note "no-NWS replay refit (excl. live NWS overlay)") to stop it overclaiming
  live coverage; added network-free `bias_resid_live_<series>` + `bias_resid_live_pooled`
  measuring the live NWS-blended μ (nearest-T24 snapshot) vs settled-bucket midpoints.
  Detection only. New thresholds (`BIAS_RESID_WATCH=0.5`, `BIAS_RESID_ALERT=1.5`,
  `BIAS_RESID_POOLED_MIN_N=30`) in `wb_thresholds`; `classify_abs` gained an
  overrideable `min_n` kwarg so the pooled gate routes through the shared classifier.
- why: `health._refit_bias` -> `refit_bias.refit(LIVE_TODAY)` -> `build_historical_inputs`
  hard-codes `nws=None` (`lab/inputs.py:113`), so `bias_drift` was structurally blind
  to the live 30% NWS overlay; nothing measured live-path bias. Per-city is noise at
  this n (CI ±2°F); pooled is the early-warning aggregate. Spec/plan:
  `docs/superpowers/specs|plans/2026-06-01-live-path-bias-monitoring.*`.
- validation: full pytest suite green (210; new threshold/parser/health tests
  RED-confirmed first, incl. adversarial-review fixes — pooled OK/ALERT branches,
  unrounded-mean classification, lead boundary). Real end-to-end `lab.cli health`
  (against the live log) exits 0, emits the renamed metric + new readings, no crash,
  no doc written. Live readings reconcile with an independent throwaway join
  (pooled -0.54/n=46 vs -0.50/n=43; CHI/LAX/MIA/DEN per-city match): per-city all
  INSUFFICIENT_DATA (n=4-8 < 30), pooled WATCH at -0.54°F (faint warm tilt, just past
  the 0.5 line).
- deployed-or-held: deployed to model-lab 2026-06-01 (PR #4 merged to main, then
  cherry-picked to model-lab).
- commit: 40aff80..1ddedd1 (model-lab); PR #4 -> main
