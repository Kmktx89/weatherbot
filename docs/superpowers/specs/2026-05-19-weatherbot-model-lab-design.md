# Weatherbot Model Lab — Design Spec

- **Status:** Draft, pending user approval
- **Date:** 2026-05-19
- **Owner:** kk@kmk-x.com
- **Target codebase:** `C:\Users\KrisKnecht\weatherbot`, branch `main`, remote `github.com/kmktx89/weatherbot.git`
- **Production process under review:** `python kalshi_temp.py serve --port 8765` (currently PID 30248, live)

---

## 1. Motivation

The live Kalshi temperature bot (`kalshi_temp.py serve`) is informing real bets. Stated pain: **the backtest disagrees with live behavior** — i.e. the picks and PnL produced by `cmd_backtest` over historical events do not predict what the dashboard does in real time, which makes both the historical edge and the production edge hard to trust.

A code audit identified five concrete differences between the live and backtest model formulas, plus a handful of secondary bugs. Any combination of these can explain the gap. We don't yet know which contribute the most. Guessing is expensive when money is on the line.

Beyond the immediate audit, the user wants a sustained capability to evolve the model — change source weights, swap bias strategies, try new sigma formulas, etc. — without breaking the live system. A one-shot patch is the wrong tool. We want a **model laboratory**.

## 2. Goals

1. Quantify how much each known live/backtest mismatch contributes to picking-divergence and PnL gap over the last ~60 settled events.
2. Provide a sustained, ergonomic mechanism to define, replay, compare, and shadow new model variants.
3. Make code changes to the math safe to ship through tests, frozen baselines, and shadow logging.
4. Preserve live production behavior byte-for-byte until the user explicitly approves a new config to graduate.

## 3. Non-goals

- Splitting `kalshi_temp.py` into many modules / extracting HTML to `static/` (deferred).
- Automating a recurring BIAS refit (the *capability* is built; the *automation* is later).
- Replacing `BASE_SIGMA=2.0` with per-city empirical σ as part of this spec.
- Touching `bot_v1.py` or `bot_v2.py` (Polymarket bots).
- Adding a `/labs` page on the dashboard (CLI-only per user decision; deferred).
- Changing the `nws_log.jsonl` schema or rotation policy.

## 4. Known live/backtest mismatches (the problem statement made concrete)

| # | Component | Live | Backtest | Impact |
|---|---|---|---|---|
| 1 | NWS overlay | `mu_raw = 0.7×blend + 0.3×NWS` if NWS present | NWS not used | Mu shift ~±0.3 × (NWS − blend); BIAS table doesn't compensate |
| 2 | TODAY-MAX truncation + push | Both `mu = today_max + 0.3` *and* lower-truncation in CDF | Neither | Double-counts the constraint; live shifts hotter than warranted |
| 3 | Sigma source list | `pstdev([ec, gfs, nws])` | `pstdev([ec, gfs])` | Live σ wider when NWS disagrees with EC/GFS |
| 4 | BIAS calibration target | Applied to NWS-blended mu | Applied to ec/gfs-only mu | Bias correction uncalibrated for live formula (existing code comment flags this) |
| 5 | Decision time | "Whenever the dashboard refreshes" | Locked to T-24h sharp | Live picks at T-2h see fresher forecasts than BIAS was fit on |

Secondary bugs to track during the audit (won't be fixed in this spec, but noted so they aren't lost):

- Rate-limit lock race in `kalshi_get` — `_kalshi_last_call` is set inside the lock but the HTTP call runs outside.
- `_sanity_keep_no` is applied in `predict_event` (HTTP API) but bypassed in `cmd_predict` (CLI).
- `actual_high_midpoint` collapses "X or below" / "X or above" buckets to a single integer — biases the BIAS calibration toward less extreme days.
- `nyc_bias.py` uses a simple `(ecmwf + gfs) / 2.0` while the production model uses per-city `weighted_mean`; the diagnostic is inconsistent with the model.

## 5. High-level approach

Build an audit/experimentation layer (the **lab**) that lives alongside `kalshi_temp.py`. The lab consumes a pure-Python model package (`model/`) which the live bot and the backtest also call. A `ModelConfig` dataclass captures every knob; variants are data, not code branches. A shadow runner inside the dashboard refresh logs candidate configs' picks to JSONL without acting on them.

Iterating on the model itself (changing `model/compute.py`) is made safe by three tiers of tests: math primitives, integration with synthetic inputs, and frozen golden outputs on real recorded events.

## 6. Directory layout

```
weatherbot/
├── kalshi_temp.py            ← behavior unchanged; one extraction (see §10)
├── model/                    ← NEW: pure-Python model code, no I/O
│   ├── __init__.py
│   ├── types.py              ← ModelInputs, ModelOutput dataclasses
│   ├── config.py             ← ModelConfig dataclass
│   ├── primitives.py         ← bucket_probability, bucket_bounds, weighted_mean,
│   │                           normal_cdf, event_local_date, apply_today_max
│   └── compute.py            ← compute(inputs, cfg) -> ModelOutput
├── lab/                      ← NEW: experimentation harness, uses model/
│   ├── __init__.py
│   ├── __main__.py           ← `python -m lab ...`
│   ├── cli.py                ← argparse routing
│   ├── configs.py            ← LIVE_TODAY, BACKTEST_TODAY, LIVE_MINUS_*, etc.
│   ├── data_cache.py         ← SQLite-backed input cache
│   ├── inputs.py             ← assembles ModelInputs from Kalshi+OpenMeteo+METAR
│   ├── replay.py             ← replays a ModelConfig over settled events
│   ├── compare.py            ← head-to-head A/B with bootstrap CI
│   ├── decompose.py          ← live − variant prob/PnL attribution
│   ├── refit_bias.py         ← refits BIAS for a given config
│   └── shadow_summary.py     ← reads shadow_picks.jsonl, joins to outcomes
├── shadow/                   ← NEW: live-side A/B logger
│   ├── __init__.py
│   ├── runner.py             ← run_shadow(inputs, configs) -> None
│   └── active.py             ← list[ModelConfig] of currently-shadowed configs
├── tests/                    ← NEW
│   ├── __init__.py
│   ├── test_primitives.py    ← pure-math unit tests
│   ├── test_compute.py       ← pipeline integration with synthetic inputs
│   ├── test_baseline.py      ← frozen outputs on ~10 real recorded events
│   ├── test_shadow_isolation.py  ← shadow errors must not break live path
│   └── fixtures/baseline/    ← *.json recorded inputs + expected outputs
├── docs/superpowers/
│   ├── specs/2026-05-19-weatherbot-model-lab-design.md     ← this file
│   └── reports/              ← first decomposition report lands here
├── pyproject.toml            ← NEW (declares packages, deps, scripts)
├── pytest.ini                ← NEW
└── requirements.txt          ← NEW (pinned runtime deps)
```

Gitignored additions: `lab/.cache.sqlite`, `shadow_picks.jsonl`, `__pycache__/`, `*.pyc`.

## 7. Data contracts

```python
# model/types.py
from dataclasses import dataclass
from collections.abc import Mapping, Sequence

@dataclass(frozen=True)
class ModelInputs:
    series: str                          # "KXHIGHNY"
    event_ticker: str                    # "KXHIGHNY-26MAY13"
    target_date: str                     # "2026-05-13"
    forecasts: Mapping[str, float | None]  # {"ecmwf": 72.4, "gfs": 71.8, "nws": 73.1}
    metar_current: float | None          # latest METAR temp °F (informational)
    today_max: float | None              # max METAR temp observed today, or None
    markets: Sequence[Mapping]           # raw Kalshi market dicts, unmodified
    decision_ts: int                     # unix seconds at decision time
    fetched_at: int                      # unix seconds when these inputs were captured

@dataclass(frozen=True)
class ModelOutput:
    mu: float | None
    sigma: float | None
    mu_raw: float | None                 # pre-bias mu (for diagnostics)
    bias_applied: float                  # the BIAS value used (0.0 if missing)
    truncation: float | None             # active lower-truncation, or None
    today_max_active: bool               # whether truncation/push fired
    probs: Mapping[str, float]           # {ticker: P(bucket)}
    config_name: str                     # echo for logging
    code_version: str                    # see "Code version resolution" below
```

**Code version resolution.** `CODE_VERSION` is computed once at import time by `model/__init__.py`:

1. If `git rev-parse --short HEAD` succeeds in the package directory, use that short SHA.
2. Otherwise, use a SHA-256 prefix (first 7 hex chars) of the concatenated contents of `model/primitives.py` + `model/compute.py`.

Either way it produces a short string that distinguishes runtime versions of the model code. Logged into every `ModelOutput` and every shadow JSONL row.

```python
# model/config.py
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Literal

@dataclass(frozen=True)
class ModelConfig:
    name: str
    source_weights: Mapping[str, Mapping[str, float]]   # series → {source: weight}
    nws_blend: float                                     # 0.0 = no NWS; live = 0.3
    bias_table: Mapping[str, float]                      # series → bias offset to subtract
    base_sigma: float                                    # 2.0 today
    sigma_sources: tuple[str, ...]                       # which sources contribute to σ spread
    today_max_mode: Literal["off", "truncate", "push", "both"]
    today_max_headroom: float                            # 0.5 °F (Kalshi METAR rounding allowance)
    today_max_push: float                                # 0.3 °F (only if mode in {"push","both"})
    sanity_no_yes_ask_min: float                         # 0.85
    sanity_no_prob_max: float                            # 0.40
    decision_lead_hours: float                           # 24.0
```

## 8. The `compute()` function

```python
# model/compute.py
def compute(inputs: ModelInputs, cfg: ModelConfig) -> ModelOutput:
    mu_raw = blend_sources(inputs.forecasts, cfg)
    if mu_raw is None:
        return ModelOutput(mu=None, sigma=None, mu_raw=None, bias_applied=0.0,
                           truncation=None, today_max_active=False, probs={},
                           config_name=cfg.name, code_version=CODE_VERSION)

    bias  = cfg.bias_table.get(inputs.series, 0.0)
    mu    = mu_raw - bias
    sigma = sigma_from(inputs.forecasts, cfg)

    truncation = None
    today_max_active = False
    if cfg.today_max_mode != "off" and inputs.today_max is not None:
        truncation, mu, today_max_active = apply_today_max(mu, inputs.today_max, cfg)

    probs = {}
    for m in inputs.markets:
        bounds = bucket_bounds(m)
        if bounds is None:
            continue
        p = bucket_probability(bounds, mu, sigma, lower_truncation=truncation)
        if p is not None:
            probs[m["ticker"]] = p

    return ModelOutput(mu=mu, sigma=sigma, mu_raw=mu_raw, bias_applied=bias,
                       truncation=truncation, today_max_active=today_max_active,
                       probs=probs, config_name=cfg.name, code_version=CODE_VERSION)
```

`blend_sources`, `sigma_from`, `apply_today_max`, `bucket_bounds`, `bucket_probability` are exported from `model/primitives.py` and have unit tests.

`apply_today_max` makes the four modes explicit, so the **current TODAY-MAX double-counting bug** (`mode="both"`) is preserved exactly under `LIVE_TODAY` but eliminated under `LIVE_MINUS_PUSH` (`mode="truncate"`).

## 9. `lab/configs.py` — named variants

At least these ship in the first cut:

```python
LIVE_TODAY            # reproduces current production formula byte-for-byte
BACKTEST_TODAY        # reproduces cmd_backtest formula byte-for-byte
LIVE_MINUS_NWS        # nws_blend=0.0
LIVE_MINUS_TRUNC      # today_max_mode="off"
LIVE_MINUS_PUSH       # today_max_mode="truncate" (fixes double-count)
LIVE_REFIT_BIAS       # BIAS re-fit against the live formula using nws_log.jsonl + outcomes
LIVE_AT_T24_STRICT    # decision_lead_hours=24, no drift
```

Each is a `ModelConfig` instance. Adding a new variant is one Python literal; no other code changes.

## 10. Extraction in `kalshi_temp.py`

`build_event_data` and `backtest_one_event` currently inline the model math. Both call sites are replaced with:

```python
from model import compute, ModelInputs
from lab.configs import LIVE_TODAY, BACKTEST_TODAY

# in build_event_data:
inputs = ModelInputs(series=series, event_ticker=ev["event_ticker"], target_date=target,
                     forecasts={"ecmwf": ecmwf, "gfs": gfs, "nws": nws},
                     metar_current=metar, today_max=today_max,
                     markets=markets, decision_ts=int(time.time()),
                     fetched_at=int(time.time()))
out = compute(inputs, LIVE_TODAY)
```

The dashboard's `model` block in the JSON response is assembled from `out` (same keys as today: mu, sigma, sources count, today_max, truncation). **EV is computed by the caller** using `out.probs[ticker]` joined to each market's `yes_ask` / `no_ask`:

```python
ev_yes = (out.probs[m["ticker"]] - to_float(m["yes_ask_dollars"])) if m["yes_ask_dollars"] else None
ev_no  = ((1 - out.probs[m["ticker"]]) - to_float(m["no_ask_dollars"])) if m["no_ask_dollars"] else None
```

This keeps `compute()` a pure model function (probability in, probability out) and EV / sanity-rule / sizing logic in the caller where they can vary independently.

The shadow runner is invoked next, with the same `inputs`:

```python
from shadow.runner import run_shadow
from shadow.active import ACTIVE_SHADOWS
run_shadow(inputs, ACTIVE_SHADOWS)
```

`test_baseline.py` snapshots 10 events' (mu, sigma, prob-per-bucket) outputs *before* the extraction and asserts they match exactly *after* the extraction. Zero production behavior change.

## 11. SQLite input cache

```sql
CREATE TABLE fetches (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    fetched_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    target_date TEXT
);
CREATE INDEX idx_source_date ON fetches(source, target_date);
```

Stored at `lab/.cache.sqlite`. Keys:

- `open_meteo:hist:<model>:<lat>,<lon>:<date>` → daily max forecast JSON
- `open_meteo:live:<model>:<lat>,<lon>:<date>` → live forecast JSON (short TTL)
- `kalshi_candle:<series>:<ticker>:<start_ts>:<end_ts>:<minutes>` → candlestick list
- `kalshi_event_markets:<event_ticker>` → markets array
- `metar_hist:<icao>:<date>` → historical METAR observations
- `nws:<lat>,<lon>:<date>` → NWS forecast

Reads check `(now - fetched_at < TTL_per_source)`; misses go to network. Purge by date via the CLI.

**Per-source TTLs:**

| Source key | TTL | Notes |
|---|---|---|
| `open_meteo:hist:*` | ∞ | Historical archive doesn't change once the date is past |
| `open_meteo:live:*` | 300 s | Matches the dashboard's current 5-min cache |
| `nws:*` | 300 s | NWS refreshes every ~3 h; 5 min is generous on the cache side |
| `metar_hist:*` | ∞ | Past-date observations are immutable |
| `kalshi_event_markets:*` | 60 s | Live markets move; tight TTL for replay-at-decision-time |
| `kalshi_candle:*` for past windows | ∞ | Historical candles don't change |
| `kalshi_candle:*` for windows ending ≤ now | 300 s | Newer windows can extend |

## 12. Shadow mode

`shadow/runner.py`:

```python
def run_shadow(inputs: ModelInputs, configs: Sequence[ModelConfig]) -> None:
    refresh_ts = utcnow_iso()
    for cfg in configs:
        try:
            out = compute(inputs, cfg)
            pick = top_pick(out, inputs.markets)   # argmax over out.probs, joined to market for yes_ask/no_ask
            append_jsonl(SHADOW_PATH, _row(refresh_ts, inputs, cfg, out, pick))
        except Exception as e:
            sys.stderr.write(f"[shadow:{cfg.name}] {e}\n")  # never crash live
```

JSONL row schema is locked at:

```json
{
  "ts": "2026-05-19T18:55:12Z",
  "event_ticker": "KXHIGHNY-26MAY20",
  "config_name": "live-minus-push",
  "code_version": "a3f8d12",
  "mu": 78.4, "sigma": 2.1, "mu_raw": 79.6, "bias_applied": -0.44,
  "truncation": 76.5, "today_max_active": true,
  "pick_ticker": "KXHIGHNY-26MAY20-B79", "pick_prob": 0.34,
  "pick_yes_ask": 0.22, "pick_no_ask": 0.79,
  "ev_yes": 0.12, "ev_no": -0.13
}
```

New fields are additive; old rows stay readable.

`shadow/active.py` is a single list literal:

```python
ACTIVE_SHADOWS = [LIVE_MINUS_NWS, LIVE_MINUS_PUSH, LIVE_REFIT_BIAS]
```

To add or remove a shadow config: edit the list, restart `kalshi_temp serve`.

Hard requirement: a shadow exception MUST NOT affect the live `ModelOutput` returned to the dashboard. Verified by `test_shadow_isolation.py`.

## 13. Tests — three tiers

**`tests/test_primitives.py`** — pure math, no I/O, no fixtures. Each function gets:

- `bucket_probability`: normal, truncated above/below mu, σ=0 degenerate, bucket entirely below truncation
- `bucket_bounds`: less / greater / between / missing-strike
- `weighted_mean`: all sources, one missing, both missing, zero total weight
- `event_local_date`: KXHIGHNY-26MAY13 → 2026-05-13, malformed input fallback
- `apply_today_max`: each mode (off / truncate / push / both)
- `normal_cdf`: monotonicity, σ limits

~30 tests, < 100 ms.

**`tests/test_compute.py`** — pipeline integration with synthetic `ModelInputs`. ~10 tests.

- `LIVE_TODAY` produces expected (mu, sigma, top-pick) on hand-built inputs
- `LIVE_MINUS_NWS` differs from `LIVE_TODAY` only on mu by the expected amount
- TODAY-MAX double-counting is demonstrated by a test that asserts the buggy behavior under mode=`"both"` and the fixed behavior under mode=`"truncate"`. The test stays green when we eventually graduate the fix.

**`tests/test_baseline.py`** — frozen golden outputs on real recorded events.

- 10 events sampled from the last 60 days, one per city plus a few edge cases (afternoon-active TODAY-MAX, NWS-missing, settled)
- Each fixture: `{inputs: ModelInputs as JSON, expected: {mu, sigma, probs}}` recorded once into `tests/fixtures/baseline/EVENT_TICKER.json`
- The test re-runs `compute(inputs, LIVE_TODAY)` and asserts output matches to 4 decimals
- Unintentional drift in `model/compute.py` fails this test
- `pytest --update-baseline` regenerates the fixtures; the resulting JSON diff is reviewed in the commit

`tests/test_shadow_isolation.py` — runs a shadow config that intentionally raises; asserts the live `ModelOutput` is unchanged and the JSONL line is absent.

All tests run in < 1 s with no network access.

## 14. The iteration loop (worked example)

To fix the TODAY-MAX double-counting bug (or any other model change):

1. Edit `model/primitives.py::apply_today_max` (or wherever).
2. `pytest -q`
   - `test_primitives.py` — pass
   - `test_compute.py` — pass; the bug-regression test still passes because it parameterizes by mode
   - `test_baseline.py` — fails on N events where the bug fired; output diff is shown
3. `git diff tests/fixtures/baseline/*.json` — review the prob shifts. If intentional, `pytest --update-baseline`.
4. Add a new named config to `lab/configs.py` (or reuse `LIVE_MINUS_PUSH`).
5. `python -m lab compare live-today live-minus-push --days 60 --bootstrap 1000`
   - Reads: bets, WR, PnL, max DD, agreement %, decision flips, CI on PnL delta.
6. `python -m lab decompose --against live-today --days 60` — attribute the PnL shift to each component (NWS, push, truncation, BIAS, lead) so the change is well-understood.
7. Add the new config to `shadow/active.py`; restart `kalshi_temp serve`.
8. Wait ~2 weeks of shadow data. `python -m lab shadow-summary --config live-minus-push --days 14`.
9. Graduate: change one line in `kalshi_temp.py` from `LIVE_TODAY` to `LIVE_MINUS_PUSH`. Commit. Restart.

This loop is the *product* — not a one-time audit but a repeatable model-development cadence.

## 15. CLI surface

```
python -m lab replay         --config NAME [--days N] [--events T1,T2] [--lead H]
python -m lab compare        CFG_A CFG_B [--days N] [--lead H] [--bootstrap N]
python -m lab decompose      [--against live-today] [--variants v1,v2,...] [--days N]
python -m lab shadow-summary --config NAME [--days N]
python -m lab refit-bias     --config NAME [--days N]
python -m lab cache          {warm | stats | purge --before DATE}
```

Every command supports `--json`. Default tables format cleanly in PowerShell.

## 16. Project plumbing

- **`pyproject.toml`** declares packages (`model`, `lab`, `shadow`), runtime dep `requests`, dev dep `pytest`, and a `[project.scripts]` entry `lab = lab.cli:main`.
- **`pytest.ini`** sets `testpaths = tests`, `addopts = -q`, registers a `--update-baseline` flag via a small conftest plugin.
- **`requirements.txt`** pins `requests` and `pytest` for the dev path; mirrors the existing README install steps.
- **`.gitignore`** adds `lab/.cache.sqlite`, `shadow_picks.jsonl`, `__pycache__/`, `*.pyc`, `tests/fixtures/baseline/.lock`.

## 17. Definition of done

- [ ] `model/` package: types, config, primitives, compute — all importable and unit-tested
- [ ] `kalshi_temp.py` extraction: `build_event_data` and `backtest_one_event` both call `compute(inputs, cfg)`; baseline tests prove `(mu, sigma, probs)` match the pre-extraction snapshot to 4 decimal places for ≥10 real events (`compute()` is pure-Python and deterministic, so the floor is set by `math.erf` precision — 4 decimals is well within slack)
- [ ] `lab/configs.py` ships `LIVE_TODAY`, `BACKTEST_TODAY`, and at least 5 "minus-one" variants
- [ ] `lab/data_cache.py` SQLite cache working; second replay over 60 days < 30 s
- [ ] `lab/replay`, `compare`, `decompose`, `refit-bias`, `cache`, `shadow-summary` CLI subcommands functional
- [ ] `shadow/runner.py` integrated into `get_dashboard_data()`; failure-isolation test green; `shadow_picks.jsonl` writing
- [ ] All three test tiers green; total runtime < 1 s; no network
- [ ] **First decomposition report** committed to `docs/superpowers/reports/2026-05-19-divergence-attribution.md`
- [ ] Graduation step (changing `LIVE_TODAY` to a different config in `kalshi_temp.py`) is one line and requires explicit user approval after the report is reviewed

## 18. Out of scope (explicit)

- Splitting `kalshi_temp.py` into many modules
- Extracting HTML/CSS/JS to a `static/` folder
- Self-refitting BIAS as a scheduled job
- Per-city empirical σ in place of `BASE_SIGMA=2.0`
- A `/labs` dashboard page
- `bot_v1.py` / `bot_v2.py` review
- Fixing the secondary bugs listed in §4 (rate-limit race, sanity-rule inconsistency, midpoint collapsing, nyc_bias coords). They are tracked but not part of this design.

## 19. Open questions

- (resolved) Live betting context: **yes, real bets are being placed** → regression baseline required.
- (resolved) Pain point: **backtest disagrees with live** → audit-first, then targeted fix.
- (resolved) Shadow mode: **yes, ship in v1**.
- (resolved) Dashboard `/labs` page: **CLI only for v1**.
- (open) Whether the first decomposition report should drive a same-day production change or only a shadow-mode trial. Defer the decision until the report exists.

## 20. Glossary

- **mu** — mean of the forecast distribution for the day's high temp (°F)
- **sigma** — standard deviation of the forecast distribution (°F)
- **BIAS** — per-city correction applied to mu, in °F, subtracted from `mu_raw`
- **today_max** — the highest METAR observation seen at the resolution station so far on the target date (a hard floor for the day's high)
- **truncation** — the lower bound passed to `bucket_probability` to condition on `high ≥ today_max − headroom`
- **lead time / lead hours** — hours before market close when the model snapshot is taken
- **decision flip** — an event where two configs picked different buckets
- **graduate** — change which named `ModelConfig` is wired into `kalshi_temp.py`'s `build_event_data` (production)
