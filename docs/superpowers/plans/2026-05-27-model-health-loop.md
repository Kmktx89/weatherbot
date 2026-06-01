# Model Health Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A daily, deterministic scan that turns the existing lab diagnostics into a git-tracked health report + opportunity scan + change journal, read by a Claude session at start — detection/reporting only, never an actor.

**Architecture:** New `lab/health.py` orchestrates existing diagnostics (`live_calibration`, `refit_bias`, a ported dispersion check) behind pure threshold/render functions; a `lab health --write` CLI command writes `docs/MODEL_HEALTH.md` + stubs `docs/MODEL_CHANGES.md`; a `WeatherbotHealth` scheduled task runs it daily; a repo `CLAUDE.md` pointer makes sessions read it. Pure logic is unit-tested; the loop only ever writes the two docs.

**Tech Stack:** Python 3.13, pytest. Reuses `lab/` + `kalshi_temp`. No new deps.

**Working directory:** All paths relative to the worktree root `C:\Users\KrisKnecht\weatherbot\.claude\worktrees\health-loop`. Run every `pytest`/`git` from there (shell starts at `C:\Users\KrisKnecht`), e.g. `cd /c/Users/KrisKnecht/weatherbot/.claude/worktrees/health-loop && pytest ...`.

**Spec:** `docs/superpowers/specs/2026-05-27-model-health-loop-design.md`

**Reused APIs (already exist — do not reimplement):**
- `lab.live_calibration.read_log(path, since_days) -> list[dict]`
- `lab.live_calibration.build_records(rows, target_lead=24.0, cache) -> (records, skips, leads)`
- `lab.live_calibration.calibrate_live(records, side) -> CalibrationReport` with fields `.n_bets, .mean_pred, .realized_rate, .brier_score`
- `lab.refit_bias.refit(event_tickers, cfg) -> {series: {"bias":float,"n":int,"sd":float}}`
- `lab.inputs.build_historical_inputs(event_ticker, cache)`, `lab.inputs.winner_of(markets)`
- `lab.refit_bias.actual_high_midpoint(winner) -> float|None`
- `lab.configs.LIVE_TODAY`; `model.compute(inputs, cfg)`
- `kalshi_temp.BIAS` (deployed per-series bias dict); `kalshi_temp.CITIES`; `kalshi_temp.list_events_for_series(series, days)`; `kalshi_temp.haircut_for("no", printed_prob) -> float` (deployed NO haircut)

---

### Task 1: `lab/health.py` scaffolding — dataclasses, thresholds, classifiers

**Files:**
- Create: `lab/health.py`
- Test: `tests/test_health.py` (create)

- [ ] **Step 1: Write failing tests**

Create `tests/test_health.py`:

```python
from lab.health import (
    MetricReading, Opportunity, HealthReport,
    MIN_N, classify_abs, classify_k,
)


def test_classify_abs_insufficient_data_below_min_n():
    assert classify_abs(99.0, n=MIN_N - 1) == "INSUFFICIENT_DATA"


def test_classify_abs_bands():
    assert classify_abs(3.0, n=50) == "OK"        # |3| <= 5
    assert classify_abs(7.0, n=50) == "WATCH"     # 5 < |7| <= 10
    assert classify_abs(-12.0, n=50) == "ALERT"   # |−12| > 10
    assert classify_abs(5.0, n=50) == "OK"        # boundary inclusive
    assert classify_abs(10.0, n=50) == "WATCH"    # boundary inclusive


def test_classify_k_bands():
    assert classify_k(1.0, n=50) == "OK"          # within [0.8, 1.25]
    assert classify_k(0.7, n=50) == "WATCH"       # [0.65, 0.8)
    assert classify_k(1.3, n=50) == "WATCH"       # (1.25, 1.4]
    assert classify_k(0.46, n=50) == "ALERT"      # < 0.65 (current LAX)
    assert classify_k(1.5, n=50) == "ALERT"       # > 1.4
    assert classify_k(1.0, n=MIN_N - 1) == "INSUFFICIENT_DATA"


def test_dataclasses_construct():
    r = MetricReading(name="x", value=1.0, threshold="|gap|<=5pp", status="OK", n=40, note="")
    o = Opportunity(kind="yes_edge", scope="KXHIGHLAX", value=0.07, n=40, note="")
    h = HealthReport(generated_at="t", days=14, readings=[r], opportunities=[o],
                     deployed_model_changed=False)
    assert h.readings[0].status == "OK" and h.opportunities[0].scope == "KXHIGHLAX"
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'lab.health'`.

- [ ] **Step 3: Implement**

Create `lab/health.py`:

```python
"""Model health loop: deterministic daily scan over the lab diagnostics.

Detection/reporting ONLY. Reads data and writes exactly two files
(docs/MODEL_HEALTH.md, docs/MODEL_CHANGES.md). Never edits model code/config/
calibration and never trades. See spec
docs/superpowers/specs/2026-05-27-model-health-loop-design.md.
"""
from dataclasses import dataclass, field

# Thresholds (initial, tunable). MIN_N encodes the n~15 lesson: below it,
# a metric reports INSUFFICIENT_DATA rather than flagging.
MIN_N = 30
CAL_WATCH_PP = 5.0
CAL_ALERT_PP = 10.0
K_OK = (0.8, 1.25)
K_WATCH = (0.65, 1.4)
BIAS_WATCH = 0.5
BIAS_ALERT = 1.0
OPP_EDGE_MIN = 0.05


@dataclass
class MetricReading:
    name: str
    value: float | None
    threshold: str
    status: str          # OK | WATCH | ALERT | INSUFFICIENT_DATA
    n: int
    note: str = ""


@dataclass
class Opportunity:
    kind: str
    scope: str
    value: float
    n: int
    note: str = ""


@dataclass
class HealthReport:
    generated_at: str
    days: int
    readings: list[MetricReading] = field(default_factory=list)
    opportunities: list[Opportunity] = field(default_factory=list)
    deployed_model_changed: bool = False


def classify_abs(value_pp: float, *, n: int,
                 watch: float = CAL_WATCH_PP, alert: float = CAL_ALERT_PP) -> str:
    """Status for a signed pp value judged on its magnitude."""
    if n < MIN_N:
        return "INSUFFICIENT_DATA"
    a = abs(value_pp)
    if a <= watch:
        return "OK"
    if a <= alert:
        return "WATCH"
    return "ALERT"


def classify_k(k: float, *, n: int) -> str:
    """Status for a dispersion k (1.0 = calibrated)."""
    if n < MIN_N:
        return "INSUFFICIENT_DATA"
    if K_OK[0] <= k <= K_OK[1]:
        return "OK"
    if K_WATCH[0] <= k <= K_WATCH[1]:
        return "WATCH"
    return "ALERT"
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_health.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add lab/health.py tests/test_health.py
git commit -m "health: scaffolding — dataclasses, thresholds, classifiers"
```
(End commit body with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.)

---

### Task 2: dispersion diagnostic (`dispersion_k` pure helper + `dispersion_by_city`)

**Files:**
- Modify: `lab/health.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_health.py`:

```python
import math
from lab.health import dispersion_k


def test_dispersion_k_calibrated_pairs_near_one():
    # err == sigma for every obs -> z=1 each, var(z)=0... use spread of z.
    # Build pairs (err, sigma) with z values having known variance.
    # z = [-1, 1] -> var 1.0; Q tiny if sigma large. Use sigma=10 so Q≈(1/3)/100.
    pairs = [(-10.0, 10.0), (10.0, 10.0)]  # z = [-1, 1], pvar(z)=1.0
    k = dispersion_k(pairs)
    # Q = (1/3)*mean(1/sigma^2) = (1/3)*(1/100) = 0.00333; k=sqrt(1-0.00333)
    assert k == math.sqrt(1.0 - (1.0 / 3.0) * (1.0 / 100.0))


def test_dispersion_k_overdispersed_below_one():
    # tiny errors vs large sigma -> z near 0 -> k < 1 (model too wide)
    pairs = [(-0.1, 5.0), (0.1, 5.0)]   # z=[-0.02,0.02], pvar≈0.0004
    assert dispersion_k(pairs) < 0.5


def test_dispersion_k_too_few_returns_none():
    assert dispersion_k([(1.0, 1.0)]) is None
    assert dispersion_k([]) is None
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_health.py -k dispersion_k -v`
Expected: FAIL — `ImportError: cannot import name 'dispersion_k'`.

- [ ] **Step 3: Implement** — append to `lab/health.py`:

```python
import math
import statistics


def dispersion_k(pairs: list[tuple[float, float]]) -> float | None:
    """Quantization-adjusted dispersion multiplier from (err, sigma) pairs.

    z = err/sigma; k = sqrt(max(pvar(z) - Q, 0)) with quantization term
    Q = (1/3)*mean(1/sigma^2) (interior 2°F bucket midpoint noise). Needs >= 2
    pairs. k~1 calibrated, k<1 over-dispersed, k>1 under-dispersed.
    """
    if len(pairs) < 2:
        return None
    z = [e / s for e, s in pairs]
    var_z = statistics.pvariance(z)
    q = (1.0 / 3.0) * statistics.mean(1.0 / (s * s) for _, s in pairs)
    return math.sqrt(max(var_z - q, 0.0))


def dispersion_by_city(days: int, cache=None) -> dict[str, tuple[float | None, int]]:
    """Per-series interior-bucket dispersion k over settled events.

    Returns {series: (k, n_interior_events)}. Reuses build_historical_inputs +
    compute(LIVE_TODAY); restricts to strike_type=="between" winners for the
    clean quantization cut. No network beyond the cached diagnostics.
    """
    import kalshi_temp as kt
    from lab.configs import LIVE_TODAY
    from lab.inputs import build_historical_inputs, winner_of
    from lab.refit_bias import actual_high_midpoint
    from model import compute

    out: dict[str, tuple[float | None, int]] = {}
    for series in kt.CITIES:
        pairs: list[tuple[float, float]] = []
        for e in kt.list_events_for_series(series, days):
            inp = build_historical_inputs(e["event_ticker"], cache=cache)
            if inp is None:
                continue
            w = winner_of(list(inp.markets))
            if w is None or w.get("strike_type") != "between":
                continue
            actual = actual_high_midpoint(w)
            if actual is None:
                continue
            res = compute(inp, LIVE_TODAY)
            if res.mu is None or res.sigma is None:
                continue
            pairs.append((actual - res.mu, res.sigma))
        out[series] = (dispersion_k(pairs), len(pairs))
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_health.py -k dispersion_k -v`
Expected: PASS (3 tests). (`dispersion_by_city` is exercised by the smoke test in Task 7 — it needs network/cache, so no unit test here.)

- [ ] **Step 5: Commit**

```bash
git add lab/health.py tests/test_health.py
git commit -m "health: dispersion_k pure helper + dispersion_by_city diagnostic"
```

---

### Task 3: calibration + bias-drift readings

**Files:**
- Modify: `lab/health.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_health.py`:

```python
from dataclasses import dataclass as _dc
from lab.health import calibration_readings, bias_drift_readings


@_dc
class _FakeRep:   # mimics lab.calibration.CalibrationReport
    n_bets: int
    mean_pred: float
    realized_rate: float
    brier_score: float = 0.2


def test_calibration_readings_yes_gap_and_no_net_of_haircut(monkeypatch):
    import lab.health as h
    # YES: pred 45%, realized 43% -> gap -2pp -> OK
    # NO: pred 88%, realized 70% -> raw gap -18pp, but net of 0.11 haircut
    #     residual = (0.88-0.70) - 0.11 = 0.07 -> 7pp -> WATCH
    monkeypatch.setattr(h, "_calibrate_side",
                        lambda recs, side: _FakeRep(40, 0.45, 0.43) if side == "yes"
                        else _FakeRep(40, 0.88, 0.70))
    readings = calibration_readings(records=[], deployed_no_haircut=0.11)
    by = {r.name: r for r in readings}
    assert by["calibration_yes"].status == "OK"
    assert by["calibration_yes"].value == _approx(-2.0)
    assert by["calibration_no_net_haircut"].status == "WATCH"
    assert by["calibration_no_net_haircut"].value == _approx(7.0)


def test_bias_drift_readings_flags_large_delta(monkeypatch):
    import lab.health as h
    import kalshi_temp as kt
    monkeypatch.setattr(kt, "BIAS", {"KXHIGHNY": -0.44, "KXHIGHDEN": -0.81})
    monkeypatch.setattr(h, "_refit_bias",
                        lambda days: {"KXHIGHNY": {"bias": -0.50, "n": 60, "sd": 1.4},
                                      "KXHIGHDEN": {"bias": -2.10, "n": 60, "sd": 2.0}})
    readings = bias_drift_readings(days=60)
    by = {r.name: r for r in readings}
    assert by["bias_drift_KXHIGHNY"].status == "OK"      # |−0.50−(−0.44)|=0.06
    assert by["bias_drift_KXHIGHDEN"].status == "ALERT"  # |−2.10−(−0.81)|=1.29 > 1.0


def _approx(v):
    from pytest import approx
    return approx(v, abs=1e-9)
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_health.py -k "calibration_readings or bias_drift" -v`
Expected: FAIL — names not defined.

- [ ] **Step 3: Implement** — append to `lab/health.py`:

```python
def _calibrate_side(records, side):
    """Indirection over live_calibration.calibrate_live (monkeypatchable)."""
    from lab.live_calibration import calibrate_live
    return calibrate_live(records, side)


def _refit_bias(days):
    """Indirection over refit_bias.refit on LIVE_TODAY (monkeypatchable)."""
    import kalshi_temp as kt
    from lab.configs import LIVE_TODAY
    from lab.refit_bias import refit
    events: list[str] = []
    for s in kt.CITIES:
        events.extend(e["event_ticker"] for e in kt.list_events_for_series(s, days))
    return refit(events, LIVE_TODAY)


def calibration_readings(records, deployed_no_haircut: float) -> list[MetricReading]:
    """YES gap (realized−pred) and NO gap scored NET of the deployed haircut."""
    out: list[MetricReading] = []
    y = _calibrate_side(records, "yes")
    y_gap = (y.realized_rate - y.mean_pred) * 100.0
    out.append(MetricReading(
        name="calibration_yes", value=round(y_gap, 1), threshold="|gap| <= 5pp",
        status=classify_abs(y_gap, n=y.n_bets), n=y.n_bets,
        note=f"pred {y.mean_pred*100:.1f}% realized {y.realized_rate*100:.1f}%"))
    n = _calibrate_side(records, "no")
    # raw overconfidence = pred − realized; residual after the deployed haircut
    no_resid = ((n.mean_pred - n.realized_rate) - deployed_no_haircut) * 100.0
    out.append(MetricReading(
        name="calibration_no_net_haircut", value=round(no_resid, 1),
        threshold="|residual after haircut| <= 5pp",
        status=classify_abs(no_resid, n=n.n_bets), n=n.n_bets,
        note=(f"NO known structural offset; pred {n.mean_pred*100:.1f}% "
              f"realized {n.realized_rate*100:.1f}% net of {deployed_no_haircut:.2f} haircut")))
    return out


def bias_drift_readings(days: int) -> list[MetricReading]:
    """Per-series |freshly-fit bias − deployed BIAS|."""
    import kalshi_temp as kt
    fresh = _refit_bias(days)
    out: list[MetricReading] = []
    for series, deployed in kt.BIAS.items():
        row = fresh.get(series)
        if row is None:
            out.append(MetricReading(name=f"bias_drift_{series}", value=None,
                                     threshold="|Δ| <= 0.5°F", status="INSUFFICIENT_DATA",
                                     n=0, note="no fresh fit"))
            continue
        delta = row["bias"] - deployed
        n = row["n"]
        if n < MIN_N:
            status = "INSUFFICIENT_DATA"
        elif abs(delta) <= BIAS_WATCH:
            status = "OK"
        elif abs(delta) <= BIAS_ALERT:
            status = "WATCH"
        else:
            status = "ALERT"
        out.append(MetricReading(
            name=f"bias_drift_{series}", value=round(delta, 2),
            threshold="|Δ| <= 0.5°F", status=status, n=n,
            note=f"deployed {deployed:+.2f} fresh {row['bias']:+.2f}"))
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_health.py -k "calibration_readings or bias_drift" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add lab/health.py tests/test_health.py
git commit -m "health: calibration (NO net-of-haircut) + bias-drift readings"
```

---

### Task 4: `scan_opportunities`

**Files:**
- Modify: `lab/health.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_health.py`:

```python
from lab.health import scan_opportunities


def test_scan_opportunities_flags_unexploited_yes_edge():
    # Per-series YES records: realized win-rate minus mean implied prob.
    # Build records where realized rate (won) exceeds the market-implied prob
    # by >= OPP_EDGE_MIN over >= MIN_N events, on picks that sat below the
    # selection threshold (taken=False).
    recs = [{"series": "KXHIGHLAX", "implied": 0.40, "won": 1, "taken": False}
            for _ in range(20)] + \
           [{"series": "KXHIGHLAX", "implied": 0.40, "won": 0, "taken": False}
            for _ in range(15)]   # 20/35 = 57.1% realized vs 40% implied = +17.1pp, n=35
    opps = scan_opportunities(recs)
    lax = [o for o in opps if o.scope == "KXHIGHLAX"]
    assert lax and lax[0].kind == "unexploited_yes_edge"
    assert lax[0].value == _approx(round((20/35) - 0.40, 4))
    assert lax[0].n == 35


def test_scan_opportunities_silent_when_thin_or_no_edge():
    # below MIN_N
    assert scan_opportunities([{"series": "KXHIGHNY", "implied": 0.4, "won": 1,
                                "taken": False}] * 10) == []
    # enough n but no positive edge (realized == implied)
    recs = [{"series": "KXHIGHNY", "implied": 0.5, "won": 1, "taken": False}
            for _ in range(20)] + \
           [{"series": "KXHIGHNY", "implied": 0.5, "won": 0, "taken": False}
            for _ in range(20)]   # 50% realized vs 50% implied = 0 edge
    assert scan_opportunities(recs) == []
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_health.py -k scan_opportunities -v`
Expected: FAIL — `cannot import name 'scan_opportunities'`.

- [ ] **Step 3: Implement** — append to `lab/health.py`:

```python
from collections import defaultdict


def scan_opportunities(records: list[dict]) -> list[Opportunity]:
    """Flag per-series unexploited YES edge.

    `records`: dicts with series, implied (market prob on the pick), won (0/1),
    taken (bool — did the bot actually take it). An opportunity = over >= MIN_N
    UNTAKEN picks in a series, realized win-rate exceeds mean implied prob by
    >= OPP_EDGE_MIN. Deterministic; no network.
    """
    by_series: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if not r.get("taken", False):
            by_series[r["series"]].append(r)
    out: list[Opportunity] = []
    for series, rs in by_series.items():
        n = len(rs)
        if n < MIN_N:
            continue
        realized = sum(r["won"] for r in rs) / n
        implied = sum(r["implied"] for r in rs) / n
        edge = realized - implied
        if edge >= OPP_EDGE_MIN:
            out.append(Opportunity(
                kind="unexploited_yes_edge", scope=series,
                value=round(edge, 4), n=n,
                note=(f"untaken YES picks won {realized*100:.1f}% vs implied "
                      f"{implied*100:.1f}% (+{edge*100:.1f}pp)")))
    return out
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_health.py -k scan_opportunities -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add lab/health.py tests/test_health.py
git commit -m "health: scan_opportunities (unexploited per-series YES edge)"
```

---

### Task 5: `render_report`

**Files:**
- Modify: `lab/health.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_health.py`:

```python
from lab.health import render_report


def test_render_report_has_sections_and_flags():
    rep = HealthReport(
        generated_at="2026-05-27T13:00:00Z", days=14,
        readings=[
            MetricReading("calibration_yes", -2.0, "|gap|<=5pp", "OK", 56, "pred 45%"),
            MetricReading("dispersion_k_KXHIGHLAX", 0.46, "k in [0.8,1.25]", "ALERT", 53,
                          "over-dispersed"),
        ],
        opportunities=[Opportunity("unexploited_yes_edge", "KXHIGHLAX", 0.07, 40,
                                   "won 47% vs 40%")],
        deployed_model_changed=True)
    md = render_report(rep)
    assert "# Model Health" in md
    assert "2026-05-27T13:00:00Z" in md
    assert "ALERT" in md and "dispersion_k_KXHIGHLAX" in md
    assert "unexploited_yes_edge" in md and "KXHIGHLAX" in md
    # deployed-change banner present when flagged
    assert "deployed model changed" in md.lower()
    # ALERT rows surfaced in a summary line near the top
    assert md.index("ALERT") < md.index("## All readings")
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_health.py -k render_report -v`
Expected: FAIL — `cannot import name 'render_report'`.

- [ ] **Step 3: Implement** — append to `lab/health.py`:

```python
def render_report(report: HealthReport) -> str:
    lines: list[str] = []
    lines.append("# Model Health")
    lines.append("")
    lines.append(f"_Generated {report.generated_at} · window {report.days}d · "
                 "auto-regenerated daily; do not hand-edit (see MODEL_CHANGES.md "
                 "for the change journal)._")
    lines.append("")
    if report.deployed_model_changed:
        lines.append("> ⚠️ **Deployed model changed since last run** — add a "
                     "`docs/MODEL_CHANGES.md` entry (what/why/validation/deployed-or-held).")
        lines.append("")
    flagged = [r for r in report.readings if r.status in ("ALERT", "WATCH")]
    lines.append("## Flags")
    if flagged:
        for r in sorted(flagged, key=lambda x: 0 if x.status == "ALERT" else 1):
            lines.append(f"- **{r.status}** `{r.name}` = {r.value} "
                         f"({r.threshold}; n={r.n}) — {r.note}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Opportunities")
    if report.opportunities:
        for o in report.opportunities:
            lines.append(f"- `{o.kind}` **{o.scope}** {o.value:+} (n={o.n}) — {o.note}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## All readings")
    lines.append("")
    lines.append("| metric | value | status | n | threshold | note |")
    lines.append("|---|---|---|---|---|---|")
    for r in report.readings:
        lines.append(f"| {r.name} | {r.value} | {r.status} | {r.n} | "
                     f"{r.threshold} | {r.note} |")
    lines.append("")
    return "\n".join(lines)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_health.py -k render_report -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lab/health.py tests/test_health.py
git commit -m "health: render_report (flags-first markdown)"
```

---

### Task 6: `detect_deployed_change`

**Files:**
- Modify: `lab/health.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write failing test**

Append to `tests/test_health.py`:

```python
from lab.health import detect_deployed_change


def test_detect_deployed_change_first_run_then_stable(tmp_path):
    marker = tmp_path / "marker.txt"
    # first run: no marker -> treated as changed, marker written
    assert detect_deployed_change(str(marker), fingerprint="abc") is True
    assert marker.read_text().strip() == "abc"
    # same fingerprint -> not changed
    assert detect_deployed_change(str(marker), fingerprint="abc") is False
    # new fingerprint -> changed, marker updated
    assert detect_deployed_change(str(marker), fingerprint="def") is True
    assert marker.read_text().strip() == "def"
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_health.py -k detect_deployed_change -v`
Expected: FAIL — name not defined.

- [ ] **Step 3: Implement** — append to `lab/health.py`:

```python
from pathlib import Path


def model_fingerprint() -> str:
    """Fingerprint of the deployed model: git HEAD of model/ + hash of
    calibration_params.json (if present)."""
    import hashlib
    import subprocess
    try:
        head = subprocess.check_output(
            ["git", "log", "-1", "--format=%H", "--", "model", "kalshi_temp.py"],
            text=True).strip()
    except Exception:
        head = "nogit"
    cp = Path("calibration_params.json")
    cp_hash = hashlib.sha256(cp.read_bytes()).hexdigest()[:12] if cp.exists() else "none"
    return f"{head[:12]}:{cp_hash}"


def detect_deployed_change(marker_path: str, *, fingerprint: str | None = None) -> bool:
    """True if the deployed-model fingerprint changed since the last run.

    Writes the new fingerprint to `marker_path`. First run (no marker) counts
    as changed. `fingerprint` is injectable for testing; defaults to
    model_fingerprint()."""
    fp = fingerprint if fingerprint is not None else model_fingerprint()
    p = Path(marker_path)
    prev = p.read_text(encoding="utf-8").strip() if p.exists() else None
    if prev != fp:
        p.write_text(fp, encoding="utf-8")
        return True
    return False
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_health.py -k detect_deployed_change -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add lab/health.py tests/test_health.py
git commit -m "health: detect_deployed_change (fingerprint marker)"
```

---

### Task 7: `run_health_scan` + `lab health` CLI + `--write` + guardrail test

**Files:**
- Modify: `lab/health.py`, `lab/cli.py`
- Test: `tests/test_health.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_health.py`:

```python
from lab.health import run_health_scan, write_report, HEALTH_PATH, CHANGES_PATH


def test_run_health_scan_assembles_report(monkeypatch):
    import lab.health as h
    monkeypatch.setattr(h.lc, "read_log", lambda path, since_days=None: [{"x": 1}])
    monkeypatch.setattr(h.lc, "build_records", lambda rows, target_lead=24.0, cache=None: ([], {}, []))
    monkeypatch.setattr(h, "calibration_readings",
                        lambda records, deployed_no_haircut: [
                            MetricReading("calibration_yes", -2.0, "t", "OK", 40, "")])
    monkeypatch.setattr(h, "bias_drift_readings", lambda days: [])
    monkeypatch.setattr(h, "dispersion_by_city",
                        lambda days, cache=None: {"KXHIGHLAX": (0.46, 53)})
    monkeypatch.setattr(h, "_opportunity_records", lambda rows, cache=None: [])
    monkeypatch.setattr(h, "detect_deployed_change", lambda marker, **k: False)
    rep = run_health_scan(days=14, log_path="x.jsonl")
    names = {r.name for r in rep.readings}
    assert "calibration_yes" in names and "dispersion_k_KXHIGHLAX" in names
    assert rep.days == 14


def test_write_report_only_touches_the_two_docs(tmp_path, monkeypatch):
    # Guardrail: write_report must write ONLY HEALTH_PATH (+ stub CHANGES_PATH).
    rep = HealthReport(generated_at="t", days=14, deployed_model_changed=False)
    health = tmp_path / "MODEL_HEALTH.md"
    changes = tmp_path / "MODEL_CHANGES.md"
    write_report(rep, health_path=str(health), changes_path=str(changes))
    assert health.exists()
    # no change stub appended when deployed_model_changed is False and file absent
    # (write_report creates CHANGES only to append a stub when flagged)
    assert "# Model Health" in health.read_text()
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/test_health.py -k "run_health_scan or write_report" -v`
Expected: FAIL — names not defined.

- [ ] **Step 3: Implement** — append to `lab/health.py`:

```python
from datetime import datetime, timezone

import lab.live_calibration as lc

HEALTH_PATH = "docs/MODEL_HEALTH.md"
CHANGES_PATH = "docs/MODEL_CHANGES.md"
MARKER_PATH = "docs/.health_marker"


def _opportunity_records(rows, cache=None) -> list[dict]:
    """Build scan_opportunities input from log rows. Each settled event's
    nearest-T24 pred row yields the YES pick's implied prob, won, and whether
    it was taken (cal_ev_yes >= MIN_BEST_EV). Reuses live_calibration helpers."""
    import kalshi_temp as kt
    recs, _skips, _leads = lc.build_records(rows, cache=cache)
    # build_records returns LiveCalRecord (no implied/taken); for the opportunity
    # scan we need per-pick market context, so re-walk the nearest-T24 rows:
    out: list[dict] = []
    by_ev = lc._by_event(rows)
    for ev, rs in by_ev.items():
        wt = lc._winner_ticker(rs, ev, cache)
        if wt is None:
            continue
        preds = [r for r in rs if lc._has_pred(r) and r.get("lead_hours") is not None]
        if not preds:
            continue
        row = min(preds, key=lambda r: abs(r["lead_hours"] - 24.0))
        pick = lc._yes_pick(row.get("buckets", []))
        if pick is None:
            continue
        implied = pick.get("yes_ask")
        if implied is None:
            continue
        taken = (pick.get("cal_ev_yes") or pick.get("ev_yes") or 0.0) >= kt.MIN_BEST_EV
        out.append({"series": ev.split("-")[0],
                    "implied": implied,
                    "won": 1 if pick["ticker"] == wt else 0,
                    "taken": bool(taken)})
    return out


def run_health_scan(days: int = 14, log_path: str = lc.LOG_PATH, cache=None) -> HealthReport:
    import kalshi_temp as kt
    rows = lc.read_log(log_path, since_days=days)
    records, _skips, _leads = lc.build_records(rows, cache=cache)
    readings: list[MetricReading] = []
    readings += calibration_readings(records, deployed_no_haircut=kt.haircut_for("no", 0.85))
    readings += bias_drift_readings(days=max(days, 60))
    for series, (k, n) in dispersion_by_city(days=max(days, 60), cache=cache).items():
        readings.append(MetricReading(
            name=f"dispersion_k_{series}",
            value=(round(k, 2) if k is not None else None),
            threshold="k in [0.8, 1.25]",
            status=(classify_k(k, n=n) if k is not None else "INSUFFICIENT_DATA"),
            n=n, note="interior-bucket dispersion (1.0 = calibrated)"))
    opportunities = scan_opportunities(_opportunity_records(rows, cache=cache))
    changed = detect_deployed_change(MARKER_PATH)
    return HealthReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        days=days, readings=readings, opportunities=opportunities,
        deployed_model_changed=changed)


def write_report(report: HealthReport, *, health_path: str = HEALTH_PATH,
                 changes_path: str = CHANGES_PATH) -> None:
    """Write the health report; append a change-journal stub iff the deployed
    model changed. These are the ONLY files this module ever writes."""
    Path(health_path).write_text(render_report(report), encoding="utf-8")
    if report.deployed_model_changed:
        stub = (f"\n## {report.generated_at[:10]} — deployed model changed (stub)\n"
                "- change: _fill in_\n- why: _fill in_\n- validation: _fill in_\n"
                "- deployed-or-held: _fill in_\n- commit: _fill in_\n")
        with open(changes_path, "a", encoding="utf-8") as f:
            f.write(stub)
```

Then add the CLI subcommand. In `lab/cli.py`, add a handler and parser:

```python
def cmd_health(args):
    from .health import run_health_scan, write_report, render_report
    report = run_health_scan(days=args.days, log_path=args.log)
    if args.write:
        write_report(report)
        print(f"wrote {report.generated_at}: "
              f"{sum(1 for r in report.readings if r.status=='ALERT')} ALERT, "
              f"{sum(1 for r in report.readings if r.status=='WATCH')} WATCH, "
              f"{len(report.opportunities)} opportunities")
    else:
        print(render_report(report))
```

And in `build_parser()` (next to the other `sub.add_parser` calls):

```python
    hp = sub.add_parser("health", help="daily model health scan -> docs/MODEL_HEALTH.md")
    hp.add_argument("--days", type=int, default=14)
    hp.add_argument("--log", default="live_picks_log.jsonl")
    hp.add_argument("--write", action="store_true",
                    help="overwrite docs/MODEL_HEALTH.md (+ change-journal stub)")
    hp.set_defaults(func=cmd_health)
```

- [ ] **Step 4: Run to verify pass**

Run: `pytest tests/test_health.py -v` then full `pytest -q`.
Expected: all PASS.

- [ ] **Step 5: Smoke-test the CLI end-to-end (real, no --write)**

Run: `cd /c/Users/KrisKnecht/weatherbot/.claude/worktrees/health-loop && cp /c/Users/KrisKnecht/weatherbot/lab/.cache.sqlite lab/.cache.sqlite 2>/dev/null; cp /c/Users/KrisKnecht/weatherbot/live_picks_log.jsonl . 2>/dev/null; python -m lab.cli health --days 14`
Expected: a markdown report prints to stdout (LAX dispersion likely ALERT at k≈0.46). If it errors, fix before committing.

- [ ] **Step 6: Commit**

```bash
git add lab/health.py lab/cli.py tests/test_health.py
git commit -m "health: run_health_scan + lab health CLI + write_report guardrail"
```

---

### Task 8: artifacts — scheduled task, repo CLAUDE.md pointer, change-journal seed

**Files:**
- Create: `run_health.bat`, `docs/MODEL_CHANGES.md`, `CLAUDE.md`
- Modify: none

- [ ] **Step 1: Create `docs/MODEL_CHANGES.md`** (the journal seed + template):

```markdown
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

## 2026-05-27 — weighted-source σ
- change: `_sigma_from` now weights the source spread by μ's effective weights instead of an unweighted pstdev.
- why: σ over-weighted sources μ suppresses (LAX ECMWF +6°F at weight 0.10) → LAX ~4× over-dispersed.
- validation: dispersion re-run (LAX interior k 0.36→0.46, σ 3.83→2.43; DEN control exact; pooled 0.83→0.85); μ unchanged (baselines regenerated); full suite green; opus final review.
- deployed-or-held: deployed; live LAX σ=1.236 confirmed = weighted formula.
- commit: c60d193 (merged to model-lab a06d3d0)
```

- [ ] **Step 2: Create `run_health.bat`** (mirrors `run_dashboard.bat`):

```bat
@echo off
REM Daily model-health scan. Started by the WeatherbotHealth scheduled task.
REM Writes docs/MODEL_HEALTH.md, stubs docs/MODEL_CHANGES.md on deploy change,
REM and commits ONLY those two docs (never model code).
cd /d "C:\Users\KrisKnecht\weatherbot"
"C:\Users\KrisKnecht\AppData\Local\Programs\Python\Python313\python.exe" -m lab.cli health --days 14 --write >> health.log 2>&1
git add docs/MODEL_HEALTH.md docs/MODEL_CHANGES.md docs/.health_marker
git commit -m "health: daily scan %DATE%" >> health.log 2>&1
```

- [ ] **Step 3: Create repo `CLAUDE.md`** (repo-scoped pointer; layered under the user-global one):

```markdown
# weatherbot — repo session guidance

At the start of any weatherbot **model** session, before proposing model changes:
1. Read `docs/MODEL_HEALTH.md` (auto-regenerated daily by the WeatherbotHealth task) — current calibration / dispersion / bias-drift flags + unexploited-edge opportunities.
2. Skim `docs/MODEL_CHANGES.md` — the change journal (what's been tried, what helped, what was held).

The health loop is detection/reporting only; any change still goes through the gated pipeline (TDD → spec-and-code review → validation → human-gated deploy). See `docs/superpowers/specs/2026-05-27-model-health-loop-design.md`.
```

- [ ] **Step 4: Verify the scan writes cleanly with `--write` in the worktree**

Run: `cd /c/Users/KrisKnecht/weatherbot/.claude/worktrees/health-loop && python -m lab.cli health --days 14 --write && head -30 docs/MODEL_HEALTH.md`
Expected: `docs/MODEL_HEALTH.md` is written and shows the report; exit 0.

- [ ] **Step 5: Commit**

```bash
git add run_health.bat docs/MODEL_CHANGES.md CLAUDE.md docs/MODEL_HEALTH.md
git commit -m "health: scheduled-task bat, repo CLAUDE.md pointer, change-journal seed"
```

- [ ] **Step 6: Document the scheduled-task registration (operator runs once — NOT automated by this plan)**

Add to the PR/merge notes (do not execute here): register `WeatherbotHealth` to run `run_health.bat` daily at 13:00 UTC, mirroring `WeatherbotDashboard`. Example (operator runs in an elevated shell):
```powershell
schtasks /Create /TN WeatherbotHealth /TR "C:\Users\KrisKnecht\weatherbot\run_health.bat" /SC DAILY /ST 13:00 /RL LIMITED
```

---

## Self-review

- **Spec coverage:** health report (Tasks 1,3,5,7), opportunity scan (Task 4 + `_opportunity_records` Task 7), change journal + auto-stub (Tasks 6,7,8), dispersion_by_city self-contained (Task 2), thresholds incl. MIN_N guard + NO net-of-haircut (Tasks 1,3), surface = git-tracked docs + repo CLAUDE.md (Task 8), daily scheduled task (Task 8), guardrail "only writes two docs" (Task 7 `write_report` + test). ✓
- **Detection-only guardrail:** `write_report` is the sole writer and touches only the two doc paths; `run_health_scan` calls only read/diagnostic APIs. The Task 7 guardrail test pins it. ✓
- **Placeholders:** none — every step has concrete code/content. The `_fill in_` strings are intentional journal-template content, not plan placeholders.
- **Type consistency:** `MetricReading`/`Opportunity`/`HealthReport` fields and `classify_abs`/`classify_k`/`dispersion_k`/`render_report`/`run_health_scan`/`write_report` signatures are consistent across tasks. `_calibrate_side`/`_refit_bias`/`_opportunity_records`/`detect_deployed_change` indirections are defined where first used and monkeypatched by name in tests.
- **Sequencing:** built on `model-lab` post-σ-merge (rebased), so `dispersion_by_city` reflects the fixed σ.
