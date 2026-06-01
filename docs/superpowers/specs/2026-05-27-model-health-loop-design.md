# Model Health Loop — unprompted detection + reporting for the weatherbot model

**Date:** 2026-05-27
**Branch:** `health-loop` (off `model-lab`)
**Status:** design approved, pre-implementation

## Purpose

Make the weatherbot model's context **compound without being prompted**: a
daily, deterministic scan that turns the data already accumulating
(`live_picks_log.jsonl`, settled events, the lab diagnostics) into a
git-tracked report a Claude Code session reads at the start of any model
session. The loop **detects and reports only** — it never changes model code,
config, calibration params, never trades, never restarts anything. Every change
it surfaces still goes through the human-gated pipeline (TDD → two-stage review
→ validation → sign-off → deploy). See `~/.claude` memory
`weatherbot-model-improvement-loop` and `weatherbot-continuous-improvement-capability`.

This is **detection/reporting infrastructure**, safe to run unattended. The
*judgment* about what to do stays with the human-gated session that reads the
report.

## What it produces (the three pieces requested)

1. **Point-in-time health report** — current readings of the model's key
   diagnostics, each with a threshold and an OK / WATCH / ALERT status.
2. **Opportunity scan** — deterministic flags of *unexploited* edges (not just
   risks): cities/leads where realized YES edge is positive but picks sat below
   the selection threshold; per-city positive realized PnL that isn't being
   sized.
3. **Decision/change journal** — append-only record of model changes (what /
   why / validation result / deployed-or-held). Session-written; the scan
   auto-stubs an entry when it detects the deployed model changed since the last
   run, for the session to fill in.

(Trend-tracking was deliberately deselected. The git history of the overwritten
health report gives trend-via-diff for free; a dedicated time-series store is
out of scope — YAGNI.)

## Architecture (extends the existing `lab/` package — not a new subsystem)

### `lab/health.py` (new, pure-ish core)

- `@dataclass MetricReading`: `name, value, threshold, status ("OK"|"WATCH"|
  "ALERT"|"INSUFFICIENT_DATA"), n, note`.
- `@dataclass Opportunity`: `kind, scope (series/lead), value, n, note`.
- `@dataclass HealthReport`: `generated_at, days, readings: list[MetricReading],
  opportunities: list[Opportunity], deployed_model_changed: bool`.
- `run_health_scan(days, log_path, cache) -> HealthReport`: orchestrates the
  EXISTING diagnostics, no new model math:
  - **Calibration** (pooled + by-lead) via `lab.live_calibration.calibrate_live`
    / `calibrate_by_lead` → YES/NO gaps and Brier per lead bin.
  - **Per-city dispersion `k`** via a new small helper `dispersion_by_city`
    (a first-class, tested `lab/` diagnostic — self-contained, do not depend on
    any job-dir scratch script). Per settled event: `out = compute(inputs,
    LIVE_TODAY)`, `actual = bucket midpoint of the winning market`, standardised
    residual `z = (actual − out.mu)/out.sigma`. Over the interior-bucket cut
    (`strike_type == "between"`, where the ±1°F midpoint quantization is clean),
    report `k = sqrt(max(var(z) − Q, 0))` with quantization term
    `Q = (1/3)·mean(1/sigma²)`. `k≈1` calibrated, `k<1` over-dispersed, `k>1`
    under-dispersed. Reuses `lab.inputs.build_historical_inputs`,
    `lab.inputs.winner_of`, `lab.refit_bias.actual_high_midpoint`.
  - **Bias drift** via `lab.refit_bias.refit` on `LIVE_TODAY`: compare freshly
    fit per-city bias to the deployed `kalshi_temp.BIAS`; flag the delta.
  - **Live-vs-backtest divergence**: compare live calibration gaps to the
    backtest-promised figures.
- `scan_opportunities(days, log_path, cache) -> list[Opportunity]`: the one new
  analytic. Deterministic rules over the live log + settled events:
  - per (series) and per (lead-bin): realized YES win-rate minus mean implied
    market probability; flag positive edge ≥ `OPP_EDGE_MIN` over ≥ `MIN_N`
    events where the model's YES pick sat below the selection threshold (an
    edge we saw but didn't take).
  - per series: realized PnL of taken picks; flag consistently positive series
    that are under-sized.
- `render_report(report) -> str`: markdown.
- `detect_deployed_change(state_path) -> bool`: compares current `git rev-parse
  HEAD` for `model/` + a hash of `calibration_params.json` against a small
  stored marker file; True if changed since last run (drives the journal stub).

### `lab/cli.py` (extend)

Add subcommand: `python -m lab.cli health [--days N] [--log PATH] [--write]`.
Prints the report to stdout; with `--write`, overwrites `docs/MODEL_HEALTH.md`
and (if `deployed_model_changed`) appends a stub to `docs/MODEL_CHANGES.md`.

### Report files (git-tracked, repo root `docs/`)

- **`docs/MODEL_HEALTH.md`** — regenerated each run (overwritten). Sections:
  generated-at + window; health table (metric / value / threshold / status / n /
  note); opportunities list; "deployed model changed since last run" banner when
  applicable. Git history = trend record.
- **`docs/MODEL_CHANGES.md`** — append-only decision/change journal. Header
  documents the entry template (date / change / why / validation result /
  deployed-or-held / commit). Session-written; scan auto-stubs on detected
  deploy.

### Scheduling

- `run_health.bat` (mirrors `run_dashboard.bat` pattern): runs
  `python -m lab.cli health --write`, then commits **only** `docs/MODEL_HEALTH.md`
  and `docs/MODEL_CHANGES.md` (report-only commit; never touches model code, so
  conflict risk with a dev session is negligible — `git commit -- docs/...`).
- `WeatherbotHealth` scheduled task: daily ~13:00 UTC (same slot as the T-24
  card; the prior day's events have settled by then). Registered like the
  existing `WeatherbotDashboard` task. Registration commands documented; the
  task is created once by the operator.

### Repo `CLAUDE.md` pointer

Create `C:\Users\KrisKnecht\weatherbot\CLAUDE.md` (repo-scoped, layered under the
user-global one) with: *"At the start of any weatherbot model session, read
`docs/MODEL_HEALTH.md` (auto-updated daily) and `docs/MODEL_CHANGES.md` before
proposing model changes."*

## Thresholds (initial; constants in `lab/health.py`, tunable)

- **Min sample guard (applies to every metric):** if `n < MIN_N` (default 30),
  status is `INSUFFICIENT_DATA`, never a flag. Directly encodes the n≈15 lesson
  — no thin-sample false alarms or thin-sample-driven action.
- **Calibration gap** (per side/lead): `|gap| ≤ 5pp` OK; `5–10pp` WATCH;
  `>10pp` ALERT. NO carries a known structural offset (adverse selection), so a
  raw `>10pp` gap is expected, not news. Instead, score NO against its *expected
  residual after the deployed haircut*: `no_residual = gap + deployed_haircut`
  (the haircut from `calibration_params.json` / code default, currently 0.11);
  apply the same 5/10pp OK/WATCH/ALERT bands to `no_residual`, and annotate the
  row "NO: known structural offset; scored net of deployed haircut."
- **Dispersion k** (per city, interior): `0.8 ≤ k ≤ 1.25` OK; `0.65–0.8` or
  `1.25–1.4` WATCH; outside `0.65–1.4` ALERT. (LAX is currently ~0.36 → ALERT
  until the `weighted-sigma` fix lands.)
- **Bias drift** (per city, n ≥ MIN_N): `|fresh − deployed| ≤ 0.5°F` OK;
  `0.5–1.0` WATCH; `>1.0` ALERT.
- **Opportunity:** `OPP_EDGE_MIN` = +5pp realized YES edge over `MIN_N` events
  with picks below selection threshold.

## Guardrails (hard, tested)

The loop's only side effects are: reading data/cache, and writing
`docs/MODEL_HEALTH.md` / `docs/MODEL_CHANGES.md`. It MUST NOT import or call any
order/trade path, MUST NOT write `model/`, `kalshi_temp.py`, `lab/configs.py`,
or `calibration_params.json`, MUST NOT restart the dashboard. A test asserts the
`health` code path performs no writes outside the two `docs/` files.

## Testing

- `dispersion_by_city`, threshold/status classification, `scan_opportunities`
  rules, and `render_report` are pure functions over **synthetic** diagnostic
  inputs / log rows → full unit coverage, no network.
- `detect_deployed_change` tested against a temp marker file.
- A smoke test runs `run_health_scan` over a tiny synthetic log end-to-end and
  asserts a well-formed `HealthReport` + that only the two docs files would be
  written (guardrail test).
- Reused diagnostics (`live_calibration`, `refit_bias`) already have coverage.

## Out of scope

- Any autonomous change to the model, config, calibration, or trading.
- A dedicated time-series/metrics datastore (git history covers trends).
- An LLM-in-the-loop interpreter (judgment stays with the human-gated session).
- A dashboard UI panel (the surface is the git-tracked report + CLAUDE.md
  pointer; a panel can be a later, separate project).
- Auto-tuning thresholds (they start as documented constants).

## Sequencing note

Implementation should follow the `weighted-sigma` branch merging into
`model-lab` (so `dispersion_by_city` reflects the fixed σ). This branch is cut
from `model-lab`; rebase onto the merged `model-lab` before implementing.
