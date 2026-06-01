# Live-path Bias Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `bias_drift` from overclaiming (rename it to a replay-path metric) and add a log-pure `bias_resid_live_*` health metric that actually measures the live NWS-blended model's bias.

**Architecture:** Two changes inside the existing health loop. (A) Rename `bias_drift_<series>` → `bias_drift_replay_<series>` + honest note. (B) New network-free `bias_resid_live_readings` in `lab/health.py` that joins the snapshot log's live μ (nearest-T24) against settled-bucket midpoints, emitting per-city + a pooled early-warning reading. New thresholds live in `wb_thresholds.py` per the backtest-harness contract. Detection only; no refit, no selection-path change.

**Tech Stack:** Python 3.13, pytest. Modules: `wb_thresholds.py`, `lab/health.py`, `lab/live_calibration.py`. Spec: `docs/superpowers/specs/2026-06-01-live-path-bias-monitoring-design.md`.

**Branch:** work continues on the current worktree branch (`worktree-model-notes-live-bias-finding`). Deploy is human-gated — do NOT merge to a deploy branch; hold like the other 2026-06-01 `MODEL_CHANGES.md` entries.

---

## File Structure

- `wb_thresholds.py` — **Modify.** Add 3 constants: `BIAS_RESID_WATCH`, `BIAS_RESID_ALERT`, `BIAS_RESID_POOLED_MIN_N`. Single source of truth.
- `lab/live_calibration.py` — **Modify.** Add pure `bucket_midpoint(subtitle)` parser (owns log-row interpretation).
- `lab/health.py` — **Modify.** Import the 3 constants; add `bias_resid_live_readings`; rename inside `bias_drift_readings`; wire one line into `run_health_scan`.
- `tests/test_thresholds.py` — **Modify.** Pin the 3 new values + assert `lab.health` re-exports them.
- `tests/test_live_calibration.py` — **Modify.** Test `bucket_midpoint`.
- `tests/test_health.py` — **Modify.** Test `bias_resid_live_readings`; update the rename assertions + honesty pin; fix the assembly test's monkeypatch set.

---

## Task 1: Add shared thresholds + wire into health

**Files:**
- Modify: `wb_thresholds.py:33` (after `OPP_EDGE_MIN`)
- Modify: `lab/health.py:21-30` (the `from wb_thresholds import (...)` block)
- Test: `tests/test_thresholds.py`

- [ ] **Step 1: Write the failing pins**

In `tests/test_thresholds.py`, add to `test_canonical_values_pinned` (after the `OPP_EDGE_MIN` line):

```python
    assert T.BIAS_RESID_WATCH == 0.5
    assert T.BIAS_RESID_ALERT == 1.5
    assert T.BIAS_RESID_POOLED_MIN_N == 30
```

And add to `test_lab_health_reexports_shared` (after the `h.OPP_EDGE_MIN` line):

```python
    assert h.BIAS_RESID_WATCH == T.BIAS_RESID_WATCH
    assert h.BIAS_RESID_ALERT == T.BIAS_RESID_ALERT
    assert h.BIAS_RESID_POOLED_MIN_N == T.BIAS_RESID_POOLED_MIN_N
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_thresholds.py -q`
Expected: FAIL — `AttributeError: module 'wb_thresholds' has no attribute 'BIAS_RESID_WATCH'`.

- [ ] **Step 3: Add the constants**

In `wb_thresholds.py`, immediately after the `OPP_EDGE_MIN = 0.05 ...` line:

```python
BIAS_RESID_WATCH = 0.5       # live-path bias residual |mean| <= 0.5degF = OK
BIAS_RESID_ALERT = 1.5       # |mean| > 1.5degF = ALERT (between = WATCH; looser than replay drift — live path is noisier)
BIAS_RESID_POOLED_MIN_N = 30 # pooled live-residual sufficiency gate
```

- [ ] **Step 4: Re-export from health**

In `lab/health.py`, inside the `from wb_thresholds import (` block (ends at `OPP_EDGE_MIN,`), add three lines before the closing `)`:

```python
    BIAS_RESID_WATCH,
    BIAS_RESID_ALERT,
    BIAS_RESID_POOLED_MIN_N,
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_thresholds.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add wb_thresholds.py lab/health.py tests/test_thresholds.py
git commit -m "feat(thresholds): add live-path bias-residual thresholds"
```

---

## Task 2: `bucket_midpoint` subtitle parser

**Files:**
- Modify: `lab/live_calibration.py` (top imports + new function)
- Test: `tests/test_live_calibration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_live_calibration.py`:

```python
def test_bucket_midpoint_interior_open_ended_and_junk():
    assert lc.bucket_midpoint("94° to 95°") == (94.5, "interior")
    assert lc.bucket_midpoint("70° or above") == (71.0, "open")
    assert lc.bucket_midpoint("69° or below") == (68.0, "open")
    assert lc.bucket_midpoint("") == (None, None)
    assert lc.bucket_midpoint(None) == (None, None)
    assert lc.bucket_midpoint("nonsense") == (None, None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_live_calibration.py::test_bucket_midpoint_interior_open_ended_and_junk -q`
Expected: FAIL — `AttributeError: module 'lab.live_calibration' has no attribute 'bucket_midpoint'`.

- [ ] **Step 3: Implement the parser**

In `lab/live_calibration.py`, ensure `import re` is present at the top (add it if not). Then add this function (near the other bucket helpers, e.g. after `_yes_pick`):

```python
def bucket_midpoint(subtitle: str | None) -> tuple[float | None, str | None]:
    """Realized-high midpoint from a settled bucket subtitle.

    "94° to 95°"   -> (94.5, "interior")
    "70° or above" -> (71.0, "open")     # open-ended; midpoint is not a true high
    "69° or below" -> (68.0, "open")
    "" / None / unparseable -> (None, None)
    """
    if not subtitle:
        return None, None
    s = subtitle.lower()
    nums = [int(x) for x in re.findall(r"-?\d+", subtitle)]
    if "to" in s and len(nums) >= 2:
        return (nums[0] + nums[1]) / 2.0, "interior"
    if "above" in s and nums:
        return nums[0] + 1.0, "open"
    if "below" in s and nums:
        return nums[0] - 1.0, "open"
    return None, None
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_live_calibration.py::test_bucket_midpoint_interior_open_ended_and_junk -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lab/live_calibration.py tests/test_live_calibration.py
git commit -m "feat(live_calibration): bucket_midpoint subtitle parser"
```

---

## Task 3: `bias_resid_live_readings` (per-city + pooled)

**Files:**
- Modify: `lab/health.py` (new function; ensure `import statistics` at top)
- Test: `tests/test_health.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_health.py` (note: `bias_resid_live_readings` must also be added to the existing top-of-file import from `lab.health` — add it there):

```python
def test_bias_resid_live_per_city_and_pooled(monkeypatch):
    import lab.health as h
    import kalshi_temp as kt
    monkeypatch.setattr(kt, "BIAS", {"KXHIGHAUS": -1.81, "KXHIGHMIA": -1.52})
    # 35 distinct AUS events: actual 94.5, mu 95.5 -> resid -1.0 each.
    rows = [{
        "event_ticker": f"KXHIGHAUS-26JUN{n:02d}", "lead_hours": 24.0,
        "settled_bucket": "94° to 95°", "model": {"mu": 95.5},
    } for n in range(1, 36)]
    monkeypatch.setattr(h.lc, "read_log", lambda path, since_days=None: rows)
    by = {r.name: r for r in bias_resid_live_readings(days=60, log_path="x")}
    assert by["bias_resid_live_KXHIGHAUS"].value == _approx(-1.0)
    assert by["bias_resid_live_KXHIGHAUS"].n == 35
    assert by["bias_resid_live_KXHIGHAUS"].status == "WATCH"   # 0.5 < 1.0 <= 1.5
    # MIA has no rows -> INSUFFICIENT_DATA, value None
    assert by["bias_resid_live_KXHIGHMIA"].status == "INSUFFICIENT_DATA"
    assert by["bias_resid_live_KXHIGHMIA"].value is None
    # pooled = mean of all interior resids (35 x -1.0), n=35 >= 30 -> WATCH
    assert by["bias_resid_live_pooled"].value == _approx(-1.0)
    assert by["bias_resid_live_pooled"].n == 35
    assert by["bias_resid_live_pooled"].status == "WATCH"
    assert "see per-city" in by["bias_resid_live_pooled"].note


def test_bias_resid_live_excludes_open_ended_and_off_lead(monkeypatch):
    import lab.health as h
    import kalshi_temp as kt
    monkeypatch.setattr(kt, "BIAS", {"KXHIGHLAX": -0.55})
    rows = [
        {"event_ticker": "KXHIGHLAX-26JUN01", "lead_hours": 24.0,
         "settled_bucket": "95° or above", "model": {"mu": 96.0}},   # open-ended -> excluded
        {"event_ticker": "KXHIGHLAX-26JUN02", "lead_hours": 40.0,
         "settled_bucket": "80° to 81°", "model": {"mu": 79.0}},     # |40-24|=16 > 12 -> excluded
    ]
    monkeypatch.setattr(h.lc, "read_log", lambda path, since_days=None: rows)
    by = {r.name: r for r in bias_resid_live_readings(days=60, log_path="x")}
    assert by["bias_resid_live_KXHIGHLAX"].n == 0
    assert by["bias_resid_live_KXHIGHLAX"].status == "INSUFFICIENT_DATA"
    assert by["bias_resid_live_pooled"].n == 0
    assert by["bias_resid_live_pooled"].status == "INSUFFICIENT_DATA"
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_health.py -k bias_resid_live -q`
Expected: FAIL — `ImportError: cannot import name 'bias_resid_live_readings'`.

- [ ] **Step 3: Implement the function**

In `lab/health.py`, ensure `import statistics` is present at the top (add if missing). Add this function right after `bias_drift_readings` (so it sits beside its sibling):

```python
def bias_resid_live_readings(days: int, log_path: str = lc.LOG_PATH) -> list[MetricReading]:
    """Live-path bias residual: realized high (settled-bucket midpoint) minus the
    live NWS-blended mu at the nearest-T24 snapshot, per series + pooled.

    Network-free (reads only the snapshot log). Measures what
    bias_drift_replay_* structurally CANNOT — the live NWS overlay's effect on mu.
    Sign: negative => mu runs hot (over-forecasts).
    """
    import kalshi_temp as kt
    rows = lc.read_log(log_path, since_days=max(days, 60))
    by_series: dict[str, list[float]] = defaultdict(list)
    for ev, rs in lc._by_event(rows).items():
        sb = next((r["settled_bucket"] for r in rs if r.get("settled_bucket")), None)
        actual, kind = lc.bucket_midpoint(sb)
        if actual is None or kind != "interior":
            continue
        preds = [r for r in rs
                 if (r.get("model") or {}).get("mu") is not None
                 and r.get("lead_hours") is not None]
        if not preds:
            continue
        row = min(preds, key=lambda r: abs(r["lead_hours"] - 24.0))
        if abs(row["lead_hours"] - 24.0) > 12.0:
            continue
        by_series[ev.split("-")[0]].append(actual - row["model"]["mu"])

    out: list[MetricReading] = []
    pooled: list[float] = []
    for series in sorted(kt.BIAS):
        resids = by_series.get(series, [])
        pooled.extend(resids)
        n = len(resids)
        mean = round(statistics.mean(resids), 2) if resids else None
        status = (classify_abs(mean, n=n, watch=BIAS_RESID_WATCH, alert=BIAS_RESID_ALERT)
                  if mean is not None else "INSUFFICIENT_DATA")
        out.append(MetricReading(
            name=f"bias_resid_live_{series}", value=mean,
            threshold="|actual − live μ| <= 0.5°F", status=status, n=n,
            note=("live NWS-blended μ vs settled-bucket midpoint @≈T-24; neg ⇒ μ runs hot"
                  if mean is not None else "no settled interior pairs")))

    pn = len(pooled)
    pmean = round(statistics.mean(pooled), 2) if pooled else None
    if pmean is None or pn < BIAS_RESID_POOLED_MIN_N:
        pstatus = "INSUFFICIENT_DATA"
    elif abs(pmean) <= BIAS_RESID_WATCH:
        pstatus = "OK"
    elif abs(pmean) <= BIAS_RESID_ALERT:
        pstatus = "WATCH"
    else:
        pstatus = "ALERT"
    out.append(MetricReading(
        name="bias_resid_live_pooled", value=pmean,
        threshold="|actual − live μ| <= 0.5°F", status=pstatus, n=pn,
        note="early-warning aggregate; NOT a correction — see per-city before acting"))
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_health.py -k bias_resid_live -q`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add lab/health.py tests/test_health.py
git commit -m "feat(health): bias_resid_live_* live-path bias metric"
```

---

## Task 4: Rename the replay metric (honesty)

**Files:**
- Modify: `lab/health.py:178-196` (`bias_drift_readings` — both the no-fresh-fit branch and the main `MetricReading`)
- Test: `tests/test_health.py` (`test_bias_drift_readings_flags_large_delta`)

- [ ] **Step 1: Update the test to expect the honest name + note**

Replace `test_bias_drift_readings_flags_large_delta` body assertions with:

```python
    readings = bias_drift_readings(days=60)
    by = {r.name: r for r in readings}
    assert by["bias_drift_replay_KXHIGHNY"].status == "OK"      # |−0.50−(−0.44)|=0.06
    assert by["bias_drift_replay_KXHIGHDEN"].status == "ALERT"  # |−2.10−(−0.81)|=1.29 > 1.0
    assert "no-NWS replay" in by["bias_drift_replay_KXHIGHNY"].note
```

(Leave the two `monkeypatch.setattr` lines above unchanged.)

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_health.py::test_bias_drift_readings_flags_large_delta -q`
Expected: FAIL — `KeyError: 'bias_drift_replay_KXHIGHNY'` (still emitting `bias_drift_KXHIGHNY`).

- [ ] **Step 3: Rename in both emit sites**

In `lab/health.py` `bias_drift_readings`, the **no-fresh-fit** branch — change:

```python
            out.append(MetricReading(name=f"bias_drift_{series}", value=None,
                                     threshold="|Δ| <= 0.5°F", status="INSUFFICIENT_DATA",
                                     n=0, note="no fresh fit"))
```
to:
```python
            out.append(MetricReading(name=f"bias_drift_replay_{series}", value=None,
                                     threshold="|Δ| <= 0.5°F", status="INSUFFICIENT_DATA",
                                     n=0, note="no-NWS replay refit (excl. live NWS overlay); no fresh fit"))
```

And the **main** reading — change:

```python
        out.append(MetricReading(
            name=f"bias_drift_{series}", value=round(delta, 2),
            threshold="|Δ| <= 0.5°F", status=status, n=n,
            note=f"deployed {deployed:+.2f} fresh {row['bias']:+.2f}"))
```
to:
```python
        out.append(MetricReading(
            name=f"bias_drift_replay_{series}", value=round(delta, 2),
            threshold="|Δ| <= 0.5°F", status=status, n=n,
            note=f"no-NWS replay refit (excl. live NWS overlay); deployed {deployed:+.2f} fresh {row['bias']:+.2f}"))
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_health.py::test_bias_drift_readings_flags_large_delta -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lab/health.py tests/test_health.py
git commit -m "refactor(health): rename bias_drift -> bias_drift_replay (honesty)"
```

---

## Task 5: Wire into `run_health_scan`

**Files:**
- Modify: `lab/health.py:343` (`run_health_scan`, after the `bias_drift_readings` line)
- Test: `tests/test_health.py` (`test_run_health_scan_assembles_report`)

- [ ] **Step 1: Update the assembly test**

In `test_run_health_scan_assembles_report`, after the line
`monkeypatch.setattr(h, "bias_drift_readings", lambda days: [])`
add a sentinel-returning monkeypatch (so the test genuinely fails until the metric
is wired in):

```python
    monkeypatch.setattr(h, "bias_resid_live_readings",
                        lambda days, log_path=None: [
                            MetricReading("bias_resid_live_pooled", -0.5, "t", "OK", 40, "")])
```

And add to the assertions at the end of the test:

```python
    assert "bias_resid_live_pooled" in names
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_health.py::test_run_health_scan_assembles_report -q`
Expected: FAIL — `AssertionError`: `run_health_scan` does not yet append `bias_resid_live_readings`, so `"bias_resid_live_pooled"` is absent from `names`.

- [ ] **Step 3: Add the wiring line**

In `run_health_scan`, immediately after:
```python
    readings += bias_drift_readings(days=max(days, 60))
```
add:
```python
    readings += bias_resid_live_readings(days=max(days, 60), log_path=log_path)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_health.py::test_run_health_scan_assembles_report -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lab/health.py tests/test_health.py
git commit -m "feat(health): wire bias_resid_live into run_health_scan"
```

---

## Task 6: Full-suite + real-scan validation gate

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest -q`
Expected: PASS, no failures. (Confirms no other test pinned the old `bias_drift_<series>` name.)

- [ ] **Step 2: Run the REAL health scan end-to-end (no write)**

This exercises the actual `run_health_scan` against the live log — the path units bypass (the 05-28 crash shipped green because units monkeypatch the scan).

Run: `python -m lab.cli health --days 14 --log live_picks_log.jsonl`
Expected: prints a Model Health report, no traceback. The "All readings" table contains `bias_drift_replay_` rows (NOT `bias_drift_` without `_replay`) AND a `bias_resid_live_pooled` row plus `bias_resid_live_<series>` rows.

- [ ] **Step 3: Confirm the rename + new rows are present**

Run: `python -m lab.cli health --days 14 --log live_picks_log.jsonl | grep -E "bias_drift_replay_|bias_resid_live_pooled"`
Expected: at least one `bias_drift_replay_*` line and one `bias_resid_live_pooled` line. (No `bias_drift_KXHIGH*` line lacking `_replay`.)

- [ ] **Step 4: Confirm no stray doc write**

The command above omits `--write`, so `docs/MODEL_HEALTH.md` must be unchanged.

Run: `git status --short docs/MODEL_HEALTH.md`
Expected: empty output (no modification).

- [ ] **Step 5: Append the change-journal entry**

Add to `docs/MODEL_CHANGES.md` (newest at bottom, per its template):

```markdown
## 2026-06-01 — live-path bias monitoring (fix bias_drift blind spot)
- change: renamed health metric `bias_drift_<series>` -> `bias_drift_replay_<series>`
  (+ note "no-NWS replay refit") to stop it overclaiming live coverage; added
  network-free `bias_resid_live_<series>` + `bias_resid_live_pooled` measuring live
  NWS-blended mu vs settled-bucket midpoints @≈T-24. Detection only. New thresholds
  (BIAS_RESID_WATCH/ALERT/POOLED_MIN_N) in wb_thresholds.
- why: `health._refit_bias` -> `build_historical_inputs` hard-codes nws=None, so
  `bias_drift` was structurally blind to the live 30% NWS overlay; nothing measured
  live-path bias. 19-day read: per-city noise (CI ±2°F), pooled −0.5°F warm tilt.
- validation: full pytest suite green (incl. new threshold/parser/health tests,
  RED-confirmed first); real `lab.cli health` end-to-end emits the renamed metric +
  new readings with no crash.
- deployed-or-held: HELD on lab branch pending human-gated deploy.
- commit: _set on commit_
```

- [ ] **Step 6: Commit**

```bash
git add docs/MODEL_CHANGES.md
git commit -m "docs: MODEL_CHANGES entry for live-path bias monitoring"
```

---

## Self-Review notes (already reconciled)

- **Spec coverage:** Part A rename → Task 4; Part B per-city+pooled → Task 3; constants in wb_thresholds → Task 1; subtitle midpoint (log-pure, interior-only) → Task 2; wiring → Task 5; honesty-pin test → Task 4 Step 1; real-scan validation → Task 6.
- **Open-ended exclusion** enforced in Task 3 (`kind != "interior"`) and tested.
- **±12h lead band** enforced in Task 3 and tested.
- **Pooled gate** uses `BIAS_RESID_POOLED_MIN_N` (inline), not `classify_abs`'s `MIN_N` — intentional (per spec); both equal 30 today but kept independent.
- **Type consistency:** `bucket_midpoint -> (float|None, str|None)`; `bias_resid_live_readings(days, log_path)` signature matches the monkeypatch in Task 5 and the wiring call. `classify_abs(value, *, n, watch, alert)` reused with °F args.
- **No new network:** Task 3 reads only the log; no `_winner_ticker`/market fetch.
