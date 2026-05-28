# Weighted-Source σ Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `σ` respect the same per-source weights that `μ` uses, so a source down-weighted in the mean (e.g. LAX ECMWF at 0.10) no longer inflates the dispersion.

**Architecture:** Add a pure `weighted_std` primitive mirroring `weighted_mean`, then rewrite `_sigma_from` to build each source's effective μ-weight `((1−nws_blend)·source_weight for ecmwf/gfs, nws_blend for nws)` and pass them to `weighted_std`. Equal weights reduce exactly to the current `pstdev`, so balanced cities are invariant; weight-skewed cities sharpen.

**Tech Stack:** Python 3.13, pytest. Pure functions in `model/`, no I/O.

**Working directory:** All paths are relative to the worktree root `C:\Users\KrisKnecht\weatherbot\.claude\worktrees\weighted-sigma`. Run every `pytest` and `git` command from there.

**Spec:** `docs/superpowers/specs/2026-05-27-weighted-source-sigma-design.md`

---

### Task 1: `weighted_std` primitive

**Files:**
- Modify: `model/primitives.py` (add `weighted_std` after `weighted_mean`, ~line 37)
- Test: `tests/test_primitives.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_primitives.py`. Note `pytest_approx` is already defined in this file; `weighted_mean` is already imported on line 3 — extend that import to include `weighted_std`.

```python
import statistics
from model.primitives import weighted_std


def test_weighted_std_equal_weights_matches_pstdev():
    # Equal weights must reduce exactly to population stdev (the old behaviour).
    assert weighted_std({"a": 72.0, "b": 70.0}, {"a": 1.0, "b": 1.0}) == \
        pytest_approx(statistics.pstdev([72.0, 70.0]))  # == 1.0


def test_weighted_std_skewed_weights_down_weights_outlier():
    # a=70 wt .1, b=72 wt .9 -> mean 71.8, var .1*(1.8^2)+.9*(.2^2)=.36, std .6
    assert weighted_std({"a": 70.0, "b": 72.0}, {"a": 0.1, "b": 0.9}) == \
        pytest_approx(0.6)


def test_weighted_std_one_missing_renormalises():
    # b missing -> use a(.25),c(.75) renormalised. mean=10*.25+20*.75=17.5
    # var=.25*(7.5^2)+.75*(2.5^2)=14.0625+4.6875=18.75 -> std=sqrt(18.75)
    out = weighted_std({"a": 10.0, "b": None, "c": 20.0},
                       {"a": 0.25, "b": 0.5, "c": 0.75})
    assert out == pytest_approx(18.75 ** 0.5)


def test_weighted_std_single_present_returns_zero():
    # one surviving source -> no spread measurable
    assert weighted_std({"a": 75.0, "b": None}, {"a": 0.4, "b": 0.6}) == 0.0


def test_weighted_std_none_present_returns_none():
    assert weighted_std({"a": None, "b": None}, {"a": 0.4, "b": 0.6}) is None


def test_weighted_std_zero_total_weight_returns_none():
    assert weighted_std({"a": 10.0}, {"a": 0.0}) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_primitives.py -k weighted_std -v`
Expected: FAIL — `ImportError: cannot import name 'weighted_std'`.

- [ ] **Step 3: Write minimal implementation**

In `model/primitives.py`, add directly after `weighted_mean` (after line 36):

```python
def weighted_std(
    values: Mapping[str, float | None],
    weights: Mapping[str, float],
) -> float | None:
    """Population standard deviation of `values`, weighted by `weights`.

    Same key/None/renormalisation semantics as `weighted_mean`: missing
    values are dropped and weights renormalised over survivors. Returns
    None if no key has a non-None value or surviving weights sum to <= 0,
    and 0.0 if only one source survives (no spread is measurable). With
    equal weights this equals statistics.pstdev of the surviving values.
    """
    parts: list[tuple[float, float]] = []
    for key, w in weights.items():
        v = values.get(key)
        if v is None or w <= 0:
            continue
        parts.append((v, w))
    if not parts:
        return None
    total_w = sum(w for _, w in parts)
    if total_w <= 0:
        return None
    mean = sum(v * w for v, w in parts) / total_w
    var = sum(w * (v - mean) ** 2 for v, w in parts) / total_w
    return math.sqrt(var)
```

`math` is already imported at the top of `primitives.py` (line 2).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_primitives.py -k weighted_std -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add model/primitives.py tests/test_primitives.py
git commit -m "Add weighted_std primitive (weighted population stdev, weighted_mean semantics)"
```

---

### Task 2: Rewire `_sigma_from` to weighted spread

**Files:**
- Modify: `model/compute.py:46-50` (`_sigma_from`), import line 16-21, drop unused `import statistics` (line 12)
- Test: `tests/test_compute.py`

- [ ] **Step 1: Write/adjust the tests**

In `tests/test_compute.py`: (a) **replace** the σ assertion in the existing `test_compute_ecmwf_only_with_nws_overlay` (lines ~132-146), and (b) **add** two new tests. Derive expectations from first principles so they pin the math independently of the implementation.

Replace the body of `test_compute_ecmwf_only_with_nws_overlay` with:

```python
def test_compute_ecmwf_only_with_nws_overlay():
    """ECMWF=present, GFS=None, NWS=present, ECMWF weight 0.10.
    mu unchanged; sigma now uses mu's effective weights, so the
    0.10-weight ECMWF barely contributes to spread."""
    inputs = _basic_inputs(forecasts={"ecmwf": 70.0, "gfs": None, "nws": 72.0})
    cfg = _basic_cfg(
        source_weights={"KXHIGHNY": {"ecmwf": 0.10, "gfs": 0.90}},
        nws_blend=0.3,
        sigma_sources=("ecmwf", "gfs", "nws"),
    )
    out = compute(inputs, cfg)
    # mu_raw unchanged: weighted_mean -> 70.0, then 0.7*70 + 0.3*72 = 70.6
    assert out.mu_raw == pytest.approx(70.6, abs=1e-9)
    # weighted sigma: eff weights ecmwf=(1-.3)*.10=.07, nws=.3 (gfs None dropped)
    w_ec, w_nws = 0.7 * 0.10, 0.3
    tw = w_ec + w_nws
    mean_w = (70.0 * w_ec + 72.0 * w_nws) / tw
    var_w = (w_ec * (70.0 - mean_w) ** 2 + w_nws * (72.0 - mean_w) ** 2) / tw
    assert out.sigma == pytest.approx(math.sqrt(2.0 ** 2 + var_w), abs=1e-9)
```

Add these two new tests at the end of the file:

```python
def test_compute_downweighted_outlier_barely_moves_sigma():
    """The fix: a heavily down-weighted, far-off source (LAX-shaped:
    ECMWF +10 off at weight 0.10) must not blow up sigma the way the old
    equal-weight pstdev did."""
    inputs = _basic_inputs(forecasts={"ecmwf": 80.0, "gfs": 70.0, "nws": 70.0})
    cfg = _basic_cfg(
        base_sigma=1.0,
        source_weights={"KXHIGHNY": {"ecmwf": 0.10, "gfs": 0.90}},
        nws_blend=0.3,
        sigma_sources=("ecmwf", "gfs", "nws"),
    )
    out = compute(inputs, cfg)
    # effective weights: ecmwf .07, gfs .63, nws .30 (sum 1.0)
    w = {"ecmwf": 0.07, "gfs": 0.63, "nws": 0.30}
    mean_w = sum(v * w[k] for k, v in {"ecmwf": 80.0, "gfs": 70.0, "nws": 70.0}.items())
    var_w = sum(w[k] * (v - mean_w) ** 2
                for k, v in {"ecmwf": 80.0, "gfs": 70.0, "nws": 70.0}.items())
    expected_sigma = math.sqrt(1.0 ** 2 + var_w)
    assert out.sigma == pytest.approx(expected_sigma, abs=1e-9)
    # And it must be far below the OLD unweighted formula sqrt(1 + pstdev^2):
    import statistics
    old_sigma = math.sqrt(1.0 ** 2 + statistics.pstdev([80.0, 70.0, 70.0]) ** 2)
    assert out.sigma < old_sigma - 1.0   # ~2.74 vs ~4.82


def test_compute_equal_weights_sigma_unchanged():
    """DEN control: equal ECMWF/GFS weights, no NWS -> weighted std == pstdev,
    so sigma is exactly the old value."""
    import statistics
    inputs = _basic_inputs(forecasts={"ecmwf": 72.0, "gfs": 68.0, "nws": None})
    cfg = _basic_cfg(
        base_sigma=1.0,
        source_weights={"KXHIGHNY": {"ecmwf": 0.5, "gfs": 0.5}},
        nws_blend=0.0,
        sigma_sources=("ecmwf", "gfs"),
    )
    out = compute(inputs, cfg)
    old_sigma = math.sqrt(1.0 ** 2 + statistics.pstdev([72.0, 68.0]) ** 2)
    assert out.sigma == pytest.approx(old_sigma, abs=1e-9)  # == sqrt(1+4)
```

- [ ] **Step 2: Run tests to verify the new/changed ones fail**

Run: `pytest tests/test_compute.py -k "ecmwf_only_with_nws or downweighted_outlier or equal_weights_sigma" -v`
Expected: `test_compute_ecmwf_only_with_nws_overlay` and `test_compute_downweighted_outlier_barely_moves_sigma` FAIL (current σ is the unweighted value); `test_compute_equal_weights_sigma_unchanged` PASSES already (equal weights are invariant even today).

- [ ] **Step 3: Rewrite `_sigma_from`**

In `model/compute.py`, replace `_sigma_from` (lines 46-50):

```python
def _sigma_from(inputs: ModelInputs, cfg: ModelConfig) -> float:
    """Spread of the forecast sources, weighted the same way mu weights them.

    Each source's effective weight in mu is (1 - nws_blend) * source_weight
    for ecmwf/gfs and nws_blend for nws. Using those same weights here keeps
    sigma consistent with mu: a source mu distrusts (e.g. LAX ECMWF at 0.10)
    contributes proportionally little to the dispersion instead of the full
    equal-weight share it got before. cfg.sigma_sources stays the eligibility
    allowlist. Equal weights reduce exactly to the prior statistics.pstdev.
    """
    src_w = cfg.source_weights.get(inputs.series, {})
    eff_weights: dict[str, float] = {}
    for name in cfg.sigma_sources:
        w = cfg.nws_blend if name == "nws" else (1.0 - cfg.nws_blend) * src_w.get(name, 0.0)
        if w > 0:
            eff_weights[name] = w
    spread = weighted_std(inputs.forecasts, eff_weights) or 0.0
    return math.sqrt(cfg.base_sigma ** 2 + spread ** 2)
```

Update the import block (lines 16-21) to add `weighted_std`:

```python
from .primitives import (
    apply_today_max,
    bucket_bounds,
    bucket_probability,
    weighted_mean,
    weighted_std,
)
```

Delete the now-unused `import statistics` on line 12 (verify with `grep -n statistics model/compute.py` — it must return nothing after deletion). Keep `import math` (still used).

- [ ] **Step 4: Run the targeted tests, then the whole compute suite**

Run: `pytest tests/test_compute.py -v`
Expected: all PASS, including the three from Step 1.

- [ ] **Step 5: Commit**

```bash
git add model/compute.py tests/test_compute.py
git commit -m "Weight sigma by mu's effective source weights (fixes LAX over-dispersion)"
```

---

### Task 3: Regenerate frozen baselines + review the real-data diff

`tests/test_baseline.py` compares `compute(inputs, LIVE_TODAY)` against stored per-city JSON. σ/probs will shift for every weight-skewed city; this regen IS the real-data before/after validation.

**Files:**
- Modify (regenerated): `tests/fixtures/baseline/*.json`

- [ ] **Step 1: Confirm the baselines now fail (the change is real)**

Run: `pytest tests/test_baseline.py -v`
Expected: FAIL on LAX, MIA, AUS, PHIL, NY, CHI (σ/probs moved); DEN essentially unchanged (it may still pass within 1e-4, or move only on the small NWS-weight shift).

- [ ] **Step 2: Regenerate baselines**

The currently-committed fixtures hold the OLD values, so `git diff` after regen is the before/after. Run: `pytest tests/test_baseline.py --update-baseline -v`
Expected: all parametrised cases SKIP with "baseline rewritten".

- [ ] **Step 3: Review the diff — this is the acceptance gate**

Run: `git --no-pager diff tests/fixtures/baseline/`
Acceptance criteria (read the diff, do not just trust it):
- **LAX** `sigma` drops sharply (≈3–4 → ≈1.x) and its `probs` concentrate toward the modal bucket. This is the fix.
- **DEN** `sigma`/`probs` ~unchanged (≤ small NWS-weight shift); `mu`, `mu_raw`, `bias_applied` unchanged for **every** city (μ logic was not touched — if any μ moved, STOP, the change leaked into the mean).
- MIA/AUS/PHIL/NY/CHI `sigma` shrink modestly; no city's σ should *increase*.

If μ moved anywhere, or LAX σ did not drop, halt and re-examine `_sigma_from` before committing.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/baseline/
git commit -m "Regenerate baselines for weighted sigma (LAX sigma ~3.8->~1; mu unchanged)"
```

---

### Task 4: Full suite green + dispersion confirmation

**Files:** none modified (validation only). 

- [ ] **Step 1: Run the entire test suite**

Run: `pytest -q`
Expected: all green. If `tests/test_configs.py`, `tests/test_calibration.py`, `tests/test_t24_card.py`, or `tests/test_live_calibration.py` fail, investigate — they should be unaffected (they don't assert `compute()` σ directly). Do not paper over a failure by editing assertions without understanding it.

- [ ] **Step 2: Re-run the dispersion check against the NEW code**

Copy `C:\Users\KrisKnecht\.claude\jobs\870c1321\dispersion_check.py` to `dispersion_check_wt.py` in the job dir and change its two path lines so the **worktree** code is imported while the **live** data cache is reused:

```python
os.chdir(r"C:\Users\KrisKnecht\weatherbot")                       # reuse live lab cache
sys.path.insert(0, r"C:\Users\KrisKnecht\weatherbot\.claude\worktrees\weighted-sigma")  # NEW code first
```

Run: `python "C:\Users\KrisKnecht\.claude\jobs\870c1321\dispersion_check_wt.py" 90`
Acceptance criteria (record the output in the commit message or PR notes):
- **LAX** interior `k` rises from 0.36 toward ~1.0 and `modelSig` drops from ~3.8 to ~1.x.
- **DEN** interior `k` essentially unchanged (~0.93).
- No city overshoots to interior `k` > 1.3 (would signal σ collapsed too far → `base_sigma` needs a bump → out of scope, but flag it).

- [ ] **Step 3: Record the dispersion result**

Append a short "Validation — weighted σ" note (the per-city before/after `k` line for LAX and DEN, plus the pooled interior `k`) to `docs/MODEL_NOTES.md`, then commit:

```bash
git add docs/MODEL_NOTES.md
git commit -m "MODEL_NOTES: record weighted-sigma dispersion validation (LAX k 0.36->~1.0)"
```

---

## Validation gates handled at finish/deploy (NOT in this plan)

Per the spec, before the branch merges and the live dashboard restarts:
- **Gate 2 — replay before/after:** run `python -m lab.cli replay --config live-today --json` in the `model-lab` checkout (old σ) and this worktree (new σ) over the same window; diff YES/NO bets, win-rate, PnL (expect changes concentrated on LAX, no material harm elsewhere).
- **Gate 3 — per-city live calibration:** `python -m lab.cli live-calibration` confirming sharper LAX does not push pooled/per-city YES into material overconfidence.

These are deploy-readiness checks for the finishing-a-development-branch step, not implementation tasks.

## Self-review notes

- **Spec coverage:** weighted-std math (Task 1+2), effective-weight construction incl. NWS via `nws_blend` (Task 2 `_sigma_from`), all five edge cases (Task 1 tests: equal=pstdev, missing renormalise, single→0, none→None, zero-weight→None; Task 2 DEN-control test), blast-radius/real-data check (Task 3 baseline diff), dispersion gate 1 (Task 4). Gates 2/3 explicitly deferred to finish. ✓
- **No new config** (spec invariant): `_sigma_from` reads only existing `source_weights`/`nws_blend`/`sigma_sources`. ✓
- **Type consistency:** `weighted_std(values: Mapping[str, float|None], weights: Mapping[str, float]) -> float | None` used identically in Task 1 (definition) and Task 2 (`_sigma_from` call). ✓
