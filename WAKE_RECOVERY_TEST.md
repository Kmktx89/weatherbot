# Wake-recovery test — 2026-05-18

Verifies that reloading the dashboard or clicking the Chrome bookmark after
the laptop has been idle gracefully recovers when the backend is briefly
unavailable. Backend implementation: `kalshi_temp.py` `fetchJsonWithRetry`
plus `visibilitychange` / `online` listeners (commit `53f43c9`).

## Method

Tested live in Chrome against the local dashboard at
`http://localhost:8765/`, instrumented via JS injected through the MCP
browser-automation tool. Steps:

1. Verified `fetchJsonWithRetry`, `refresh`, `openPane`, `visibilitychange`
   listener, and `online` listener are all defined on initial page load.
2. Spy-replaced `refresh` and dispatched `visibilitychange` + `online`
   events to confirm each listener invokes refresh.
3. Stopped `WeatherbotDashboard` scheduled task and killed the python
   process to take the server fully offline. Probed HTTP and confirmed
   `confirmed dead`.
4. Fired `refresh(true)` against the dead server and instrumented
   `window.fetch` + a `MutationObserver` on `#err` so every retry attempt
   and every err-text transition was captured.
5. Allowed the full 6-attempt retry budget to elapse, then started the
   scheduled task to revive the server. Verified final UX state.
6. Reloaded the page (natural user-flow recovery) and confirmed clean
   first-load against the now-healthy server.

## Results

**Listeners (step 2):** both wire correctly.
- `online` → `refresh` called: ✅
- `visibilitychange` while tab in `hidden` state → `refresh` NOT called: ✅
  (the listener's `if (document.visibilityState === 'visible')` guard
  fires only when the user is actually looking at the tab)

**Retry sequence against dead server (steps 3-4):**

| Attempt | Time elapsed | Err text shown                | fetch result      |
|---------|-------------:|-------------------------------|-------------------|
| 1       | T+0.001s     | (silent first try)            | TypeError: Failed to fetch |
| 2       | T+3.4s       | `Reconnecting… (2/6)`         | TypeError: Failed to fetch |
| 3       | T+7.4s       | `Reconnecting… (3/6)`         | TypeError: Failed to fetch |
| 4       | T+12.4s      | `Reconnecting… (4/6)`         | TypeError: Failed to fetch |
| 5       | T+19.4s      | `Reconnecting… (5/6)`         | TypeError: Failed to fetch |
| 6       | T+30.4s      | `Reconnecting… (6/6)`         | TypeError: Failed to fetch |
| final   | T+32.8s      | `Error: Failed to fetch — will auto-retry on focus` | — |

Inter-attempt intervals (3.4s, 4s, 5s, 7s, 11s) are ~7× the designed
spacing (0.5s, 1s, 2s, 4s, 8s) because **Chrome throttles `setTimeout` in
hidden tabs to ≥1000ms each**. The automation-driven tab reports
`visibilityState: "hidden"` for the entire run.

In a **foreground tab** — the actual user flow when clicking the bookmark
or focusing the window after wake — `setTimeout` is not throttled and the
schedule runs at its designed 15.5s total budget. The python supervisor
(`run_dashboard.bat`) respawns in 3s and the server is responsive within
~5s, well inside that.

**Failure-state UX:** previous page content was preserved during the
failed retry (root kept its 14 event cards, the last successful `ts` was
kept). The error text appeared above stale data instead of wiping the
page. Final message tells the user recovery is automatic.

**Recovery on reload (step 6):** after reload with the server alive, page
fetched and rendered cleanly in <4s — 14 events, fresh timestamp, no
error.

## Caveats observed during testing

- Chrome's `~1000ms minimum setTimeout` throttling for hidden tabs
  stretches the retry budget when the tab isn't visible. This is the
  desired behavior in production (don't pound the server from a tab the
  user isn't looking at), so no change is warranted.
- `delete window.fetch` does NOT restore the native `fetch` in this
  Chrome — it removes the function entirely. Future test scripts should
  save and re-assign the original reference instead.
