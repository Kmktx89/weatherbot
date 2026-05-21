# T-24h Daily Card Page — Design

**Date:** 2026-05-21
**Status:** Approved, ready for implementation plan

## Purpose

Give the user a single dashboard page that shows, for each of today's KXHIGH events, the model's prediction frozen at the T-24h-before-close moment — same data the "Predict by Ticker" button on the dashboard would have produced at that moment. The card is a paper trail of the call that was made at the lead time the historical sweep (`sweep_leadtest.out`) identified as the best risk-adjusted entry window.

The user will use this card to inform daily selections. They understand the frozen T-24h prediction may differ from a live "Predict" run later in the day; the T-24h snapshot is the call of record.

## Non-goals

- **Not** a backtester replacement. The existing backtester rebuilds predictions from scratch using only ECMWF + GFS via `fetch_open_meteo(historical=True)` under `BACKTEST_TODAY` config (`kalshi_temp.py:1573`), and does **not** read `live_picks_log.jsonl`. The T-24h card uses the full 4-source live model (ECMWF + GFS + NWS + METAR + today-max). The two outputs are not directly comparable and the card does not feed the backtester.
- **No historical archive page** in this iteration. Just today's card. Per-day JSON files accumulate on disk and can power a history page later if wanted.
- **No new alerting.** The existing T-24h Pushover alerts (`alerts.py`, fires at lead ∈ [23.5, 24.5]) remain the notification path. The card is for visual review, not push.

## Architecture

```
snapshot_task.bat (hourly Scheduled Task, already running)
    └─> snapshot.py
        └─> appends rows to live_picks_log.jsonl

generate_t24_card.py  (NEW; runs once daily at ~13:00 UTC via Scheduled Task)
    └─> reads live_picks_log.jsonl
    └─> for each KXHIGH series with target_date == today (ET):
            select the row with min(|lead_hours − 24|)
    └─> writes t24_cards/YYYY-MM-DD.json

kalshi_temp.py serve  (existing dashboard process)
    ├─> /t24            (static HTML shell — new T24_HTML constant)
    └─> /api/t24        (new JSON endpoint)
                └─> reads t24_cards/<today-ET>.json
                └─> overlays settlement info by re-scanning live_picks_log.jsonl
                    for later rows where settled=true
```

The hourly snapshot already captures predictions at the T-24h moment as a side effect of running every hour. We don't need to schedule a separate "predict at exactly T-24h" call — we just pick which already-written row is the T-24h one.

## Components

### 1. `generate_t24_card.py` (new, ~80 lines)

Standalone script. CLI: `python generate_t24_card.py` (no args). Writes one file: `t24_cards/<today-ET>.json`.

**Selection logic:**

- "Today" = today's calendar date in America/New_York.
- Iterate `live_picks_log.jsonl` once. For each unique `(target_date, series)` where `target_date == today_et` and `model.mu is not None`, keep the row with `min(|lead_hours − 24|)`.
- Filter: only series in the dashboard's KXHIGH set (`KXHIGHNY`, `KXHIGHCHI`, `KXHIGHMIA`, `KXHIGHLAX`, `KXHIGHDEN`, `KXHIGHAUS`, `KXHIGHPHIL`).
- If a series has no snapshot for today, record an explicit `{"status": "missing"}` entry for it so the card can show "Waiting for T-24h snapshot."
- Write atomically: write to `t24_cards/<today-ET>.json.tmp` then rename. Prevents partial reads if the page loads mid-write.

**Output schema:** `t24_cards/2026-05-21.json`

```json
{
  "target_date": "2026-05-21",
  "generated_at": "2026-05-21T13:00:00+00:00",
  "events": [
    {
      "series": "KXHIGHNY",
      "city": "NYC",
      "event_ticker": "KXHIGHNY-26MAY21",
      "snapshot_ts": "2026-05-21T04:55:59+00:00",
      "lead_hours": 24.05,
      "close_time": "2026-05-22T04:59:00Z",
      "model": { "mu": 66.88, "sigma": 3.23, "sources": 3, "bias": -0.44 },
      "forecasts": { "ecmwf": 70.5, "gfs": 66.2, "nws": 63.0, "metar": 66.0 },
      "buckets": [ /* same shape as live_picks_log.jsonl bucket entries */ ]
    },
    { "series": "KXHIGHCHI", "city": "CHI", "status": "missing" }
  ]
}
```

### 2. Scheduled task `WeatherbotT24Card`

- **Trigger:** Daily at 13:00 UTC (≈ 8–9 AM ET year-round; well past every city's T-24h moment — NYC's T-24h is ~05:00 UTC, LAX's is ~08:00 UTC).
- **Action:** `pythonw.exe generate_t24_card.py` from `C:\Users\KrisKnecht\weatherbot`.
- **Idempotent:** safe to re-run. Overwrites the day's file.
- Registered via PowerShell `Register-ScheduledTask`, same pattern as `WeatherbotDashboard`.

### 3. `/api/t24` endpoint (new branch in `Handler.do_GET`)

- Resolves "today" in ET, opens `t24_cards/<today-ET>.json`.
- If today's file missing, opens the most recent prior file and adds `{"stale": true, "stale_for_date": "2026-05-21"}` to the response.
- Settlement overlay: after loading the frozen card, tail-scan `live_picks_log.jsonl` for each event's latest row where `settled == true`. If found, attach `settled_bucket` to that event in the response.
- Returns 200 + JSON, or 503 + JSON `{"error": "no t24 cards generated yet"}` if no archive file exists at all.

### 4. `/t24` static HTML page (new `T24_HTML` constant in `kalshi_temp.py`)

- Dark theme, Inter font, same `<style>` block as `/sources` for visual consistency.
- Header: `T-24h calls for <date>` + back link + "stale" badge if applicable.
- One stacked card per event in fixed city order (NYC, CHI, MIA, LAX, DEN, AUS, PHIL):
  - Card header: `CITY — TICKER` · `lead = 24.1h` · `snapshot HH:MM UTC` · `→ live` link
  - Forecasts row: `ECMWF X.X  GFS X.X  NWS X.X  METAR X.X`
  - Model line: `μ = X.X °F  σ = X.XX  sources = N`
  - Bucket table: columns `Bucket | p | YES bid/ask | EV(YES) | EV(NO)`
  - Conditional formatting: prob horizontal-bar (existing `probBg`-style), EV cells tinted green/red where `|EV| > 0.03` (same threshold as dashboard's `cls` helper, `kalshi_temp.py:687`).
  - Footer line: `Resolves HH:MM UTC` OR `Resolved: <bucket>` (green) if settled.
  - For missing-snapshot events: gray placeholder card `Waiting for T-24h snapshot`.

### 5. Dashboard header button

Add a third button to the existing header row in `DASHBOARD_HTML` (`kalshi_temp.py:631`):

```html
<button onclick="openPane('/t24', 'kt-t24', 700, 900)" style="background:#2f4366;">T-24h</button>
```

Same `openPane()` behavior — separate window on desktop, in-tab navigation on mobile / PWA.

## Data flow timing (example day)

| Time (ET)         | Event                                                                 |
|-------------------|-----------------------------------------------------------------------|
| 2026-05-20, all day | Hourly snapshot writes rows for tomorrow's events to live_picks_log.jsonl |
| 2026-05-20 ~21:00 | NYC's lead crosses 30h; rows enter the 30-24h window                  |
| 2026-05-21 00:55 UTC (~21:00 ET prior day) | NYC snapshot at lead ≈ 28h            |
| 2026-05-21 04:55 UTC (~00:55 ET) | NYC snapshot at lead ≈ 24h — **this becomes NYC's T-24h card row** |
| 2026-05-21 13:00 UTC (~09:00 ET) | Scheduled task runs `generate_t24_card.py`, writes 2026-05-21.json |
| 2026-05-21 throughout day | User opens dashboard, clicks "T-24h", sees frozen cards         |
| 2026-05-22 04:59 UTC | Events settle. Later snapshots write `settled: true` rows.        |
| Next page load    | Settlement overlay added live by `/api/t24`                          |

## Files touched / added

| Path                                                             | Change      |
|------------------------------------------------------------------|-------------|
| `kalshi_temp.py`                                                 | edit: add `T24_HTML`, `/api/t24` branch, "T-24h" header button |
| `generate_t24_card.py`                                           | new file    |
| `t24_cards/`                                                     | new directory (created by script; gitignored)                  |
| `.gitignore`                                                     | add `t24_cards/`                                               |
| Scheduled task `WeatherbotT24Card`                               | new (registered via PowerShell one-liner; setup documented)    |

## Decisions

- **Timezone for "today":** America/New_York. User is in NY; alerter is centered on ET. Cards filter on `target_date == today_et`.
- **EV highlight threshold:** `|EV| > 0.03`, matching the existing dashboard `cls` helper (`kalshi_temp.py:687`).
- **Best-of-snapshot selection:** nearest to lead=24h; no lead-band cap. If the closest row is, say, lead=22.5h or 26.5h because an hourly snapshot ran a bit early/late, take it. Real-world cadence drift is bounded by the hourly task interval.
- **Live-comparison link:** include `→ live` link on each card. Cheap to add, lets the user A/B the frozen call vs the current state.
- **Daily packaging time:** 13:00 UTC. Safely past every city's T-24h (latest is LAX at ~08:00 UTC). Avoids the user's intended morning review window.
- **Archive retention:** files accumulate in `t24_cards/`. No pruning. Roughly 5 KB per day; ~2 MB per year. Trivial.

## Testing

- Unit-test `generate_t24_card.py` selection logic against a synthetic `live_picks_log.jsonl` with multiple snapshots per event spanning leads from 30h down to 18h. Assert: the nearest-to-24h row is chosen; missing series are flagged; non-KXHIGH series are filtered out.
- Smoke-test `/api/t24` by running `generate_t24_card.py` against the real log and curl-checking the endpoint.
- Visual check `/t24` in browser (desktop pop-out and mobile in-tab).
- Verify `WeatherbotT24Card` scheduled task fires at 13:00 UTC; verify `t24_cards/<today>.json` exists afterward.

## Open questions

None. All resolved during brainstorm.
