# Weatherbot Model Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pure-Python `model/` package, a `lab/` experimentation harness, and a `shadow/` live A/B logger so the live Kalshi temperature bot's calibration can be iteratively improved while preserving today's production behavior byte-for-byte until a graduation step is explicitly approved.

**Architecture:** Extract the model math currently inlined in `kalshi_temp.py` into `model.compute(inputs, cfg)` driven by a `ModelConfig` data structure. Live and backtest paths both call `compute()` with different configs. The `lab/` package replays configs over settled events with a SQLite-backed input cache; `compare`, `decompose`, and `refit-bias` subcommands turn the harness into a development loop. `shadow/` runs candidate configs alongside the live path on every dashboard refresh and logs picks to JSONL without acting on them. Three tiers of tests (primitives, pipeline integration, frozen baselines on real recorded events) make changes to `model/compute.py` safe to ship.

**Tech Stack:** Python 3.13, stdlib only for runtime (`requests` for HTTP, `sqlite3` for cache, `dataclasses`, `statistics`, `math`), `pytest` for tests. No new third-party deps.

**Reference spec:** `docs/superpowers/specs/2026-05-19-weatherbot-model-lab-design.md`

**Worktree note:** The live bot is currently running from this directory (`C:\Users\KrisKnecht\weatherbot`, PID 30248). Most tasks are additive (new files in `model/`, `lab/`, `shadow/`, `tests/`); they cannot affect live behavior. **Tasks 14–15 modify `kalshi_temp.py` directly** and require care — verify `test_baseline.py` is green before each commit, and never restart the live process automatically. Consider executing this plan in a `git worktree` for full isolation if you prefer; not strictly required.

---

## File Structure

**New files (all additive, no overwriting):**

```
weatherbot/
├── pyproject.toml                          ← packaging + dev deps
├── pytest.ini                              ← pytest config
├── requirements.txt                        ← runtime deps pin
├── .gitignore                              ← extend
├── model/
│   ├── __init__.py                         ← exports + CODE_VERSION
│   ├── types.py                            ← ModelInputs, ModelOutput
│   ├── config.py                           ← ModelConfig
│   ├── primitives.py                       ← pure-math functions
│   └── compute.py                          ← compute(inputs, cfg)
├── lab/
│   ├── __init__.py
│   ├── __main__.py                         ← python -m lab entry
│   ├── cli.py                              ← argparse routing
│   ├── configs.py                          ← LIVE_TODAY, BACKTEST_TODAY, variants
│   ├── data_cache.py                       ← SQLite input cache
│   ├── inputs.py                           ← assemble ModelInputs through cache
│   ├── replay.py                           ← replay a config over events
│   ├── compare.py                          ← A/B between configs with bootstrap CI
│   ├── decompose.py                        ← per-component attribution
│   ├── refit_bias.py                       ← refit BIAS for a config
│   └── shadow_summary.py                   ← join shadow log to outcomes
├── shadow/
│   ├── __init__.py
│   ├── runner.py                           ← run_shadow(inputs, configs)
│   └── active.py                           ← ACTIVE_SHADOWS list
├── tests/
│   ├── conftest.py                         ← --update-baseline flag
│   ├── test_primitives.py
│   ├── test_compute.py
│   ├── test_baseline.py
│   ├── test_shadow_isolation.py
│   ├── test_data_cache.py
│   └── fixtures/
│       └── baseline/                       ← *.json snapshots (gitignored sqlite,  tracked json)
└── docs/superpowers/
    ├── specs/2026-05-19-weatherbot-model-lab-design.md  ← already committed
    ├── plans/2026-05-19-weatherbot-model-lab.md         ← this file
    └── reports/2026-05-19-divergence-attribution.md     ← produced by Task 25
```

**Files modified:**

- `kalshi_temp.py:412-461` — replace inline model math in `build_event_data` with `compute(inputs, LIVE_TODAY)` (Task 14)
- `kalshi_temp.py:1460-1500` — replace inline model math in `backtest_one_event` with `compute(inputs, BACKTEST_TODAY)` (Task 15)
- `kalshi_temp.py:470-493` — add `run_shadow(...)` call inside `get_dashboard_data()` (Task 22)

---

## Task 1: Project plumbing

**Files:**
- Create: `pyproject.toml`
- Create: `pytest.ini`
- Create: `requirements.txt`
- Modify: `.gitignore` (create if absent)

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "weatherbot"
version = "0.1.0"
description = "Kalshi temperature predictor with model lab"
requires-python = ">=3.11"
dependencies = ["requests>=2.31"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[project.scripts]
lab = "lab.cli:main"

[tool.setuptools.packages.find]
where = ["."]
include = ["model*", "lab*", "shadow*"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"
```

- [ ] **Step 2: Create `pytest.ini`**

```ini
[pytest]
testpaths = tests
addopts = -q
filterwarnings =
    ignore::DeprecationWarning
```

- [ ] **Step 3: Create `requirements.txt`**

```
requests>=2.31
pytest>=8.0
```

- [ ] **Step 4: Extend `.gitignore`**

Append (create the file if absent):

```
__pycache__/
*.pyc
*.pyo
lab/.cache.sqlite
lab/.cache.sqlite-journal
lab/.cache.sqlite-wal
lab/.cache.sqlite-shm
shadow_picks.jsonl
.pytest_cache/
*.egg-info/
```

- [ ] **Step 5: Install dev deps and verify pytest runs**

Run: `python -m pip install -e ".[dev]"`
Expected: installs the package in editable mode. No errors.

Run: `python -m pytest`
Expected: `no tests ran` (collected 0 items). Exit 5 is normal when no tests are found.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml pytest.ini requirements.txt .gitignore
git commit -m "Add project plumbing for tests and packaging."
```

---

## Task 2: `model/__init__.py` + `CODE_VERSION`

**Files:**
- Create: `model/__init__.py`
- Create: `tests/test_code_version.py`

- [ ] **Step 1: Write the failing test**

`tests/test_code_version.py`:

```python
import re

def test_code_version_is_short_string():
    from model import CODE_VERSION
    assert isinstance(CODE_VERSION, str)
    assert 4 <= len(CODE_VERSION) <= 40
    assert re.fullmatch(r"[0-9a-f]+", CODE_VERSION)

def test_code_version_stable_across_imports():
    from model import CODE_VERSION as v1
    from model import CODE_VERSION as v2
    assert v1 == v2
```

- [ ] **Step 2: Run test, verify it fails**

Run: `python -m pytest tests/test_code_version.py -v`
Expected: ImportError (no `model` package yet).

- [ ] **Step 3: Implement `model/__init__.py`**

```python
"""Pure-Python weatherbot model package.

Exports:
    CODE_VERSION   — short string identifying the model code at runtime
    compute        — main entry point: compute(inputs, cfg) -> ModelOutput
    ModelInputs    — input dataclass
    ModelOutput    — output dataclass
    ModelConfig    — configuration dataclass
"""
import hashlib
import subprocess
from pathlib import Path


def _resolve_code_version() -> str:
    pkg_dir = Path(__file__).resolve().parent
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(pkg_dir.parent),
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        if sha:
            return sha
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    h = hashlib.sha256()
    for name in ("primitives.py", "compute.py", "config.py", "types.py"):
        path = pkg_dir / name
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()[:7]


CODE_VERSION: str = _resolve_code_version()

__all__ = ["CODE_VERSION"]
```

- [ ] **Step 4: Run test, verify it passes**

Run: `python -m pytest tests/test_code_version.py -v`
Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add model/__init__.py tests/test_code_version.py
git commit -m "Add model package with CODE_VERSION resolver."
```

---

## Task 3: Primitives — `normal_cdf`, `weighted_mean`

**Files:**
- Create: `model/primitives.py`
- Create: `tests/test_primitives.py`

- [ ] **Step 1: Write failing tests**

`tests/test_primitives.py`:

```python
import math

from model.primitives import normal_cdf, weighted_mean


def test_normal_cdf_at_mean_is_half():
    assert normal_cdf(0.0, 0.0, 1.0) == 0.5

def test_normal_cdf_monotonic():
    samples = [normal_cdf(x, 0.0, 1.0) for x in (-3, -1, 0, 1, 3)]
    assert all(a <= b for a, b in zip(samples, samples[1:]))

def test_normal_cdf_extremes():
    assert normal_cdf(-10.0, 0.0, 1.0) < 1e-15
    assert normal_cdf(10.0, 0.0, 1.0) > 1 - 1e-15

def test_normal_cdf_sigma_zero_degenerate():
    # σ=0 is a point mass at μ; CDF is a step function.
    assert normal_cdf(5.0, 5.0, 0.0) == 1.0
    assert normal_cdf(4.9, 5.0, 0.0) == 0.0
    assert normal_cdf(5.1, 5.0, 0.0) == 1.0


def test_weighted_mean_all_present():
    out = weighted_mean({"a": 10.0, "b": 20.0}, {"a": 0.3, "b": 0.7})
    assert out == pytest_approx(17.0)

def test_weighted_mean_one_missing():
    out = weighted_mean({"a": 10.0, "b": None}, {"a": 0.3, "b": 0.7})
    assert out == pytest_approx(10.0)  # renormalised to weight 'a' alone

def test_weighted_mean_all_missing_returns_none():
    assert weighted_mean({"a": None, "b": None}, {"a": 0.3, "b": 0.7}) is None

def test_weighted_mean_zero_total_weight_returns_none():
    assert weighted_mean({"a": 10.0}, {"a": 0.0}) is None

def test_weighted_mean_ignores_unknown_keys():
    out = weighted_mean({"a": 1.0, "z": 99.0}, {"a": 1.0})
    assert out == pytest_approx(1.0)


def pytest_approx(v, tol=1e-9):
    from pytest import approx
    return approx(v, abs=tol)
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_primitives.py -v`
Expected: ImportError on `model.primitives` (file doesn't exist).

- [ ] **Step 3: Implement `model/primitives.py`** (this task's portion only — other primitives added in later tasks)

```python
"""Pure-math primitives used by model.compute."""
import math
from collections.abc import Mapping


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    """CDF of N(mu, sigma^2) at x. Degenerate sigma=0 returns step function."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def weighted_mean(
    values: Mapping[str, float | None],
    weights: Mapping[str, float],
) -> float | None:
    """Weighted mean over keys present in both `values` and `weights`.

    Missing values (None) are dropped and weights are renormalised over
    surviving keys. Returns None if no key has a non-None value or if
    the surviving weights sum to <= 0.
    """
    parts: list[tuple[float, float]] = []
    for key, w in weights.items():
        v = values.get(key)
        if v is None:
            continue
        if w <= 0:
            continue
        parts.append((v, w))
    if not parts:
        return None
    total_w = sum(w for _, w in parts)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in parts) / total_w
```

- [ ] **Step 4: Run, verify passes**

Run: `python -m pytest tests/test_primitives.py -v`
Expected: all primitive tests in this task pass.

- [ ] **Step 5: Commit**

```bash
git add model/primitives.py tests/test_primitives.py
git commit -m "Add normal_cdf and weighted_mean primitives."
```

---

## Task 4: Primitives — `bucket_bounds`, `bucket_probability`

**Files:**
- Modify: `model/primitives.py`
- Modify: `tests/test_primitives.py`

- [ ] **Step 1: Append failing tests to `tests/test_primitives.py`**

```python
from model.primitives import bucket_bounds, bucket_probability


# --- bucket_bounds ----------------------------------------------------

def test_bucket_bounds_less():
    assert bucket_bounds({"strike_type": "less", "cap_strike": 50}) == (
        float("-inf"), 49.5
    )

def test_bucket_bounds_greater():
    assert bucket_bounds({"strike_type": "greater", "floor_strike": 80}) == (
        80.5, float("inf")
    )

def test_bucket_bounds_between():
    assert bucket_bounds({"strike_type": "between", "floor_strike": 70,
                          "cap_strike": 72}) == (69.5, 72.5)

def test_bucket_bounds_missing_strike_returns_none():
    assert bucket_bounds({"strike_type": "less"}) is None
    assert bucket_bounds({"strike_type": "between", "floor_strike": 70}) is None
    assert bucket_bounds({"strike_type": "weird"}) is None


# --- bucket_probability ----------------------------------------------

def test_bucket_probability_untruncated_normal():
    p = bucket_probability((69.5, 72.5), mu=71.0, sigma=2.0)
    # P(69.5 < X < 72.5) where X ~ N(71, 4)
    assert 0.4 < p < 0.6

def test_bucket_probability_truncated_below_mu():
    # truncation 67 means condition on X >= 67. Bucket above truncation.
    p_t = bucket_probability((69.5, 72.5), mu=71.0, sigma=2.0, lower_truncation=67.0)
    p_u = bucket_probability((69.5, 72.5), mu=71.0, sigma=2.0)
    # Conditional probability should be higher than unconditional.
    assert p_t > p_u

def test_bucket_probability_bucket_entirely_below_truncation():
    p = bucket_probability((60.0, 65.0), mu=71.0, sigma=2.0, lower_truncation=68.0)
    assert p == 0.0

def test_bucket_probability_truncation_above_bucket_upper():
    # Bucket lives wholly below the truncation -> 0
    assert bucket_probability((60.0, 65.0), mu=71.0, sigma=2.0,
                              lower_truncation=66.0) == 0.0

def test_bucket_probability_sigma_zero_point_mass():
    # At sigma=0, prob is 1.0 if mu falls in the bucket, else 0.
    assert bucket_probability((69.5, 72.5), mu=71.0, sigma=0.0) == 1.0
    assert bucket_probability((60.0, 65.0), mu=71.0, sigma=0.0) == 0.0
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_primitives.py -v`
Expected: ImportError on `bucket_bounds` / `bucket_probability`.

- [ ] **Step 3: Append to `model/primitives.py`**

```python
def bucket_bounds(market) -> tuple[float, float] | None:
    """Continuous bounds for a Kalshi bucket. Lower/upper may be ±inf.

    The 0.5 offsets match Kalshi's bucket semantics: 'less cap=50' means
    integer high <= 49, encoded as the continuous interval (-inf, 49.5);
    'between floor=70 cap=72' means 70 <= high <= 72, encoded (69.5, 72.5).
    """
    st = market.get("strike_type")
    cap = market.get("cap_strike")
    floor = market.get("floor_strike")
    if st == "less" and cap is not None:
        return float("-inf"), cap - 0.5
    if st == "greater" and floor is not None:
        return floor + 0.5, float("inf")
    if st == "between" and floor is not None and cap is not None:
        return floor - 0.5, cap + 0.5
    return None


def bucket_probability(
    bounds: tuple[float, float],
    mu: float,
    sigma: float,
    *,
    lower_truncation: float | None = None,
) -> float | None:
    """Probability the day's high falls in `bounds`.

    With `lower_truncation` set (e.g. an observed afternoon peak), returns
    the conditional probability P(bucket | high >= lower_truncation),
    zeroing buckets that lie entirely below the truncation and
    renormalising over the surviving tail.
    """
    lower, upper = bounds
    if lower_truncation is None:
        return normal_cdf(upper, mu, sigma) - normal_cdf(lower, mu, sigma)
    if upper <= lower_truncation:
        return 0.0
    denom = 1.0 - normal_cdf(lower_truncation, mu, sigma)
    if denom <= 0:
        return 1.0 if lower_truncation < upper else 0.0
    eff_lower = max(lower, lower_truncation)
    raw = normal_cdf(upper, mu, sigma) - normal_cdf(eff_lower, mu, sigma)
    return max(0.0, raw / denom)
```

- [ ] **Step 4: Run, verify passes**

Run: `python -m pytest tests/test_primitives.py -v`
Expected: all primitive tests pass (Task 3's + Task 4's).

- [ ] **Step 5: Commit**

```bash
git add model/primitives.py tests/test_primitives.py
git commit -m "Add bucket_bounds and bucket_probability primitives."
```

---

## Task 5: Primitives — `apply_today_max`, `event_local_date`

**Files:**
- Modify: `model/primitives.py`
- Modify: `tests/test_primitives.py`

- [ ] **Step 1: Append failing tests**

```python
from model.primitives import apply_today_max, event_local_date


# --- apply_today_max --------------------------------------------------

def _mode(name, headroom=0.5, push=0.3):
    from model.config import ModelConfig
    return ModelConfig(
        name="t", source_weights={}, nws_blend=0.0, bias_table={},
        base_sigma=2.0, sigma_sources=("ecmwf", "gfs"),
        today_max_mode=name, today_max_headroom=headroom, today_max_push=push,
        sanity_no_yes_ask_min=0.85, sanity_no_prob_max=0.40,
        decision_lead_hours=24.0,
    )

def test_apply_today_max_off():
    trunc, mu, active = apply_today_max(mu=71.0, today_max=78.0, cfg=_mode("off"))
    assert trunc is None and mu == 71.0 and active is False

def test_apply_today_max_truncate_above_mu():
    # today_max=78, headroom=0.5 -> truncation=77.5. mu=71, so truncation > mu.
    trunc, mu, active = apply_today_max(71.0, 78.0, _mode("truncate"))
    assert trunc == 77.5 and mu == 71.0 and active is True

def test_apply_today_max_truncate_below_mu():
    trunc, mu, active = apply_today_max(80.0, 70.0, _mode("truncate"))
    assert trunc == 69.5 and mu == 80.0 and active is False

def test_apply_today_max_push_above_mu():
    trunc, mu, active = apply_today_max(71.0, 78.0, _mode("push"))
    assert trunc is None and mu == 77.5 + 0.3 and active is True

def test_apply_today_max_push_below_mu():
    trunc, mu, active = apply_today_max(80.0, 70.0, _mode("push"))
    assert trunc is None and mu == 80.0 and active is False

def test_apply_today_max_both_above_mu():
    # current production behaviour (the double-counting case)
    trunc, mu, active = apply_today_max(71.0, 78.0, _mode("both"))
    assert trunc == 77.5 and mu == 77.5 + 0.3 and active is True

def test_apply_today_max_both_below_mu():
    trunc, mu, active = apply_today_max(80.0, 70.0, _mode("both"))
    assert trunc == 69.5 and mu == 80.0 and active is False


# --- event_local_date -------------------------------------------------

def test_event_local_date_ticker_suffix():
    assert event_local_date({"event_ticker": "KXHIGHNY-26MAY13"}) == "2026-05-13"
    assert event_local_date({"event_ticker": "KXHIGHCHI-26JAN05"}) == "2026-01-05"

def test_event_local_date_strike_fallback():
    ev = {"event_ticker": "KXHIGHNY-BAD", "strike_date": "2026-05-14T05:00:00Z"}
    # strike_date 05:00Z = 01:00 ET = day-after close, so target should be prior day
    assert event_local_date(ev) == "2026-05-13"

def test_event_local_date_unparseable_returns_today_utc():
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    assert event_local_date({"event_ticker": "GARBAGE"}) == today
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_primitives.py -v`
Expected: ImportError on `apply_today_max` and on `model.config` (config not built yet — see Task 6); plus failures on event_local_date.

If you want to defer the apply_today_max tests until Task 6 lands `ModelConfig`, you may skip them with `pytest.mark.skip` here and unskip in Task 6. Recommended: just do Task 6 next; it's tiny.

- [ ] **Step 3: Append to `model/primitives.py`**

```python
from datetime import datetime, timedelta, timezone


_MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}


def event_local_date(ev: Mapping) -> str:
    """Decode 'KXHIGHNY-26MAY12' -> '2026-05-12'.

    Falls back to strike_date − 12h heuristic, then to today UTC.
    """
    try:
        suffix = ev["event_ticker"].rsplit("-", 1)[-1]
        yy = int(suffix[:2])
        mon = _MONTHS[suffix[2:5].upper()]
        day = int(suffix[5:])
        return f"{2000 + yy:04d}-{mon:02d}-{day:02d}"
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(ev["strike_date"].replace("Z", "+00:00"))
        return (dt - timedelta(hours=12)).date().isoformat()
    except Exception:
        return datetime.now(timezone.utc).date().isoformat()


def apply_today_max(mu: float, today_max: float, cfg) -> tuple[float | None, float, bool]:
    """Apply the TODAY-MAX adjustment per cfg.today_max_mode.

    Returns (truncation, mu_adjusted, active).

    Modes:
        "off"       — no adjustment. Returns (None, mu, False).
        "truncate"  — pass `today_max - headroom` as lower-truncation to the
                      CDF. mu unchanged. Active iff truncation > mu.
        "push"      — if truncation > mu, raise mu to `truncation + push`.
                      Do NOT also pass truncation to the CDF.
        "both"      — current production behaviour: both push mu AND pass
                      truncation to the CDF. (Known to double-count.)
    """
    if cfg.today_max_mode == "off":
        return None, mu, False
    truncation = today_max - cfg.today_max_headroom
    over = truncation > mu
    if cfg.today_max_mode == "truncate":
        return truncation, mu, over
    if cfg.today_max_mode == "push":
        if over:
            return None, truncation + cfg.today_max_push, True
        return None, mu, False
    if cfg.today_max_mode == "both":
        if over:
            return truncation, truncation + cfg.today_max_push, True
        return truncation, mu, False
    raise ValueError(f"unknown today_max_mode: {cfg.today_max_mode!r}")
```

- [ ] **Step 4: Run, verify passes**

Run: `python -m pytest tests/test_primitives.py -v`
Expected: `event_local_date` tests pass; `apply_today_max` tests still fail because `ModelConfig` doesn't exist yet — that's resolved in Task 6.

- [ ] **Step 5: Commit**

```bash
git add model/primitives.py tests/test_primitives.py
git commit -m "Add event_local_date and apply_today_max primitives."
```

---

## Task 6: Data contracts — `model/types.py`, `model/config.py`

**Files:**
- Create: `model/types.py`
- Create: `model/config.py`
- Modify: `model/__init__.py`

- [ ] **Step 1: Create `model/types.py`**

```python
"""Input / output dataclasses for model.compute."""
from dataclasses import dataclass
from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class ModelInputs:
    series: str                              # "KXHIGHNY"
    event_ticker: str                        # "KXHIGHNY-26MAY13"
    target_date: str                         # "2026-05-13"
    forecasts: Mapping[str, float | None]    # {"ecmwf": 72.4, "gfs": 71.8, "nws": 73.1}
    metar_current: float | None              # latest METAR (informational)
    today_max: float | None                  # max METAR observed today
    markets: Sequence[Mapping]               # raw Kalshi market dicts
    decision_ts: int                         # unix seconds at decision time
    fetched_at: int                          # unix seconds when captured


@dataclass(frozen=True)
class ModelOutput:
    mu: float | None
    sigma: float | None
    mu_raw: float | None                     # pre-bias mu (diagnostics)
    bias_applied: float                      # the BIAS value used
    truncation: float | None                 # active lower-truncation
    today_max_active: bool
    probs: Mapping[str, float]               # {ticker: P(bucket)}
    config_name: str
    code_version: str
```

- [ ] **Step 2: Create `model/config.py`**

```python
"""ModelConfig — every knob that varies between live, backtest, and variants."""
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Literal


TodayMaxMode = Literal["off", "truncate", "push", "both"]


@dataclass(frozen=True)
class ModelConfig:
    name: str
    source_weights: Mapping[str, Mapping[str, float]]   # series → {source: weight}
    nws_blend: float                                     # 0.0 = no NWS overlay
    bias_table: Mapping[str, float]                      # series → bias offset (subtracted)
    base_sigma: float                                    # σ floor in °F
    sigma_sources: tuple[str, ...]                       # which keys feed pstdev
    today_max_mode: TodayMaxMode
    today_max_headroom: float                            # °F (e.g. 0.5)
    today_max_push: float                                # °F (only used if mode in {"push","both"})
    sanity_no_yes_ask_min: float                         # e.g. 0.85
    sanity_no_prob_max: float                            # e.g. 0.40
    decision_lead_hours: float                           # e.g. 24.0
```

- [ ] **Step 3: Update `model/__init__.py` exports**

Edit the file to add imports and re-exports:

```python
from .config import ModelConfig, TodayMaxMode
from .types import ModelInputs, ModelOutput

__all__ = ["CODE_VERSION", "ModelConfig", "ModelInputs", "ModelOutput", "TodayMaxMode"]
```

(Place `from .types ...` and `from .config ...` AFTER the existing `CODE_VERSION` block.)

- [ ] **Step 4: Run primitive tests, now apply_today_max passes**

Run: `python -m pytest tests/test_primitives.py -v`
Expected: all primitive tests pass (including the `apply_today_max` ones from Task 5).

- [ ] **Step 5: Commit**

```bash
git add model/types.py model/config.py model/__init__.py
git commit -m "Add ModelInputs, ModelOutput, ModelConfig dataclasses."
```

---

## Task 7: `model/compute.py`

**Files:**
- Create: `model/compute.py`
- Modify: `model/__init__.py`
- Create: `tests/test_compute.py`

- [ ] **Step 1: Write failing integration tests**

`tests/test_compute.py`:

```python
import math
import time

import pytest

from model import compute, ModelConfig, ModelInputs


def _basic_cfg(**overrides) -> ModelConfig:
    base = dict(
        name="test",
        source_weights={"KXHIGHNY": {"ecmwf": 0.5, "gfs": 0.5}},
        nws_blend=0.0,
        bias_table={},
        base_sigma=2.0,
        sigma_sources=("ecmwf", "gfs"),
        today_max_mode="off",
        today_max_headroom=0.5,
        today_max_push=0.3,
        sanity_no_yes_ask_min=0.85,
        sanity_no_prob_max=0.40,
        decision_lead_hours=24.0,
    )
    base.update(overrides)
    return ModelConfig(**base)


def _basic_inputs(**overrides) -> ModelInputs:
    base = dict(
        series="KXHIGHNY",
        event_ticker="KXHIGHNY-26MAY20",
        target_date="2026-05-20",
        forecasts={"ecmwf": 72.0, "gfs": 70.0, "nws": None},
        metar_current=None, today_max=None,
        markets=[
            {"ticker": "T-LOW", "strike_type": "less", "cap_strike": 70},
            {"ticker": "T-MID", "strike_type": "between", "floor_strike": 70, "cap_strike": 72},
            {"ticker": "T-HI",  "strike_type": "greater", "floor_strike": 72},
        ],
        decision_ts=int(time.time()),
        fetched_at=int(time.time()),
    )
    base.update(overrides)
    return ModelInputs(**base)


def test_compute_basic_no_nws_no_truncation():
    out = compute(_basic_inputs(), _basic_cfg())
    # mu = 0.5*72 + 0.5*70 = 71, sigma = sqrt(4 + pstdev([72,70])^2) = sqrt(4+1) = sqrt(5)
    assert out.mu == pytest.approx(71.0, abs=1e-9)
    assert out.sigma == pytest.approx(math.sqrt(5.0), abs=1e-9)
    assert out.mu_raw == pytest.approx(71.0, abs=1e-9)
    assert out.bias_applied == 0.0
    assert out.truncation is None and out.today_max_active is False
    assert set(out.probs.keys()) == {"T-LOW", "T-MID", "T-HI"}
    assert sum(out.probs.values()) == pytest.approx(1.0, abs=1e-9)


def test_compute_with_nws_overlay():
    inputs = _basic_inputs(forecasts={"ecmwf": 72.0, "gfs": 70.0, "nws": 75.0})
    cfg = _basic_cfg(nws_blend=0.3, sigma_sources=("ecmwf", "gfs", "nws"))
    out = compute(inputs, cfg)
    # mu_blend = 71, then 0.7*71 + 0.3*75 = 72.2
    assert out.mu_raw == pytest.approx(72.2, abs=1e-9)


def test_compute_applies_bias():
    cfg = _basic_cfg(bias_table={"KXHIGHNY": -0.44})
    out = compute(_basic_inputs(), cfg)
    # mu_raw 71, bias -0.44, subtract -> mu 71 - (-0.44) = 71.44
    assert out.mu == pytest.approx(71.44, abs=1e-9)
    assert out.bias_applied == -0.44


def test_compute_today_max_truncate_only():
    inputs = _basic_inputs(today_max=72.0)
    cfg = _basic_cfg(today_max_mode="truncate")
    out = compute(inputs, cfg)
    # truncation = 72 - 0.5 = 71.5. mu unchanged at 71. truncation > mu so active.
    assert out.truncation == pytest.approx(71.5, abs=1e-9)
    assert out.today_max_active is True
    assert out.mu == pytest.approx(71.0, abs=1e-9)


def test_compute_today_max_both_doubles_correction():
    """Reproduces the current production bug exactly."""
    inputs = _basic_inputs(today_max=72.0)
    cfg = _basic_cfg(today_max_mode="both")
    out = compute(inputs, cfg)
    # truncation = 71.5, but mu also pushed to 71.5 + 0.3 = 71.8
    assert out.truncation == pytest.approx(71.5, abs=1e-9)
    assert out.mu == pytest.approx(71.8, abs=1e-9)
    assert out.today_max_active is True


def test_compute_no_sources_returns_empty():
    inputs = _basic_inputs(forecasts={"ecmwf": None, "gfs": None, "nws": None})
    out = compute(inputs, _basic_cfg())
    assert out.mu is None and out.sigma is None and out.probs == {}


def test_compute_carries_config_name_and_code_version():
    out = compute(_basic_inputs(), _basic_cfg(name="my-config"))
    assert out.config_name == "my-config"
    from model import CODE_VERSION
    assert out.code_version == CODE_VERSION
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_compute.py -v`
Expected: ImportError — `compute` not in `model` yet.

- [ ] **Step 3: Implement `model/compute.py`**

```python
"""compute(inputs, cfg) — the model pipeline.

This is the single function called by:
    - kalshi_temp.build_event_data         (live)
    - kalshi_temp.backtest_one_event       (backtest)
    - lab.replay                            (experimentation)
    - shadow.runner                         (live A/B)

Pure with respect to inputs+cfg: no I/O, no global state.
"""
import math
import statistics

from . import CODE_VERSION
from .config import ModelConfig
from .primitives import (
    apply_today_max,
    bucket_bounds,
    bucket_probability,
    weighted_mean,
)
from .types import ModelInputs, ModelOutput


def _blend_sources(inputs: ModelInputs, cfg: ModelConfig) -> float | None:
    """ECMWF/GFS weighted blend per cfg, then optional NWS overlay."""
    weights = cfg.source_weights.get(inputs.series, {})
    blend = weighted_mean(inputs.forecasts, weights) if weights else None

    if blend is None:
        # Fall back to a simple mean of all available forecasts. Matches the
        # current live behaviour when both ECMWF and GFS are missing but NWS
        # (or any other source) is present.
        present = [v for v in inputs.forecasts.values() if v is not None]
        if not present:
            return None
        blend = sum(present) / len(present)

    nws = inputs.forecasts.get("nws")
    if cfg.nws_blend > 0 and nws is not None:
        blend = (1.0 - cfg.nws_blend) * blend + cfg.nws_blend * nws

    return blend


def _sigma_from(inputs: ModelInputs, cfg: ModelConfig) -> float:
    sources = [inputs.forecasts.get(s) for s in cfg.sigma_sources]
    sources = [v for v in sources if v is not None]
    spread = statistics.pstdev(sources) if len(sources) > 1 else 0.0
    return math.sqrt(cfg.base_sigma ** 2 + spread ** 2)


def compute(inputs: ModelInputs, cfg: ModelConfig) -> ModelOutput:
    mu_raw = _blend_sources(inputs, cfg)
    if mu_raw is None:
        return ModelOutput(
            mu=None, sigma=None, mu_raw=None, bias_applied=0.0,
            truncation=None, today_max_active=False, probs={},
            config_name=cfg.name, code_version=CODE_VERSION,
        )

    bias = cfg.bias_table.get(inputs.series, 0.0)
    mu = mu_raw - bias
    sigma = _sigma_from(inputs, cfg)

    truncation: float | None = None
    today_max_active = False
    if cfg.today_max_mode != "off" and inputs.today_max is not None:
        truncation, mu, today_max_active = apply_today_max(mu, inputs.today_max, cfg)

    probs: dict[str, float] = {}
    for m in inputs.markets:
        bounds = bucket_bounds(m)
        if bounds is None:
            continue
        p = bucket_probability(bounds, mu, sigma, lower_truncation=truncation)
        if p is not None:
            probs[m["ticker"]] = p

    return ModelOutput(
        mu=mu, sigma=sigma, mu_raw=mu_raw, bias_applied=bias,
        truncation=truncation, today_max_active=today_max_active,
        probs=probs, config_name=cfg.name, code_version=CODE_VERSION,
    )
```

- [ ] **Step 4: Update `model/__init__.py` to export `compute`**

Add to the imports block in `model/__init__.py`:

```python
from .compute import compute
```

And add `"compute"` to `__all__`.

- [ ] **Step 5: Run, verify all tests pass**

Run: `python -m pytest -v`
Expected: all primitive + compute + code_version tests pass.

- [ ] **Step 6: Commit**

```bash
git add model/compute.py model/__init__.py tests/test_compute.py
git commit -m "Add compute() pipeline with NWS overlay and TODAY-MAX modes."
```

---

## Task 8: `lab/configs.py` part 1 — `LIVE_TODAY` and `BACKTEST_TODAY`

**Files:**
- Create: `lab/__init__.py`
- Create: `lab/configs.py`
- Create: `tests/test_configs.py`

These two configs MUST reproduce the current production formulas byte-for-byte. Read `kalshi_temp.py:48-78` for the current values.

- [ ] **Step 1: Create empty `lab/__init__.py`**

```python
"""weatherbot model lab — experimentation harness over model.compute."""
```

- [ ] **Step 2: Write failing test**

`tests/test_configs.py`:

```python
"""LIVE_TODAY and BACKTEST_TODAY must reproduce the current kalshi_temp.py
constants exactly. If you change kalshi_temp's SOURCE_WEIGHTS, BIAS, or
BASE_SIGMA, mirror it here and re-record baselines."""

import kalshi_temp as kt

from lab.configs import LIVE_TODAY, BACKTEST_TODAY


def test_live_today_mirrors_source_weights():
    assert LIVE_TODAY.source_weights == {
        k: {"ecmwf": v["ecmwf_ifs025"], "gfs": v["gfs_seamless"]}
        for k, v in kt.SOURCE_WEIGHTS.items()
    }

def test_live_today_mirrors_bias_table():
    assert LIVE_TODAY.bias_table == kt.BIAS

def test_live_today_base_sigma_matches_kt():
    assert LIVE_TODAY.base_sigma == kt.BASE_SIGMA

def test_live_today_decision_lead_matches_kt():
    assert LIVE_TODAY.decision_lead_hours == kt.DEFAULT_LEAD_HOURS

def test_live_today_uses_nws_overlay_and_both_today_max():
    assert LIVE_TODAY.nws_blend == 0.3
    assert LIVE_TODAY.today_max_mode == "both"
    assert LIVE_TODAY.sigma_sources == ("ecmwf", "gfs", "nws")

def test_backtest_today_skips_nws_and_today_max():
    assert BACKTEST_TODAY.nws_blend == 0.0
    assert BACKTEST_TODAY.today_max_mode == "off"
    assert BACKTEST_TODAY.sigma_sources == ("ecmwf", "gfs")
    # Same bias table, same source weights, same base sigma.
    assert BACKTEST_TODAY.source_weights == LIVE_TODAY.source_weights
    assert BACKTEST_TODAY.bias_table == LIVE_TODAY.bias_table
    assert BACKTEST_TODAY.base_sigma == LIVE_TODAY.base_sigma
```

- [ ] **Step 3: Run, verify failure**

Run: `python -m pytest tests/test_configs.py -v`
Expected: ImportError on `lab.configs`.

- [ ] **Step 4: Implement `lab/configs.py`**

```python
"""Named ModelConfig instances. LIVE_TODAY reproduces current production
exactly; BACKTEST_TODAY reproduces the current cmd_backtest formula. New
variants are added here as additional named constants."""
from types import MappingProxyType

import kalshi_temp as _kt

from model import ModelConfig


# Translate kalshi_temp's "ecmwf_ifs025" / "gfs_seamless" keys to the short
# names compute() expects in inputs.forecasts ("ecmwf", "gfs", "nws").
_KT_SOURCE_WEIGHTS = MappingProxyType({
    series: MappingProxyType({"ecmwf": w["ecmwf_ifs025"], "gfs": w["gfs_seamless"]})
    for series, w in _kt.SOURCE_WEIGHTS.items()
})


LIVE_TODAY = ModelConfig(
    name="live-today",
    source_weights=_KT_SOURCE_WEIGHTS,
    nws_blend=0.3,
    bias_table=MappingProxyType(dict(_kt.BIAS)),
    base_sigma=_kt.BASE_SIGMA,
    sigma_sources=("ecmwf", "gfs", "nws"),
    today_max_mode="both",
    today_max_headroom=0.5,
    today_max_push=0.3,
    sanity_no_yes_ask_min=0.85,
    sanity_no_prob_max=0.40,
    decision_lead_hours=_kt.DEFAULT_LEAD_HOURS,
)


BACKTEST_TODAY = ModelConfig(
    name="backtest-today",
    source_weights=LIVE_TODAY.source_weights,
    nws_blend=0.0,
    bias_table=LIVE_TODAY.bias_table,
    base_sigma=LIVE_TODAY.base_sigma,
    sigma_sources=("ecmwf", "gfs"),
    today_max_mode="off",
    today_max_headroom=0.5,
    today_max_push=0.3,
    sanity_no_yes_ask_min=0.85,
    sanity_no_prob_max=0.40,
    decision_lead_hours=LIVE_TODAY.decision_lead_hours,
)


_BY_NAME = {c.name: c for c in (LIVE_TODAY, BACKTEST_TODAY)}


def get(name: str) -> ModelConfig:
    if name not in _BY_NAME:
        raise KeyError(f"unknown config: {name!r}. Known: {sorted(_BY_NAME)}")
    return _BY_NAME[name]


def all_configs() -> list[ModelConfig]:
    return list(_BY_NAME.values())
```

- [ ] **Step 5: Run, verify passes**

Run: `python -m pytest tests/test_configs.py -v`
Expected: all 6 tests pass.

- [ ] **Step 6: Commit**

```bash
git add lab/__init__.py lab/configs.py tests/test_configs.py
git commit -m "Add LIVE_TODAY and BACKTEST_TODAY configs mirroring current code."
```

---

## Task 9: Baseline fixtures — capture script + first snapshots

**Files:**
- Create: `tests/fixtures/baseline/.gitkeep`
- Create: `tests/_record_baseline.py`
- Capture and commit 10 fixture JSON files under `tests/fixtures/baseline/`

The baseline fixtures are the regression canary for the kalshi_temp.py extraction in Tasks 10–11. They capture *current production outputs* on real events, BEFORE the extraction touches the code. After extraction, the same fixtures must reproduce exactly the same outputs.

- [ ] **Step 1: Create `tests/fixtures/baseline/.gitkeep`**

```
(empty file — just so the directory is tracked)
```

- [ ] **Step 2: Create `tests/_record_baseline.py`**

```python
"""Capture (ModelInputs, expected_output) snapshots from CURRENT production.

Run from repo root, BEFORE the kalshi_temp.py extraction in Tasks 10-11.

    python tests/_record_baseline.py KXHIGHNY-26MAY15 KXHIGHNY-26MAY16 ...

For each event ticker, this script:
    1. Calls kalshi_temp.build_event_data (the current live path)
    2. Captures the inputs (forecasts, metar, today_max, markets) that
       went into the model decision and the outputs (mu, sigma, probs)
    3. Writes tests/fixtures/baseline/<event_ticker>.json

These snapshots become the ground truth that Task 11 must reproduce
to 4 decimal places after the extraction.
"""
import json
import sys
import time
from pathlib import Path

import kalshi_temp as kt


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "baseline"


def capture_one(event_ticker: str) -> dict:
    ev, markets = kt.fetch_event_and_markets(event_ticker)
    data = kt.build_event_data(ev, markets)
    if data is None:
        raise SystemExit(f"unsupported event: {event_ticker}")
    f = data["forecasts"]
    m = data["model"]
    # Captured inputs match the ModelInputs dataclass (post-extraction).
    fixture = {
        "event_ticker": event_ticker,
        "series": ev["series_ticker"],
        "target_date": data["target_date"],
        "forecasts": {
            "ecmwf": f.get("ecmwf"),
            "gfs": f.get("gfs"),
            "nws": f.get("nws"),
        },
        "metar_current": f.get("metar"),
        "today_max": f.get("today_max"),
        "markets": [
            {k: v for k, v in market.items()
             if k in {"ticker", "strike_type", "floor_strike", "cap_strike",
                      "yes_ask_dollars", "yes_bid_dollars", "no_ask_dollars",
                      "subtitle"}}
            for market in markets
        ],
        "captured_at": int(time.time()),
        "expected": {
            "mu": m.get("mu"),
            "sigma": m.get("sigma"),
            "mu_raw": m.get("mu_raw"),
            "bias_applied": m.get("bias"),
            "truncation": m.get("truncation"),
            "today_max_active": (m.get("truncation") is not None or
                                  m.get("today_max") is not None and
                                  m.get("truncation") is not None),
            "probs": {x["ticker"]: x["prob"] for x in data["markets"]
                      if x.get("prob") is not None},
        },
    }
    return fixture


def main(argv):
    if not argv:
        sys.exit("Usage: python tests/_record_baseline.py TICKER1 [TICKER2 ...]")
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    for ticker in argv:
        print(f"Capturing {ticker}...", file=sys.stderr)
        fixture = capture_one(ticker)
        path = FIXTURE_DIR / f"{ticker}.json"
        path.write_text(json.dumps(fixture, indent=2))
        print(f"  -> {path}", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1:])
```

- [ ] **Step 3: Pick 10 events to capture**

Pick from recently settled events across all 7 cities, plus 2-3 edge cases:

```bash
python kalshi_temp.py backtest --series KXHIGHNY --days 7
# note 1-2 ticker names that show 'TODAY-MAX active' or 'truncation' in the model line
python kalshi_temp.py backtest --series KXHIGHCHI --days 7
python kalshi_temp.py backtest --series KXHIGHMIA --days 7
python kalshi_temp.py backtest --series KXHIGHLAX --days 7
python kalshi_temp.py backtest --series KXHIGHDEN --days 7
python kalshi_temp.py backtest --series KXHIGHAUS --days 7
python kalshi_temp.py backtest --series KXHIGHPHIL --days 7
```

Pick 10 event tickers, at least one per city; include 1-2 events from very hot / very cold days (likely to have a "less" or "greater" winning bucket, which exercises the edge cases) and 1-2 events captured during a still-open event (today_max non-null).

- [ ] **Step 4: Run the capture script**

```bash
python tests/_record_baseline.py KXHIGHNY-26MAY15 KXHIGHCHI-26MAY15 KXHIGHMIA-26MAY15 \
    KXHIGHLAX-26MAY15 KXHIGHDEN-26MAY15 KXHIGHAUS-26MAY15 KXHIGHPHIL-26MAY15 \
    <ticker8> <ticker9> <ticker10>
```

Expected: 10 JSON files appear in `tests/fixtures/baseline/`.

- [ ] **Step 5: Verify fixtures are sane**

Quick sanity check — each JSON has `expected.probs` summing close to 1.0 for settled events:

```bash
python -c "import json,glob; [print(p, sum(json.loads(open(p).read())['expected']['probs'].values())) for p in glob.glob('tests/fixtures/baseline/*.json')]"
```

Expected: each prob sum should be 0.99–1.01.

- [ ] **Step 6: Commit**

```bash
git add tests/_record_baseline.py tests/fixtures/baseline/
git commit -m "Capture baseline fixtures for 10 settled events (pre-extraction)."
```

---

## Task 10: Baseline regression test + `--update-baseline` flag

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_baseline.py`

- [ ] **Step 1: Create `tests/conftest.py`**

```python
"""Shared pytest fixtures and the --update-baseline flag for test_baseline.py."""
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--update-baseline",
        action="store_true",
        default=False,
        help="Rewrite baseline fixtures with current model outputs (intentional drift only).",
    )


@pytest.fixture
def update_baseline(request):
    return request.config.getoption("--update-baseline")
```

- [ ] **Step 2: Create `tests/test_baseline.py`**

```python
"""Frozen-output regression on real recorded events.

If a change to model/compute.py is intentional, run:
    pytest --update-baseline
review the JSON diff in `git diff`, and commit.
"""
import json
import time
from pathlib import Path

import pytest

from lab.configs import LIVE_TODAY
from model import ModelInputs, compute


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "baseline"
TOLERANCE = 1e-4  # 4 decimal places


def _fixture_paths():
    return sorted(FIXTURE_DIR.glob("*.json"))


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.stem)
def test_live_today_matches_baseline(path, update_baseline):
    fixture = json.loads(path.read_text())
    inputs = ModelInputs(
        series=fixture["series"],
        event_ticker=fixture["event_ticker"],
        target_date=fixture["target_date"],
        forecasts=fixture["forecasts"],
        metar_current=fixture.get("metar_current"),
        today_max=fixture.get("today_max"),
        markets=fixture["markets"],
        decision_ts=fixture["captured_at"],
        fetched_at=fixture["captured_at"],
    )
    out = compute(inputs, LIVE_TODAY)
    actual = {
        "mu": out.mu, "sigma": out.sigma, "mu_raw": out.mu_raw,
        "bias_applied": out.bias_applied, "truncation": out.truncation,
        "today_max_active": out.today_max_active,
        "probs": dict(out.probs),
    }
    expected = fixture["expected"]

    if update_baseline:
        fixture["expected"] = actual
        path.write_text(json.dumps(fixture, indent=2))
        pytest.skip(f"baseline rewritten for {path.stem}")

    # numeric fields with tolerance
    for key in ("mu", "sigma", "mu_raw", "bias_applied", "truncation"):
        if expected[key] is None or actual[key] is None:
            assert expected[key] == actual[key], f"{key}: {expected[key]} vs {actual[key]}"
        else:
            assert actual[key] == pytest.approx(expected[key], abs=TOLERANCE), \
                f"{path.stem}/{key}"

    assert actual["today_max_active"] == expected["today_max_active"], \
        f"{path.stem}/today_max_active"

    # probs: keys must match, values match to tolerance
    assert set(actual["probs"]) == set(expected["probs"]), \
        f"{path.stem} probs key mismatch"
    for ticker, ep in expected["probs"].items():
        assert actual["probs"][ticker] == pytest.approx(ep, abs=TOLERANCE), \
            f"{path.stem}/probs/{ticker}"
```

- [ ] **Step 3: Run, verify baseline test passes BEFORE extraction**

Run: `python -m pytest tests/test_baseline.py -v`
Expected: all 10 fixtures pass (because the new compute() with LIVE_TODAY is designed to match current behavior — if anything fails here, the bug is in compute() or LIVE_TODAY, not in kalshi_temp.py).

If failures appear, fix `model/compute.py` or `lab/configs.py:LIVE_TODAY` until the baseline is green. The point of having captured the baseline BEFORE touching `kalshi_temp.py` is exactly to drive this convergence.

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py tests/test_baseline.py
git commit -m "Add baseline regression test and --update-baseline flag."
```

---

## Task 11: Extract model math in `kalshi_temp.py::build_event_data`

**Files:**
- Modify: `kalshi_temp.py:412-461` (the model block inside `build_event_data`)

This is the highest-risk task in the plan. The live bot serves real money. Verify `tests/test_baseline.py` is green before and after; do not skip the verification step.

- [ ] **Step 1: Verify baseline is currently green**

Run: `python -m pytest tests/test_baseline.py -v`
Expected: 10/10 pass. If not, fix before continuing.

- [ ] **Step 2: Make the edit in `kalshi_temp.py`**

Locate `build_event_data` (around line 376). Replace the body from where forecasts are first computed (around line 398, after `forecasts = {"ecmwf": ecmwf, ...}`) down through the `out_markets` assembly (around line 449) with this:

```python
    forecasts = {"ecmwf": ecmwf, "gfs": gfs, "nws": nws, "metar": metar,
                 "today_max": today_max}

    settled_bucket = None
    for m in markets:
        if (to_float(m.get("yes_bid_dollars")) or 0) >= 0.95:
            settled_bucket = m.get("subtitle") or synth_subtitle(m) or "?"
            break
    settled = settled_bucket is not None

    if settled:
        model = {"mu": None, "sigma": None}
        out_markets = [dict(_market_summary(m), prob=None, ev_yes=None, ev_no=None)
                       for m in markets]
    else:
        from model import ModelInputs, compute
        from lab.configs import LIVE_TODAY
        inputs = ModelInputs(
            series=series, event_ticker=ev["event_ticker"], target_date=target,
            forecasts={"ecmwf": ecmwf, "gfs": gfs, "nws": nws},
            metar_current=metar, today_max=today_max,
            markets=markets,
            decision_ts=int(time.time()),
            fetched_at=int(time.time()),
        )
        out = compute(inputs, LIVE_TODAY)
        if out.mu is None:
            model = {"mu": None, "sigma": None}
            out_markets = [dict(_market_summary(m), prob=None, ev_yes=None, ev_no=None)
                           for m in markets]
        else:
            sources_count = sum(1 for k in ("ecmwf", "gfs", "nws")
                                if forecasts.get(k) is not None)
            model = {"mu": out.mu, "mu_raw": out.mu_raw, "bias": out.bias_applied,
                     "sigma": out.sigma, "sources": sources_count,
                     "today_max": today_max, "truncation": out.truncation}
            out_markets = []
            for m in markets:
                prob = out.probs.get(m["ticker"])
                ya = to_float(m.get("yes_ask_dollars"))
                na = to_float(m.get("no_ask_dollars"))
                ev_yes = (prob - ya) if (prob is not None and ya not in (None, 0.0)) else None
                ev_no = ((1 - prob) - na) if (prob is not None and na not in (None, 0.0)) else None
                out_markets.append(dict(_market_summary(m), prob=prob,
                                        ev_yes=ev_yes, ev_no=ev_no))
```

Notable differences from current code:
- The `if not sources or settled` check is split: settled handles markets separately; the no-sources fallback is now inside `compute()` (returns `out.mu is None`).
- All model math moved into `compute(inputs, LIVE_TODAY)`.
- Dashboard JSON fields are unchanged: `mu`, `mu_raw`, `bias`, `sigma`, `sources`, `today_max`, `truncation`.

- [ ] **Step 3: Run all tests**

Run: `python -m pytest -v`
Expected: 100% pass, including all 10 baseline fixtures.

If baselines fail: do NOT proceed. Investigate the diff and fix either `compute()` or the wiring. The point of the baseline is to catch exactly this drift.

- [ ] **Step 4: Smoke-test the live dashboard**

Without restarting the live process, run the predict command directly to verify the output shape is unchanged:

```bash
python kalshi_temp.py predict KXHIGHNY-26MAY20 --json | python -m json.tool | head -40
```

Expected: same `model: {mu, sigma, sources, today_max, truncation}` shape and roughly the same numbers as before the extraction (small diff allowed; production fetches new live data on each call).

- [ ] **Step 5: Commit**

```bash
git add kalshi_temp.py
git commit -m "Extract build_event_data model math into model.compute."
```

- [ ] **Step 6: Restart live server (manual, with user approval)**

DO NOT automate this. Tell the user:

> "Tests are green and the extraction is committed. To pick up the change in the running live server, you need to restart it:
> ```
> # Find PID
> Get-NetTCPConnection -LocalPort 8765 -State Listen | % { Stop-Process -Id $_.OwningProcess }
> # Restart
> pythonw kalshi_temp.py serve --port 8765
> ```
> Wait for the dashboard to come back up before continuing."

---

## Task 12: Extract model math in `kalshi_temp.py::backtest_one_event`

**Files:**
- Modify: `kalshi_temp.py:1460-1500`

- [ ] **Step 1: Make the edit**

Locate `backtest_one_event` (around line 1425). Find the block that computes `mu_raw`, applies BIAS, and computes `sigma` (around lines 1464-1469). Replace from `mu_raw = weighted_mean(...)` through `ranked = []` setup (around line 1471) with:

```python
    from model import ModelInputs, compute
    from lab.configs import BACKTEST_TODAY

    inputs = ModelInputs(
        series=series, event_ticker=event_ticker, target_date=target_date,
        forecasts={"ecmwf": ecmwf, "gfs": gfs, "nws": None},
        metar_current=None, today_max=None,
        markets=markets,
        decision_ts=int(close_dt.timestamp() - lead_hours * 3600) if False else 0,
        fetched_at=0,
    )
    out = compute(inputs, BACKTEST_TODAY)
    if out.mu is None:
        return {"event": event_ticker, "skipped": "no_probability"}
    mu, sigma = out.mu, out.sigma

    ranked = []
    for m in markets:
        p = out.probs.get(m["ticker"])
        if p is not None:
            ranked.append((m, p))
```

Wait — `close_dt` is defined a few lines later in the original code. Move the `close_dt` parsing block above the `compute()` call:

```python
    try:
        close_dt = datetime.fromisoformat(markets[0]["close_time"].replace("Z", "+00:00"))
    except Exception:
        return {"event": event_ticker, "skipped": "no_close_time"}

    inputs = ModelInputs(
        series=series, event_ticker=event_ticker, target_date=target_date,
        forecasts={"ecmwf": ecmwf, "gfs": gfs, "nws": None},
        metar_current=None, today_max=None,
        markets=markets,
        decision_ts=int(close_dt.timestamp() - lead_hours * 3600),
        fetched_at=0,
    )
    from model import compute
    from lab.configs import BACKTEST_TODAY
    out = compute(inputs, BACKTEST_TODAY)
    if out.mu is None:
        return {"event": event_ticker, "skipped": "no_probability"}
    mu, sigma = out.mu, out.sigma

    ranked = [(m, out.probs[m["ticker"]]) for m in markets if m["ticker"] in out.probs]
    if not ranked:
        return {"event": event_ticker, "skipped": "no_probability"}

    decision_ts = int(close_dt.timestamp() - lead_hours * 3600)
    winner_ticker = winner["ticker"]
```

Then delete the original `decision_ts = int(close_dt.timestamp() - lead_hours * 3600)` line lower down (it was duplicated above). Leave everything from `winner_ticker = ...` and the bucket-price fetch block UNCHANGED.

- [ ] **Step 2: Run all tests**

Run: `python -m pytest -v`
Expected: 100% pass.

- [ ] **Step 3: Smoke test backtest**

```bash
python kalshi_temp.py backtest --series KXHIGHNY --days 7
```

Expected: produces a normal backtest report. Numbers should be very close to (ideally identical to) a pre-extraction run, since `BACKTEST_TODAY` mirrors the old formula.

- [ ] **Step 4: Commit**

```bash
git add kalshi_temp.py
git commit -m "Extract backtest_one_event model math into model.compute."
```

---

## Task 13: `lab/data_cache.py` SQLite layer

**Files:**
- Create: `lab/data_cache.py`
- Create: `tests/test_data_cache.py`

- [ ] **Step 1: Write failing tests**

`tests/test_data_cache.py`:

```python
import time
from pathlib import Path

import pytest

from lab.data_cache import DataCache


@pytest.fixture
def cache(tmp_path):
    return DataCache(tmp_path / "cache.sqlite")


def test_set_get_roundtrip(cache):
    cache.set("k1", {"value": 42}, source="open_meteo:hist", target_date="2026-05-01")
    assert cache.get("k1", ttl=None) == {"value": 42}


def test_get_missing_returns_none(cache):
    assert cache.get("nope", ttl=None) is None


def test_ttl_expiry(cache):
    cache.set("k1", "v", source="t", target_date=None)
    assert cache.get("k1", ttl=300) == "v"
    # Force the row to look old
    cache._conn.execute("UPDATE fetches SET fetched_at = ?", (int(time.time()) - 1000,))
    cache._conn.commit()
    assert cache.get("k1", ttl=300) is None


def test_ttl_none_means_forever(cache):
    cache.set("k1", "v", source="t", target_date="2020-01-01")
    cache._conn.execute("UPDATE fetches SET fetched_at = ?", (0,))
    cache._conn.commit()
    assert cache.get("k1", ttl=None) == "v"


def test_purge_before_date(cache):
    cache.set("k_old", "old", source="t", target_date="2025-01-01")
    cache.set("k_new", "new", source="t", target_date="2026-06-01")
    n = cache.purge_before("2026-01-01")
    assert n == 1
    assert cache.get("k_old", ttl=None) is None
    assert cache.get("k_new", ttl=None) == "new"


def test_stats(cache):
    cache.set("k1", "v", source="t", target_date=None)
    cache.set("k2", "v", source="t", target_date=None)
    s = cache.stats()
    assert s["total"] == 2
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_data_cache.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `lab/data_cache.py`**

```python
"""SQLite-backed cache for lab inputs.

Stores a single table of (key, json-value, fetched_at, source, target_date).
Reads check per-source TTL (the caller supplies it). Writes overwrite.
"""
import json
import sqlite3
import time
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS fetches (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    target_date TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_date ON fetches(source, target_date);
"""


class DataCache:
    def __init__(self, path: str | Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.executescript(_SCHEMA)

    def get(self, key: str, *, ttl: int | None) -> Any | None:
        """Return the cached value or None.

        `ttl` is per-source max age in seconds. None = no expiry.
        """
        row = self._conn.execute(
            "SELECT value, fetched_at FROM fetches WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        value_json, fetched_at = row
        if ttl is not None and (time.time() - fetched_at) > ttl:
            return None
        return json.loads(value_json)

    def set(self, key: str, value: Any, *, source: str, target_date: str | None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO fetches (key, value, fetched_at, source, target_date) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, json.dumps(value), int(time.time()), source, target_date),
        )

    def purge_before(self, target_date: str) -> int:
        cur = self._conn.execute(
            "DELETE FROM fetches WHERE target_date IS NOT NULL AND target_date < ?",
            (target_date,),
        )
        return cur.rowcount

    def stats(self) -> dict:
        total = self._conn.execute("SELECT COUNT(*) FROM fetches").fetchone()[0]
        by_source = dict(self._conn.execute(
            "SELECT source, COUNT(*) FROM fetches GROUP BY source"
        ).fetchall())
        oldest = self._conn.execute("SELECT MIN(fetched_at) FROM fetches").fetchone()[0]
        return {"total": total, "by_source": by_source, "oldest": oldest}

    def close(self) -> None:
        self._conn.close()


# Module-level singleton for production lab use.
_DEFAULT_PATH = Path(__file__).resolve().parent / ".cache.sqlite"
_default: DataCache | None = None


def default() -> DataCache:
    global _default
    if _default is None:
        _default = DataCache(_DEFAULT_PATH)
    return _default
```

- [ ] **Step 4: Run, verify passes**

Run: `python -m pytest tests/test_data_cache.py -v`
Expected: 6/6 pass.

- [ ] **Step 5: Commit**

```bash
git add lab/data_cache.py tests/test_data_cache.py
git commit -m "Add SQLite-backed input cache for the lab harness."
```

---

## Task 14: `lab/inputs.py` — assemble `ModelInputs` through the cache

**Files:**
- Create: `lab/inputs.py`

- [ ] **Step 1: Implement `lab/inputs.py`**

```python
"""Assemble ModelInputs for a settled event, going through DataCache.

Used by lab.replay (settled events, historical forecasts) and by future
shadow-mode analysis if needed.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import kalshi_temp as kt

from model import ModelInputs

from .data_cache import DataCache, default


# Per-source TTLs in seconds (see spec §11). None = forever.
TTL = {
    "open_meteo:hist": None,
    "open_meteo:live": 300,
    "nws": 300,
    "metar_hist": None,
    "kalshi_event_markets": 60,
}


def fetch_historical_open_meteo(lat: float, lon: float, target_date: str,
                                 model: str, cache: DataCache) -> float | None:
    key = f"open_meteo:hist:{model}:{lat:.4f},{lon:.4f}:{target_date}"
    cached = cache.get(key, ttl=TTL["open_meteo:hist"])
    if cached is not None:
        return cached.get("value")
    v = kt.fetch_open_meteo(lat, lon, target_date, model, historical=True)
    cache.set(key, {"value": v}, source="open_meteo:hist", target_date=target_date)
    return v


def fetch_event_markets(event_ticker: str, cache: DataCache) -> list[dict]:
    key = f"kalshi_event_markets:{event_ticker}"
    cached = cache.get(key, ttl=TTL["kalshi_event_markets"])
    if cached is not None:
        return cached
    markets = kt.kalshi_get("/markets", {"event_ticker": event_ticker, "limit": 200}
                            ).get("markets", []) or []
    cache.set(key, markets, source="kalshi_event_markets", target_date=None)
    return markets


def build_historical_inputs(event_ticker: str, *, cache: DataCache | None = None) -> ModelInputs | None:
    """Build ModelInputs for a settled event using cached historical forecasts.

    Returns None if the event is unsupported or fetches fail.
    """
    cache = cache or default()
    series = event_ticker.split("-")[0]
    city = kt.CITIES.get(series)
    if not city:
        return None
    markets = fetch_event_markets(event_ticker, cache)
    if not markets:
        return None
    target_date = kt.event_local_date({"event_ticker": event_ticker,
                                       "strike_date": markets[0].get("close_time", "")})
    try:
        close_dt = datetime.fromisoformat(markets[0]["close_time"].replace("Z", "+00:00"))
    except Exception:
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_ec = pool.submit(fetch_historical_open_meteo, city["lat"], city["lon"],
                           target_date, "ecmwf_ifs025", cache)
        f_gfs = pool.submit(fetch_historical_open_meteo, city["lat"], city["lon"],
                            target_date, "gfs_seamless", cache)
        ecmwf, gfs = f_ec.result(), f_gfs.result()

    return ModelInputs(
        series=series, event_ticker=event_ticker, target_date=target_date,
        forecasts={"ecmwf": ecmwf, "gfs": gfs, "nws": None},
        metar_current=None, today_max=None,
        markets=markets,
        decision_ts=int(close_dt.timestamp()),  # caller passes a lead-shifted value via replay
        fetched_at=int(datetime.now(timezone.utc).timestamp()),
    )


def winner_of(markets: list[dict]) -> dict | None:
    return next((m for m in markets if m.get("result") == "yes"), None)
```

- [ ] **Step 2: Run all tests to confirm no regressions**

Run: `python -m pytest -v`
Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add lab/inputs.py
git commit -m "Add lab.inputs: assemble ModelInputs through the data cache."
```

---

## Task 15: `lab/replay.py` — run a config over events

**Files:**
- Create: `lab/replay.py`
- Create: `tests/test_replay.py` (smoke test only — full coverage is from real-event integration)

- [ ] **Step 1: Implement `lab/replay.py`**

```python
"""Replay a ModelConfig over a list of settled event tickers.

For each event:
    - build inputs from cached/fresh sources
    - run compute(inputs, cfg)
    - pick the highest-prob bucket (the YES strategy)
    - look up the entry price at T-cfg.decision_lead_hours
    - resolve win/loss against the settled winner
"""
from dataclasses import dataclass
from typing import Iterable

import kalshi_temp as kt

from model import ModelConfig, compute

from .data_cache import default
from .inputs import build_historical_inputs, winner_of


@dataclass
class ReplayRecord:
    event_ticker: str
    series: str
    target_date: str
    mu: float | None
    sigma: float | None
    pick_ticker: str | None
    pick_prob: float | None
    pick_bucket: str | None
    entry_price: float | None
    won: bool | None
    pnl: float | None
    skip: str | None


def _kalshi_candle_yes_ask_at(series: str, ticker: str, decision_ts: int) -> float | None:
    """Read yes_ask from the latest candle <= decision_ts."""
    return kt.yes_ask_at(series, ticker, decision_ts)


def replay_one(event_ticker: str, cfg: ModelConfig) -> ReplayRecord:
    inputs = build_historical_inputs(event_ticker)
    if inputs is None:
        return ReplayRecord(event_ticker, "", "", None, None, None, None, None, None, None, None, "no_inputs")

    winner = winner_of(list(inputs.markets))
    if winner is None:
        return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                            None, None, None, None, None, None, None, None, "not_settled")

    out = compute(inputs, cfg)
    if out.mu is None or not out.probs:
        return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                            None, None, None, None, None, None, None, None, "no_model")

    # decision_ts = close - lead_hours
    from datetime import datetime
    close_dt = datetime.fromisoformat(inputs.markets[0]["close_time"].replace("Z", "+00:00"))
    decision_ts = int(close_dt.timestamp() - cfg.decision_lead_hours * 3600)

    pick_ticker = max(out.probs, key=lambda t: out.probs[t])
    pick_prob = out.probs[pick_ticker]
    pick_market = next((m for m in inputs.markets if m["ticker"] == pick_ticker), None)
    pick_bucket = (pick_market.get("subtitle") if pick_market else None) or "?"

    yes_ask = _kalshi_candle_yes_ask_at(inputs.series, pick_ticker, decision_ts)
    if yes_ask is None or yes_ask <= 0 or yes_ask >= 1:
        return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                            out.mu, out.sigma, pick_ticker, pick_prob, pick_bucket,
                            None, None, None, "no_price")

    won = pick_ticker == winner["ticker"]
    pnl = (1.0 - yes_ask) if won else -yes_ask
    return ReplayRecord(event_ticker, inputs.series, inputs.target_date,
                        out.mu, out.sigma, pick_ticker, pick_prob, pick_bucket,
                        yes_ask, won, pnl, None)


def replay_many(event_tickers: Iterable[str], cfg: ModelConfig) -> list[ReplayRecord]:
    return [replay_one(t, cfg) for t in event_tickers]


def summarize(records: list[ReplayRecord]) -> dict:
    bets = [r for r in records if r.pnl is not None]
    if not bets:
        return {"events": len(records), "bets": 0, "wins": 0, "win_rate": 0.0,
                "total_pnl": 0.0, "avg_pnl": 0.0, "max_drawdown": 0.0}
    wins = sum(1 for r in bets if r.won)
    pnls = [r.pnl for r in bets]
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return {
        "events": len(records), "bets": len(bets), "wins": wins,
        "win_rate": wins / len(bets), "total_pnl": sum(pnls),
        "avg_pnl": sum(pnls) / len(bets), "max_drawdown": max_dd,
    }
```

- [ ] **Step 2: Quick smoke test**

```bash
python -c "from lab.replay import replay_many, summarize; from lab.configs import LIVE_TODAY; r = replay_many(['KXHIGHNY-26MAY15'], LIVE_TODAY); print(r); print(summarize(r))"
```

Expected: prints a `ReplayRecord` and a summary dict.

- [ ] **Step 3: Commit**

```bash
git add lab/replay.py
git commit -m "Add lab.replay: replay a config over settled events."
```

---

## Task 16: `lab/configs.py` part 2 — minus-one variants

**Files:**
- Modify: `lab/configs.py`

- [ ] **Step 1: Add variant constants**

Append to `lab/configs.py`:

```python
from dataclasses import replace


LIVE_MINUS_NWS = replace(LIVE_TODAY,
    name="live-minus-nws",
    nws_blend=0.0,
    sigma_sources=("ecmwf", "gfs"),
)


LIVE_MINUS_TRUNC = replace(LIVE_TODAY,
    name="live-minus-trunc",
    today_max_mode="off",
)


LIVE_MINUS_PUSH = replace(LIVE_TODAY,
    name="live-minus-push",
    today_max_mode="truncate",   # keep truncation, drop the +0.3 push
)


LIVE_AT_T24_STRICT = replace(LIVE_TODAY,
    name="live-at-t24-strict",
    decision_lead_hours=24.0,    # documents intent even though LIVE_TODAY already is 24.0
)


_BY_NAME = {c.name: c for c in (
    LIVE_TODAY, BACKTEST_TODAY,
    LIVE_MINUS_NWS, LIVE_MINUS_TRUNC, LIVE_MINUS_PUSH, LIVE_AT_T24_STRICT,
)}
```

(Overwrites the earlier `_BY_NAME` definition; that's intentional.)

- [ ] **Step 2: Run tests to verify nothing broke**

Run: `python -m pytest -v`
Expected: all green. `test_configs.py` still passes because the LIVE_TODAY/BACKTEST_TODAY definitions didn't change.

- [ ] **Step 3: Commit**

```bash
git add lab/configs.py
git commit -m "Add LIVE_MINUS_NWS / TRUNC / PUSH / AT_T24_STRICT variants."
```

---

## Task 17: `lab/cli.py` scaffold + `replay` subcommand

**Files:**
- Create: `lab/__main__.py`
- Create: `lab/cli.py`

- [ ] **Step 1: Create `lab/__main__.py`**

```python
"""Allow `python -m lab ...`"""
from .cli import main

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Create `lab/cli.py`**

```python
"""argparse routing for the model lab."""
import argparse
import json
import sys

from . import configs as _configs
from .replay import replay_many, summarize


def _resolve_events(args) -> list[str]:
    import kalshi_temp as kt
    if args.events:
        return [t.strip() for t in args.events.split(",") if t.strip()]
    if args.series:
        return [e["event_ticker"]
                for e in kt.list_events_for_series(args.series, args.days)]
    # all series
    tickers: list[str] = []
    for s in kt.CITIES:
        tickers.extend(e["event_ticker"]
                        for e in kt.list_events_for_series(s, args.days))
    return tickers


def cmd_replay(args):
    cfg = _configs.get(args.config)
    events = _resolve_events(args)
    if not events:
        sys.exit("no events resolved — pass --events, --series, or use the default all-series with --days")
    records = replay_many(events, cfg)
    if args.json:
        print(json.dumps({
            "config": cfg.name, "n_events": len(events),
            "summary": summarize(records),
            "records": [r.__dict__ for r in records],
        }, indent=2))
        return
    s = summarize(records)
    print(f"Config:  {cfg.name}")
    print(f"Events:  {len(events)}   Bets: {s['bets']}   Wins: {s['wins']}")
    if s["bets"]:
        print(f"WinRate: {s['win_rate']*100:.1f}%   Total PnL: ${s['total_pnl']:+.2f}   "
              f"Avg PnL: ${s['avg_pnl']:+.3f}   Max DD: ${s['max_drawdown']:.2f}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="lab", description="weatherbot model lab")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("replay", help="replay a config over events")
    pr.add_argument("--config", required=True, help="config name (e.g. live-today)")
    pr.add_argument("--days", type=int, default=30)
    pr.add_argument("--series", help="restrict to one series")
    pr.add_argument("--events", help="comma-separated event tickers (overrides --series)")
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_replay)

    return p


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Smoke test**

```bash
python -m lab replay --config live-today --series KXHIGHNY --days 7
```

Expected: prints a one-line summary.

- [ ] **Step 4: Commit**

```bash
git add lab/__main__.py lab/cli.py
git commit -m "Add lab CLI scaffold with replay subcommand."
```

---

## Task 18: `lab/compare.py` + `compare` subcommand

**Files:**
- Create: `lab/compare.py`
- Modify: `lab/cli.py`

- [ ] **Step 1: Implement `lab/compare.py`**

```python
"""Head-to-head A/B between two ModelConfigs over a set of events.

Bootstrap CI on PnL delta because raw point-estimates over ~30-100 events
can swing a lot from noise.
"""
import random
from dataclasses import dataclass, asdict
from typing import Iterable

from model import ModelConfig

from .replay import replay_many, summarize, ReplayRecord


@dataclass
class ComparisonResult:
    cfg_a: str
    cfg_b: str
    summary_a: dict
    summary_b: dict
    pnl_delta: float
    pnl_delta_ci: tuple[float, float]
    agreement_rate: float
    decision_flips: list[dict]


def _records_by_event(records: list[ReplayRecord]) -> dict[str, ReplayRecord]:
    return {r.event_ticker: r for r in records}


def _bootstrap_pnl_delta(
    a_records: list[ReplayRecord], b_records: list[ReplayRecord],
    n: int = 1000, seed: int = 17,
) -> tuple[float, tuple[float, float]]:
    a_by = _records_by_event(a_records)
    b_by = _records_by_event(b_records)
    events = sorted(set(a_by) & set(b_by))
    pairs = []
    for e in events:
        ap = a_by[e].pnl if a_by[e].pnl is not None else 0.0
        bp = b_by[e].pnl if b_by[e].pnl is not None else 0.0
        pairs.append(bp - ap)  # delta = B - A
    if not pairs:
        return 0.0, (0.0, 0.0)
    mean = sum(pairs) / len(pairs)
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        s = sum(rng.choice(pairs) for _ in range(len(pairs))) / len(pairs)
        samples.append(s)
    samples.sort()
    lo = samples[int(0.025 * n)]
    hi = samples[int(0.975 * n)]
    return mean * len(pairs), (lo * len(pairs), hi * len(pairs))


def compare(events: Iterable[str], cfg_a: ModelConfig, cfg_b: ModelConfig,
            *, bootstrap: int = 1000) -> ComparisonResult:
    events = list(events)
    a = replay_many(events, cfg_a)
    b = replay_many(events, cfg_b)

    sum_a, sum_b = summarize(a), summarize(b)

    a_by = _records_by_event(a)
    b_by = _records_by_event(b)
    common = sorted(set(a_by) & set(b_by))
    flips = []
    same = 0
    for e in common:
        if a_by[e].pick_ticker is None or b_by[e].pick_ticker is None:
            continue
        if a_by[e].pick_ticker == b_by[e].pick_ticker:
            same += 1
        else:
            flips.append({
                "event": e,
                "a_pick": a_by[e].pick_bucket, "a_prob": a_by[e].pick_prob,
                "a_mu": a_by[e].mu, "a_sigma": a_by[e].sigma,
                "b_pick": b_by[e].pick_bucket, "b_prob": b_by[e].pick_prob,
                "b_mu": b_by[e].mu, "b_sigma": b_by[e].sigma,
            })
    n_both_picked = same + len(flips)
    agreement = same / n_both_picked if n_both_picked else 0.0

    delta, ci = _bootstrap_pnl_delta(a, b, n=bootstrap)
    return ComparisonResult(
        cfg_a=cfg_a.name, cfg_b=cfg_b.name,
        summary_a=sum_a, summary_b=sum_b,
        pnl_delta=delta, pnl_delta_ci=ci,
        agreement_rate=agreement, decision_flips=flips,
    )
```

- [ ] **Step 2: Add the `compare` subcommand**

Edit `lab/cli.py` and add:

```python
from .compare import compare as run_compare


def cmd_compare(args):
    cfg_a = _configs.get(args.cfg_a)
    cfg_b = _configs.get(args.cfg_b)
    events = _resolve_events(args)
    if not events:
        sys.exit("no events resolved")
    result = run_compare(events, cfg_a, cfg_b, bootstrap=args.bootstrap)
    if args.json:
        import dataclasses
        print(json.dumps(dataclasses.asdict(result), indent=2))
        return
    print(f"A: {result.cfg_a}   B: {result.cfg_b}   Events: {len(events)}")
    print(f"  A: bets {result.summary_a['bets']}  WR {result.summary_a['win_rate']*100:.1f}%  PnL ${result.summary_a['total_pnl']:+.2f}  DD ${result.summary_a['max_drawdown']:.2f}")
    print(f"  B: bets {result.summary_b['bets']}  WR {result.summary_b['win_rate']*100:.1f}%  PnL ${result.summary_b['total_pnl']:+.2f}  DD ${result.summary_b['max_drawdown']:.2f}")
    lo, hi = result.pnl_delta_ci
    print(f"  PnL delta (B - A): ${result.pnl_delta:+.2f}   95% CI [${lo:+.2f}, ${hi:+.2f}]")
    print(f"  Agreement: {result.agreement_rate*100:.1f}%   Decision flips: {len(result.decision_flips)}")
```

In `build_parser()`:

```python
    cp = sub.add_parser("compare", help="A/B between two configs")
    cp.add_argument("cfg_a")
    cp.add_argument("cfg_b")
    cp.add_argument("--days", type=int, default=30)
    cp.add_argument("--series")
    cp.add_argument("--events")
    cp.add_argument("--bootstrap", type=int, default=1000)
    cp.add_argument("--json", action="store_true")
    cp.set_defaults(func=cmd_compare)
```

- [ ] **Step 3: Smoke test**

```bash
python -m lab compare live-today live-minus-nws --series KXHIGHNY --days 14
```

Expected: prints a multi-line A/B summary.

- [ ] **Step 4: Commit**

```bash
git add lab/compare.py lab/cli.py
git commit -m "Add lab.compare and CLI compare subcommand with bootstrap CI."
```

---

## Task 19: `lab/decompose.py` + `decompose` subcommand

**Files:**
- Create: `lab/decompose.py`
- Modify: `lab/cli.py`

- [ ] **Step 1: Implement `lab/decompose.py`**

```python
"""Per-event prob/PnL attribution across multiple minus-one variants of LIVE_TODAY.

For each variant V:
    delta_prob(event)  = P_LIVE(winner) - P_V(winner)
    delta_pnl(event)   = pnl_LIVE - pnl_V
Aggregated across events, this attributes the live-vs-V gap to the component
that V removed.
"""
from typing import Iterable

from model import ModelConfig

from . import configs as _configs
from .replay import replay_many


def decompose(events: Iterable[str], baseline: ModelConfig,
              variants: list[ModelConfig]) -> dict:
    events = list(events)
    base_records = {r.event_ticker: r for r in replay_many(events, baseline)}
    out_per_variant: list[dict] = []
    for v in variants:
        v_records = {r.event_ticker: r for r in replay_many(events, v)}
        rows = []
        total_pnl_delta = 0.0
        n_flips = 0
        for e in events:
            br = base_records.get(e)
            vr = v_records.get(e)
            if br is None or vr is None:
                continue
            base_pnl = br.pnl if br.pnl is not None else 0.0
            v_pnl = vr.pnl if vr.pnl is not None else 0.0
            total_pnl_delta += base_pnl - v_pnl
            if br.pick_ticker and vr.pick_ticker and br.pick_ticker != vr.pick_ticker:
                n_flips += 1
            rows.append({
                "event": e,
                "base_pick": br.pick_bucket, "base_prob": br.pick_prob, "base_pnl": br.pnl,
                "var_pick": vr.pick_bucket, "var_prob": vr.pick_prob, "var_pnl": vr.pnl,
            })
        out_per_variant.append({
            "variant": v.name, "pnl_delta_attrib": total_pnl_delta,
            "decision_flips": n_flips, "rows": rows,
        })
    return {"baseline": baseline.name, "variants": out_per_variant}
```

- [ ] **Step 2: Add CLI subcommand to `lab/cli.py`**

```python
from .decompose import decompose as run_decompose


_DEFAULT_VARIANTS = ["live-minus-nws", "live-minus-trunc", "live-minus-push"]


def cmd_decompose(args):
    baseline = _configs.get(args.against)
    variant_names = (args.variants.split(",") if args.variants
                     else _DEFAULT_VARIANTS)
    variants = [_configs.get(n) for n in variant_names]
    events = _resolve_events(args)
    if not events:
        sys.exit("no events resolved")
    result = run_decompose(events, baseline, variants)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"Decomposing {baseline.name} vs {len(variants)} variants over {len(events)} events")
    for v in result["variants"]:
        print(f"  {v['variant']:<22}  attrib ${v['pnl_delta_attrib']:+.2f}   flips {v['decision_flips']}")
```

And in `build_parser()`:

```python
    dp = sub.add_parser("decompose", help="prob/PnL attribution across variants")
    dp.add_argument("--against", default="live-today")
    dp.add_argument("--variants", help="comma-separated names; default = minus-nws,minus-trunc,minus-push")
    dp.add_argument("--days", type=int, default=30)
    dp.add_argument("--series")
    dp.add_argument("--events")
    dp.add_argument("--json", action="store_true")
    dp.set_defaults(func=cmd_decompose)
```

- [ ] **Step 3: Smoke test**

```bash
python -m lab decompose --days 14
```

Expected: prints a one-liner per variant with attribution.

- [ ] **Step 4: Commit**

```bash
git add lab/decompose.py lab/cli.py
git commit -m "Add lab.decompose and CLI subcommand for component attribution."
```

---

## Task 20: `lab/refit_bias.py` + `refit-bias` subcommand

**Files:**
- Create: `lab/refit_bias.py`
- Modify: `lab/cli.py`

- [ ] **Step 1: Implement `lab/refit_bias.py`**

```python
"""Refit the BIAS table for an arbitrary ModelConfig using settled events.

For each event:
    mu_pre_bias = compute(inputs, replace(cfg, bias_table={}))     # zero-bias model
    actual      = bucket_midpoint(winning_market)
    error       = mu_pre_bias - actual

Per-city bias = mean(error) over events for that city.

Print a paste-ready dict. Does NOT modify lab/configs.py.
"""
from dataclasses import replace
import statistics

from model import ModelConfig, compute

from .inputs import build_historical_inputs, winner_of


def actual_high_midpoint(winner: dict) -> float | None:
    st = winner.get("strike_type")
    floor, cap = winner.get("floor_strike"), winner.get("cap_strike")
    if st == "between" and floor is not None and cap is not None:
        return (floor + cap) / 2.0
    if st == "less" and cap is not None:
        return cap - 1.0
    if st == "greater" and floor is not None:
        return floor + 1.0
    return None


def refit(event_tickers: list[str], cfg: ModelConfig) -> dict[str, dict]:
    """Returns {series: {bias, n, sd}}."""
    zero_bias_cfg = replace(cfg, name=f"{cfg.name}__nobias", bias_table={})
    per_series: dict[str, list[float]] = {}
    for t in event_tickers:
        inputs = build_historical_inputs(t)
        if inputs is None:
            continue
        winner = winner_of(list(inputs.markets))
        if winner is None:
            continue
        actual = actual_high_midpoint(winner)
        if actual is None:
            continue
        out = compute(inputs, zero_bias_cfg)
        if out.mu is None:
            continue
        per_series.setdefault(inputs.series, []).append(out.mu - actual)

    table: dict[str, dict] = {}
    for s, errs in per_series.items():
        if not errs:
            continue
        bias = statistics.mean(errs)
        sd = statistics.pstdev(errs) if len(errs) > 1 else 0.0
        table[s] = {"bias": round(bias, 2), "n": len(errs), "sd": round(sd, 2)}
    return table
```

- [ ] **Step 2: Add CLI subcommand**

In `lab/cli.py`:

```python
from .refit_bias import refit as run_refit


def cmd_refit_bias(args):
    cfg = _configs.get(args.config)
    events = _resolve_events(args)
    table = run_refit(events, cfg)
    if args.json:
        print(json.dumps(table, indent=2))
        return
    print(f"# refit-bias output for config={cfg.name}, events={len(events)}")
    print("BIAS = {")
    for s, row in sorted(table.items()):
        print(f"    {s!r:<14}: {row['bias']:+6.2f},  # n={row['n']}, sd={row['sd']:.2f}")
    print("}")
```

In `build_parser()`:

```python
    rb = sub.add_parser("refit-bias", help="refit BIAS table for a config")
    rb.add_argument("--config", required=True)
    rb.add_argument("--days", type=int, default=60)
    rb.add_argument("--series")
    rb.add_argument("--events")
    rb.add_argument("--json", action="store_true")
    rb.set_defaults(func=cmd_refit_bias)
```

- [ ] **Step 3: Smoke test**

```bash
python -m lab refit-bias --config live-today --days 14
```

Expected: prints a BIAS dict.

- [ ] **Step 4: Commit**

```bash
git add lab/refit_bias.py lab/cli.py
git commit -m "Add lab.refit_bias and CLI subcommand."
```

---

## Task 21: `cache` CLI subcommand

**Files:**
- Modify: `lab/cli.py`

- [ ] **Step 1: Add cache CLI**

In `lab/cli.py`:

```python
from .data_cache import default as default_cache


def cmd_cache(args):
    cache = default_cache()
    if args.action == "stats":
        s = cache.stats()
        if args.json:
            print(json.dumps(s, indent=2))
        else:
            print(f"Total rows: {s['total']}   Oldest: {s['oldest']}")
            for src, n in sorted(s["by_source"].items()):
                print(f"  {src:<28} {n}")
    elif args.action == "purge":
        n = cache.purge_before(args.before)
        print(f"Purged {n} rows with target_date < {args.before}")
    elif args.action == "warm":
        sys.exit("warm is not yet implemented (TODO follow-up)")
```

In `build_parser()`:

```python
    cc = sub.add_parser("cache", help="manage the lab data cache")
    cc.add_argument("action", choices=["stats", "purge", "warm"])
    cc.add_argument("--before", help="for purge: target_date cutoff (YYYY-MM-DD)")
    cc.add_argument("--json", action="store_true")
    cc.set_defaults(func=cmd_cache)
```

- [ ] **Step 2: Smoke test**

```bash
python -m lab cache stats
```

Expected: prints cache stats.

- [ ] **Step 3: Commit**

```bash
git add lab/cli.py
git commit -m "Add cache CLI subcommand."
```

---

## Task 22: `shadow/` package — runner + active list + isolation test

**Files:**
- Create: `shadow/__init__.py`
- Create: `shadow/runner.py`
- Create: `shadow/active.py`
- Create: `tests/test_shadow_isolation.py`

- [ ] **Step 1: Write failing isolation test**

`tests/test_shadow_isolation.py`:

```python
"""A shadow exception must NOT alter the live ModelOutput or crash the caller."""
import json
import time
from pathlib import Path

from dataclasses import replace

import pytest

from model import ModelInputs, compute
from lab.configs import LIVE_TODAY, LIVE_MINUS_NWS


def _basic_inputs():
    return ModelInputs(
        series="KXHIGHNY", event_ticker="TEST-ISOLATION",
        target_date="2026-05-20",
        forecasts={"ecmwf": 72.0, "gfs": 70.0, "nws": None},
        metar_current=None, today_max=None,
        markets=[{"ticker": "T-MID", "strike_type": "between",
                  "floor_strike": 70, "cap_strike": 72}],
        decision_ts=int(time.time()), fetched_at=int(time.time()),
    )


def test_shadow_exception_doesnt_affect_live(tmp_path, monkeypatch):
    """If a shadow config raises, the live output must be unchanged."""
    from shadow import runner
    # Point SHADOW_PATH at a temp file.
    monkeypatch.setattr(runner, "SHADOW_PATH", str(tmp_path / "shadow.jsonl"))

    inputs = _basic_inputs()
    live_out = compute(inputs, LIVE_TODAY)

    # Craft a config that will blow up compute() (None weights raises in dict.get).
    bad_cfg = replace(LIVE_TODAY, name="will-explode",
                      source_weights=None)  # type: ignore[arg-type]
    runner.run_shadow(inputs, [bad_cfg])

    # Live recomputation must produce identical output to before.
    live_out_after = compute(inputs, LIVE_TODAY)
    assert live_out.mu == live_out_after.mu
    assert dict(live_out.probs) == dict(live_out_after.probs)


def test_shadow_writes_jsonl(tmp_path, monkeypatch):
    from shadow import runner
    monkeypatch.setattr(runner, "SHADOW_PATH", str(tmp_path / "shadow.jsonl"))
    inputs = _basic_inputs()
    runner.run_shadow(inputs, [LIVE_MINUS_NWS])
    rows = [json.loads(line) for line in
            Path(tmp_path / "shadow.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["config_name"] == "live-minus-nws"
    assert rows[0]["event_ticker"] == "TEST-ISOLATION"
    assert "code_version" in rows[0]
```

- [ ] **Step 2: Run, verify failure**

Run: `python -m pytest tests/test_shadow_isolation.py -v`
Expected: ImportError on `shadow`.

- [ ] **Step 3: Implement the package**

`shadow/__init__.py`:

```python
"""shadow/: live A/B logger. run_shadow(inputs, configs) appends one
JSONL row per config to shadow_picks.jsonl. Failures are isolated.
"""
```

`shadow/runner.py`:

```python
import json
import sys
from datetime import datetime, timezone
from typing import Sequence

from model import ModelInputs, ModelConfig, compute


SHADOW_PATH = "shadow_picks.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _top_pick(out, markets: Sequence[dict]):
    """Return (ticker, market_dict, prob) for the highest-prob bucket, or None."""
    if not out.probs:
        return None
    t = max(out.probs, key=lambda k: out.probs[k])
    m = next((m for m in markets if m["ticker"] == t), None)
    return t, m, out.probs[t]


def run_shadow(inputs: ModelInputs, configs: Sequence[ModelConfig]) -> None:
    """Compute each config against `inputs`; append one JSONL row per config.

    Any exception from compute() or pick-resolution is caught and logged to
    stderr. Never raises.
    """
    ts = _now_iso()
    for cfg in configs:
        try:
            out = compute(inputs, cfg)
            pick = _top_pick(out, inputs.markets)
            if pick is None:
                continue
            tkr, m, p = pick
            yes_ask = (m or {}).get("yes_ask_dollars")
            no_ask = (m or {}).get("no_ask_dollars")
            yes_ask_f = float(yes_ask) if yes_ask else None
            no_ask_f = float(no_ask) if no_ask else None
            ev_yes = (p - yes_ask_f) if yes_ask_f is not None else None
            ev_no = ((1 - p) - no_ask_f) if no_ask_f is not None else None
            row = {
                "ts": ts,
                "event_ticker": inputs.event_ticker,
                "config_name": cfg.name,
                "code_version": out.code_version,
                "mu": out.mu, "sigma": out.sigma, "mu_raw": out.mu_raw,
                "bias_applied": out.bias_applied,
                "truncation": out.truncation,
                "today_max_active": out.today_max_active,
                "pick_ticker": tkr, "pick_prob": p,
                "pick_yes_ask": yes_ask_f, "pick_no_ask": no_ask_f,
                "ev_yes": ev_yes, "ev_no": ev_no,
            }
            with open(SHADOW_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception as e:
            sys.stderr.write(f"[shadow:{cfg.name}] {e}\n")
```

`shadow/active.py`:

```python
"""Edit this list to control which configs run alongside live each refresh.

To add a shadow: append a ModelConfig; restart `kalshi_temp serve`.
To remove a shadow: remove it from the list; restart.
"""
from lab.configs import LIVE_MINUS_NWS, LIVE_MINUS_PUSH, LIVE_MINUS_TRUNC


ACTIVE_SHADOWS = [LIVE_MINUS_NWS, LIVE_MINUS_PUSH, LIVE_MINUS_TRUNC]
```

- [ ] **Step 4: Run, verify passes**

Run: `python -m pytest tests/test_shadow_isolation.py -v`
Expected: 2/2 pass.

- [ ] **Step 5: Commit**

```bash
git add shadow/__init__.py shadow/runner.py shadow/active.py tests/test_shadow_isolation.py
git commit -m "Add shadow package with run_shadow and isolation test."
```

---

## Task 23: Wire `run_shadow` into `kalshi_temp.py::build_event_data`

**Files:**
- Modify: `kalshi_temp.py` (inside `build_event_data`)

- [ ] **Step 1: Edit `kalshi_temp.py`**

In `build_event_data`, after `compute(inputs, LIVE_TODAY)` returns but only when `out.mu is not None` (i.e. we have a model), add:

```python
            # Shadow A/B: compute candidate configs on the same inputs.
            try:
                from shadow.runner import run_shadow
                from shadow.active import ACTIVE_SHADOWS
                run_shadow(inputs, ACTIVE_SHADOWS)
            except Exception as e:
                print(f"[shadow] {e}", file=sys.stderr)
```

Place this right after the `out_markets = []` loop assembly, before the function returns.

- [ ] **Step 2: Run tests**

Run: `python -m pytest -v`
Expected: all green.

- [ ] **Step 3: Smoke test**

```bash
python kalshi_temp.py predict KXHIGHNY-26MAY20 --json > /tmp/predict.json
cat shadow_picks.jsonl | head -5
```

Expected: predict command works; shadow_picks.jsonl now contains rows.

- [ ] **Step 4: Commit**

```bash
git add kalshi_temp.py
git commit -m "Wire run_shadow into build_event_data on every dashboard refresh."
```

- [ ] **Step 5: Restart live server (manual)**

Tell the user the same restart instructions as Task 11 Step 6.

---

## Task 24: `lab/shadow_summary.py` + CLI subcommand

**Files:**
- Create: `lab/shadow_summary.py`
- Modify: `lab/cli.py`

- [ ] **Step 1: Implement `lab/shadow_summary.py`**

```python
"""Summarize shadow_picks.jsonl: join to settled outcomes and report A/B."""
import json
from collections import defaultdict
from pathlib import Path

import kalshi_temp as kt

from .inputs import winner_of, fetch_event_markets
from .data_cache import default as default_cache


def _read_shadow(path: Path, *, since_days: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    if since_days is not None:
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
        rows = [r for r in rows
                if datetime.fromisoformat(r["ts"]).timestamp() >= cutoff.timestamp()]
    return rows


def summarize(config_name: str, *, days: int = 14,
              shadow_path: str = "shadow_picks.jsonl") -> dict:
    """For each event that this config saw at least once, take the LATEST
    shadow row, look up the settled winner, compute pnl using the recorded
    pick_yes_ask. Aggregate."""
    rows = _read_shadow(Path(shadow_path), since_days=days)
    rows = [r for r in rows if r["config_name"] == config_name]
    by_event: dict[str, dict] = {}
    for r in rows:
        prev = by_event.get(r["event_ticker"])
        if prev is None or r["ts"] > prev["ts"]:
            by_event[r["event_ticker"]] = r
    cache = default_cache()
    bets, wins, pnls, skips = [], 0, [], defaultdict(int)
    for ev, r in by_event.items():
        markets = fetch_event_markets(ev, cache)
        winner = winner_of(markets)
        if winner is None:
            skips["not_settled"] += 1
            continue
        if r["pick_yes_ask"] is None or r["pick_yes_ask"] <= 0 or r["pick_yes_ask"] >= 1:
            skips["no_price"] += 1
            continue
        won = r["pick_ticker"] == winner["ticker"]
        if won:
            wins += 1
            pnls.append(1.0 - r["pick_yes_ask"])
        else:
            pnls.append(-r["pick_yes_ask"])
        bets.append({"event": ev, "won": won, "pnl": pnls[-1],
                     "pick": r["pick_ticker"], "entry": r["pick_yes_ask"]})
    summary = {
        "config": config_name, "events_seen": len(by_event),
        "bets": len(bets), "wins": wins,
        "win_rate": (wins / len(bets)) if bets else 0.0,
        "total_pnl": sum(pnls),
        "skips": dict(skips),
    }
    return {"summary": summary, "bets": bets}
```

- [ ] **Step 2: Add CLI subcommand**

In `lab/cli.py`:

```python
from .shadow_summary import summarize as run_shadow_summary


def cmd_shadow_summary(args):
    result = run_shadow_summary(args.config, days=args.days)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    s = result["summary"]
    print(f"Config: {s['config']}   Days: {args.days}")
    print(f"  Events seen: {s['events_seen']}   Bets: {s['bets']}   "
          f"WR: {s['win_rate']*100:.1f}%   PnL: ${s['total_pnl']:+.2f}")
    if s["skips"]:
        print(f"  Skips: " + ", ".join(f"{n}× {k}" for k, n in s["skips"].items()))
```

In `build_parser()`:

```python
    ss = sub.add_parser("shadow-summary", help="A/B report from shadow_picks.jsonl")
    ss.add_argument("--config", required=True)
    ss.add_argument("--days", type=int, default=14)
    ss.add_argument("--json", action="store_true")
    ss.set_defaults(func=cmd_shadow_summary)
```

- [ ] **Step 3: Smoke test**

```bash
python -m lab shadow-summary --config live-minus-push --days 14
```

Expected: prints a summary (may be sparse if shadow has only been running briefly).

- [ ] **Step 4: Commit**

```bash
git add lab/shadow_summary.py lab/cli.py
git commit -m "Add lab.shadow_summary and CLI subcommand."
```

---

## Task 25: First decomposition report

**Files:**
- Create: `docs/superpowers/reports/2026-05-19-divergence-attribution.md`

- [ ] **Step 1: Run decompose across all 7 cities, last 60 days**

```bash
python -m lab decompose --against live-today --days 60 --json > /tmp/decompose.json
python -m lab decompose --against live-today --days 60 > /tmp/decompose.txt
```

Save both outputs for the report.

- [ ] **Step 2: Run per-variant compare for narrative depth**

```bash
python -m lab compare live-today live-minus-nws --days 60 --bootstrap 1000 > /tmp/cmp_nws.txt
python -m lab compare live-today live-minus-trunc --days 60 --bootstrap 1000 > /tmp/cmp_trunc.txt
python -m lab compare live-today live-minus-push --days 60 --bootstrap 1000 > /tmp/cmp_push.txt
```

- [ ] **Step 3: Run refit-bias for the live formula**

```bash
python -m lab refit-bias --config live-today --days 60 --json > /tmp/refit.json
```

This produces a BIAS table calibrated against the live (NWS-blended) formula. Compare to the current `kalshi_temp.py:BIAS` table — non-trivial differences are direct evidence for mismatch #4 from the spec.

- [ ] **Step 4: Write the report**

`docs/superpowers/reports/2026-05-19-divergence-attribution.md`:

Structure:
- **Summary** — one-paragraph answer: which component(s) explain the live-vs-backtest gap, and by how much
- **Per-variant compare** — paste the text output of the three compare runs
- **Decomposition table** — variant name, attributed PnL, decision flips
- **Refit-BIAS diff** — current vs refit table, per-city deltas
- **Recommendation** — which config to graduate (or whether to keep shadowing for another 2 weeks)
- **Caveats** — small N, NWS historical not available, etc.

Include the actual numbers from your runs — do not write placeholders.

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/reports/2026-05-19-divergence-attribution.md
git commit -m "First divergence attribution report from the lab."
```

- [ ] **Step 6: Show user the report path and stop**

DO NOT auto-graduate to a new config. Tell the user:

> "First divergence attribution report committed: `docs/superpowers/reports/2026-05-19-divergence-attribution.md`. The recommended next step is X (where X = whatever the report concludes). Please review and approve before any change to `LIVE_TODAY` is made in `kalshi_temp.py`."

---

## Self-Review

Verified against the spec at `docs/superpowers/specs/2026-05-19-weatherbot-model-lab-design.md`:

- §6 directory layout: every file/directory in the spec has a creation task (Tasks 1-24).
- §7 data contracts: ModelInputs/ModelOutput in Task 6, ModelConfig in Task 6.
- §8 compute(): Task 7.
- §9 named configs: LIVE_TODAY/BACKTEST_TODAY in Task 8; variants in Task 16.
- §10 extraction in `kalshi_temp.py`: Tasks 11-12 with baseline verification (Tasks 9-10).
- §11 SQLite cache: Task 13.
- §12 shadow mode: Tasks 22-23.
- §13 tests three tiers: primitives in Tasks 3-5, compute integration in Task 7, baseline in Tasks 9-10, isolation in Task 22, cache in Task 13.
- §14 iteration loop: implicit in the testing + lab structure. Worked example in spec.
- §15 CLI: replay (17), compare (18), decompose (19), refit-bias (20), cache (21), shadow-summary (24).
- §16 plumbing: Task 1.
- §17 definition of done: every checkbox maps to one or more tasks.
- §18 out of scope: respected (no Polymarket bot changes, no HTML extraction, no auto-refit, no /labs page).
- First decomposition report (spec §17): Task 25.

Placeholder scan: no `TBD` / `TODO` / `fill in details` / unspecified code blocks. One known acknowledged limit: `cmd_cache` has `"warm"` as a CLI choice but the implementation returns "not yet implemented (TODO follow-up)" — this is intentional and documented; warming is not on the critical path for the audit, and the cache is populated as a side effect of `replay`.

Type consistency: `ModelConfig` fields used identically across `lab/configs.py`, `model/compute.py`, `shadow/runner.py`, `lab/replay.py`. `ModelInputs` field names match between `lab/inputs.py`, `tests/_record_baseline.py`, and the kalshi_temp extraction in Tasks 11-12.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-19-weatherbot-model-lab.md`.** Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Isolates each task to a clean context and lets you stop after any task to inspect the diff.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints. Faster if you trust the plan and want continuous progress.

**Which approach?**
