# Hourly qualifying-signal report (interim bar)

**Date:** 2026-05-28
**Branch:** `signal-report` (off `model-lab`)
**Status:** design approved, pre-implementation
**Lifespan:** interim/temporary — the "probation" bar tied to the 2026-05-27 weighted-σ tightening. Revisit/retire once the post-deploy live calibration confirms or corrects the YES overconfidence.

## Purpose

Every hour, surface the model's current picks that clear the **interim conservative bar**, delivered two ways: (1) a push notification to the phone, (2) a panel on the live dashboard that shows the picks **and the time the latest report was generated**. Detection/reporting only — it never trades or changes the model.

## The interim bar (precise filter)

Start from the model's canonically-surfaced picks (reuse `kalshi_temp.best_yes_pick` / `best_no_pick`, which already apply the deployed calibration + floors + sanity cap), then apply the probation overlay:

**YES pick qualifies iff:**
- `cal_ev_yes` >= **0.10** (the raised interim bar, vs the standing 0.05), AND
- `cal_ev_yes` <= **0.25** (drop "huge" edges — post-tightening these are likely σ-artifacts, not alpha), AND
- **market agreement:** the model's YES bucket is the market favorite or an immediate neighbor. Market favorite = the open bucket with the highest market mid `(yes_bid + yes_ask)/2`; "neighbor" = adjacent bucket in the strike ladder, AND
- **executable:** spread `yes_ask - yes_bid` <= **0.05**.

**NO pick qualifies iff:**
- `cal_ev_no` >= **0.10**, AND `cal_ev_no` <= **0.25**, AND
- printed `1 - P_yes` >= **0.90** (the standing "NO only when very confident" floor), AND
- the deployed sanity cap already enforced by `best_no_pick` holds (not fighting a `yes_ask >= 0.85` market while the model rates the bucket <= 0.40), AND
- spread `no_ask - no_bid` <= **0.05**.

Each qualifying pick also carries its `lead_hours`; the report annotates picks outside the T-36h–T-24h window (esp. NO inside T-12h) as lower-confidence rather than dropping them — the bar above is the gate.

Thresholds are constants at the top of the script so they're trivially tunable as the live calibration comes in.

## Architecture (three decoupled units)

1. **Engine — `hourly_signals.py` + Windows task `WeatherbotSignals` (hourly, always-on).**
   - Enumerates currently-open KXHIGH events across all 7 cities (`kalshi_temp.list_events_for_series(series, days=2)`, status open).
   - For each, calls `kalshi_temp.build_event_data(event)` (the canonical live path: `compute(LIVE_TODAY)` + `finalize_markets` + `best_yes_pick`/`best_no_pick`) to get the cal_* fields, market prices, and the surfaced picks.
   - Applies the interim bar above; collects qualifying picks.
   - Writes `hourly_signals.json` = `{"generated_at": <ISO8601 w/ tz>, "bar": "interim-2026-05-27", "n_open_events": N, "picks": [ {series, event_ticker, target_date, side, bucket, ticker, printed_prob, market_price, ev, lead_hours, in_window: bool, note} ] }` to the weatherbot root.
   - Pure filter logic (`qualifies(event_data, bar) -> list[pick]`) is separated from I/O so it's unit-testable with synthetic event data.
   - The filter lives ONLY here (single source of truth). Deterministic; runs regardless of any Claude session.

2. **Dashboard panel.**
   - New `GET /api/signals` in `kalshi_temp.py` serves `hourly_signals.json` (or `{}` + `stale: true` if missing/older than ~90 min).
   - A "Qualifying Signals — interim bar" panel in `DASHBOARD_HTML`, rendered at the top, showing each pick (city · bucket · side · printed prob · price · EV · lead) and a prominent **"Last updated: <generated_at> (<Δ ago>)"** line; if stale (>90 min), show it in a warning color so a stalled engine is obvious.
   - Requires one live-server restart to deploy.

3. **Push — durable Claude cron (this session).**
   - `CronCreate(durable: true, recurring: true, cron: "7 * * * *")` (off the :00 mark). The prompt: read `hourly_signals.json`; send one `PushNotification` summarizing qualifying picks (`"3 signals: LAX 67-68 YES 12c · NY 75-76 YES 11c · …"`) or "no qualifying picks this hour".
   - No filter logic in the cron — it only reads the engine's output (no drift).
   - **Known limits (accepted):** fires only while a Claude session is running/idle with Remote Control connected; auto-expires after 7 days (must be re-armed). The dashboard panel keeps updating via the Windows task regardless.

## Starting now

Run `hourly_signals.py` once immediately → writes the first `hourly_signals.json`; push the first report this session; the panel shows it. Then the Windows task + cron take over hourly.

## Guardrails

Read-only on the model: the engine calls `build_event_data` (compute) and reads prices; it MUST NOT place orders, write model code/config/`calibration_params.json`, or restart anything. Its only write is `hourly_signals.json` (gitignored — runtime artifact). The dashboard endpoint is read-only.

## Testing

- `qualifies(...)` pure filter: unit tests over synthetic event-data dicts — YES at/below/above the EV band, market-agreement pass/fail, spread too wide, NO printed<0.90, huge-edge drop, empty.
- Smoke: run `hourly_signals.py` once against the warm cache / live API; assert well-formed JSON with `generated_at` and a `picks` list, and that nothing outside `hourly_signals.json` is written.

## Out of scope

- Always-on push service (Pushover/ntfy) — offered, deferred; revisit if the cron's session-bound limit bites.
- A persistent historical signal log / backtest of the bar (the report is point-in-time).
- Tuning the bar — these are the documented interim constants; the live-calibration follow-up decides the durable numbers.
