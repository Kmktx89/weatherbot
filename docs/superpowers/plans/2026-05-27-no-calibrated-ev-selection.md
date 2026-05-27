# Calibrated-EV Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the NO calibration haircut upstream of selection and collapse the three duplicated NO-selection implementations into one canonical source of truth in `kalshi_temp.py`, with the haircut sourced from a lab-emitted artifact + code fallback.

**Architecture:** A single `apply_calibration(market)` adds `cal_prob_{yes,no}` / `cal_ev_{yes,no}` fields (probability-space). `best_no_pick` / `best_yes_pick` / `predict_summary` filter, rank, and floor on the calibrated EV, computing the fields lazily if absent (so historical log rows still re-score). `alerts.py` and `generate_t24_card.py` delete their private copies and call the canonical functions. The haircut value loads from `calibration_params.json`, falling back to a provisional code default; the lab emits that JSON from its live-calibration measurement. No haircut magnitudes are committed.

**Tech Stack:** Python 3.12, pytest, stdlib only (`json`, `pathlib`). Spec: `docs/superpowers/specs/2026-05-27-no-calibrated-ev-selection-design.md`.

---

## File Structure

- **`kalshi_temp.py`** (modify) — owns the calibration layer: `DEFAULT_CAL_PARAMS`, `load_calibration_params`, `_CAL_PARAMS`, `haircut_for`, `_clamp01`, `apply_calibration`, `best_yes_pick`, `predict_summary`; `best_no_pick` rewritten to rank on `cal_ev_no`; `build_event_data` finalizes markets through `apply_calibration`; `predict_event` calls `predict_summary`.
- **`snapshot.py`** (modify) — `_bucket` serializes the four `cal_*` fields into new log rows.
- **`alerts.py`** (modify) — delete `calibrate_yes`, `calibrate_no`, `best_ev_no`, `best_yes`; `format_card` calls `kalshi_temp.best_yes_pick` / `best_no_pick` and reads `cal_*`.
- **`generate_t24_card.py`** (modify) — delete `predict_summary`, `_sanity_keep_no`, and the duplicated `MIN_BEST_EV` / `SANITY_*` / `MIN_PRINTED_NO` constants; call `kalshi_temp.predict_summary`; `format_digest_event` renders `cal_*`.
- **`lab/live_calibration.py`** (modify) — add `emit_params(records, path)`.
- **`lab/cli.py`** (modify) — add `--emit-params PATH` to the `live-calibration` subcommand.
- **`calibration_params.json`** (NOT committed) — runtime artifact; add to `.gitignore`. Absent in the repo → code default applies.
- **`tests/test_calibration.py`** (create) — the calibration layer, selection routing, loader/fallback, consolidation regression, YES identity.
- **`tests/test_emit_params.py`** (create) — the lab emit path + round-trip.

Constants already present and unchanged: `MIN_BEST_EV = 0.05`, `MIN_PRINTED_NO = 0.80`, `SANITY_MARKET_CONFIDENT_YES = 0.85`, `SANITY_MODEL_LOW_PROB = 0.40`, and `_sanity_keep_no(market)` (all in `kalshi_temp.py:1681-1701`).

---

## Task 1: Calibration params — default, loader, band lookup

**Files:**
- Modify: `kalshi_temp.py` (add after the `MIN_PRINTED_NO` block, currently line 1684)
- Test: `tests/test_calibration.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_calibration.py`:

```python
"""Tests for the calibrated-EV selection layer in kalshi_temp.py."""
import json

import kalshi_temp as kt


# ---------- params loader ----------

def test_load_params_missing_file_returns_default(tmp_path):
    p = tmp_path / "nope.json"
    assert kt.load_calibration_params(str(p)) == kt.DEFAULT_CAL_PARAMS


def test_load_params_malformed_returns_default(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert kt.load_calibration_params(str(p)) == kt.DEFAULT_CAL_PARAMS


def test_load_params_valid_file_parsed(tmp_path):
    p = tmp_path / "cal.json"
    payload = {"no": [{"lo": 0.80, "hi": 1.01, "h": 0.09}],
               "yes": [{"lo": 0.0, "hi": 1.01, "h": 0.0}]}
    p.write_text(json.dumps(payload), encoding="utf-8")
    assert kt.load_calibration_params(str(p)) == payload


def test_haircut_for_band_match_and_miss():
    params = {"no": [{"lo": 0.80, "hi": 1.01, "h": 0.11}],
              "yes": [{"lo": 0.0, "hi": 1.01, "h": 0.0}]}
    assert kt.haircut_for("no", 0.89, params) == 0.11   # in band
    assert kt.haircut_for("no", 0.50, params) == 0.0    # below band -> no match
    assert kt.haircut_for("yes", 0.42, params) == 0.0   # identity band
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_calibration.py -v`
Expected: FAIL — `AttributeError: module 'kalshi_temp' has no attribute 'load_calibration_params'`.

- [ ] **Step 3: Write minimal implementation**

In `kalshi_temp.py`, immediately after line 1684 (`MIN_PRINTED_NO = 0.80 ...`), add:

```python
# --- calibration params (haircut applied upstream of selection) ---
# Per-side list of printed-prob bands -> haircut h (in probability points).
# haircut = printed - realized: positive h = overconfident (shrink), negative
# h = underconfident (raise). The authoritative values live in
# calibration_params.json, written by `python -m lab.cli live-calibration
# --emit-params`. This code default is PROVISIONAL (NO ~11pp from the 2026-05-26
# n=49 cut; YES identity) and is superseded by the file when present. Refit the
# file at the 2026-06-03 cut.
DEFAULT_CAL_PARAMS = {
    "no":  [{"lo": 0.80, "hi": 1.01, "h": 0.11}],
    "yes": [{"lo": 0.00, "hi": 1.01, "h": 0.00}],
}
CAL_PARAMS_PATH = "calibration_params.json"


def load_calibration_params(path=CAL_PARAMS_PATH):
    """Load per-side haircut bands. Fall back to DEFAULT_CAL_PARAMS (and log)
    if the file is absent or malformed."""
    p = Path(path)
    if not p.exists():
        return DEFAULT_CAL_PARAMS
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        for side in ("yes", "no"):
            assert isinstance(data.get(side), list)
            for band in data[side]:
                float(band["lo"]); float(band["hi"]); float(band["h"])
        return data
    except Exception as e:
        print(f"[calibration] falling back to default params: {e}", file=sys.stderr)
        return DEFAULT_CAL_PARAMS


_CAL_PARAMS = load_calibration_params()


def haircut_for(side, printed_prob, params=None):
    """Haircut h for `side` at this printed probability; 0.0 if no band matches."""
    params = params if params is not None else _CAL_PARAMS
    for band in params.get(side, []):
        if band["lo"] <= printed_prob < band["hi"]:
            return float(band["h"])
    return 0.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_calibration.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add kalshi_temp.py tests/test_calibration.py
git commit -m "Calibration params: code default + loader + band lookup"
```

---

## Task 2: `apply_calibration` — the one transform

**Files:**
- Modify: `kalshi_temp.py` (add after `haircut_for` from Task 1)
- Test: `tests/test_calibration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration.py`:

```python
# ---------- apply_calibration ----------

PARAMS = {"no": [{"lo": 0.80, "hi": 1.01, "h": 0.11}],
          "yes": [{"lo": 0.0, "hi": 1.01, "h": 0.0}]}


def test_apply_calibration_no_haircut_shrinks_no_prob_and_ev():
    m = {"prob": 0.11, "yes_ask": 0.13, "no_ask": 0.82}
    kt.apply_calibration(m, PARAMS)
    # printed_no = 0.89; cal_prob_no = 0.89 - 0.11 = 0.78
    assert abs(m["cal_prob_no"] - 0.78) < 1e-9
    # cal_ev_no = 0.78 - 0.82 = -0.04  (raw ev_no was +0.07)
    assert abs(m["cal_ev_no"] - (-0.04)) < 1e-9


def test_apply_calibration_yes_identity_is_exact():
    m = {"prob": 0.576, "yes_ask": 0.88, "no_ask": 0.14}
    kt.apply_calibration(m, PARAMS)
    # h_yes = 0 -> cal must equal raw bit-for-bit
    assert m["cal_prob_yes"] == 0.576
    assert m["cal_ev_yes"] == 0.576 - 0.88


def test_apply_calibration_none_prob_yields_none_fields():
    m = {"prob": None, "yes_ask": None, "no_ask": None}
    kt.apply_calibration(m, PARAMS)
    assert m["cal_prob_no"] is None and m["cal_ev_no"] is None
    assert m["cal_prob_yes"] is None and m["cal_ev_yes"] is None


def test_apply_calibration_is_idempotent():
    m = {"prob": 0.11, "yes_ask": 0.13, "no_ask": 0.82}
    kt.apply_calibration(m, PARAMS)
    first = dict(m)
    kt.apply_calibration(m, PARAMS)
    assert m == first


def test_apply_calibration_clamps_to_unit_interval():
    # huge haircut would push below 0; must clamp
    params = {"no": [{"lo": 0.0, "hi": 1.01, "h": 5.0}], "yes": [{"lo": 0.0, "hi": 1.01, "h": 0.0}]}
    m = {"prob": 0.50, "yes_ask": 0.50, "no_ask": 0.50}
    kt.apply_calibration(m, params)
    assert m["cal_prob_no"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_calibration.py -k apply_calibration -v`
Expected: FAIL — `AttributeError: module 'kalshi_temp' has no attribute 'apply_calibration'`.

- [ ] **Step 3: Write minimal implementation**

In `kalshi_temp.py`, after `haircut_for`, add:

```python
def _clamp01(x):
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def apply_calibration(market, params=None):
    """Add cal_prob_{yes,no} and cal_ev_{yes,no} to `market` in place,
    computed from prob/yes_ask/no_ask in probability space.

    Idempotent (cal_* never feed back in). Exact identity when a side's
    haircut is 0: cal_prob == prob and cal_ev == ev bit-for-bit (no clamp,
    no rounding) so a zero-haircut side cannot flip a borderline pick.
    """
    params = params if params is not None else _CAL_PARAMS
    prob = market.get("prob")
    ya = market.get("yes_ask")
    na = market.get("no_ask")
    if prob is None:
        market["cal_prob_yes"] = None
        market["cal_prob_no"] = None
        market["cal_ev_yes"] = None
        market["cal_ev_no"] = None
        return market
    printed_no = 1.0 - prob
    h_yes = haircut_for("yes", prob, params)
    h_no = haircut_for("no", printed_no, params)
    cal_prob_yes = prob if h_yes == 0.0 else _clamp01(prob - h_yes)
    cal_prob_no = printed_no if h_no == 0.0 else _clamp01(printed_no - h_no)
    market["cal_prob_yes"] = cal_prob_yes
    market["cal_prob_no"] = cal_prob_no
    market["cal_ev_yes"] = (cal_prob_yes - ya) if ya not in (None, 0.0) else None
    market["cal_ev_no"] = (cal_prob_no - na) if na not in (None, 0.0) else None
    return market
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_calibration.py -k apply_calibration -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add kalshi_temp.py tests/test_calibration.py
git commit -m "apply_calibration: prob-space haircut with exact zero-haircut identity"
```

---

## Task 3: Route `best_no_pick` + add `best_yes_pick` through calibrated EV

**Files:**
- Modify: `kalshi_temp.py:1704-1723` (`best_no_pick`)
- Test: `tests/test_calibration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration.py`:

```python
# ---------- selection routes through calibrated EV ----------

def _mkt(ticker, prob, yes_ask, no_ask):
    return {"ticker": ticker, "subtitle": ticker, "prob": prob,
            "yes_ask": yes_ask, "no_ask": no_ask,
            "ev_yes": (prob - yes_ask), "ev_no": ((1 - prob) - no_ask)}


def test_best_no_pick_suppresses_negative_calibrated_ev(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    # raw ev_no = +7c but cal_ev_no = -4c -> must be dropped by the 5c floor
    losing = _mkt("A", prob=0.11, yes_ask=0.13, no_ask=0.82)
    # raw ev_no = +17c, cal_ev_no = 0.84 - 0.78 = +6c -> survives
    winning = _mkt("B", prob=0.05, yes_ask=0.06, no_ask=0.78)
    pick = kt.best_no_pick([losing, winning])
    assert pick is not None and pick["ticker"] == "B"


def test_best_no_pick_lazy_computes_cal_fields_on_raw_rows(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    row = _mkt("B", prob=0.05, yes_ask=0.06, no_ask=0.78)  # no cal_* keys
    assert "cal_ev_no" not in row
    pick = kt.best_no_pick([row])
    assert pick is not None and "cal_ev_no" in pick


def test_best_no_pick_keeps_printed_no_floor(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    # printed_no = 0.79 < 0.80 floor -> never surfaces regardless of EV
    below = _mkt("C", prob=0.21, yes_ask=0.05, no_ask=0.05)
    assert kt.best_no_pick([below]) is None


def test_best_yes_pick_matches_raw_ev_yes_under_identity(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    a = _mkt("A", prob=0.60, yes_ask=0.50, no_ask=0.45)  # ev_yes = +10c
    b = _mkt("B", prob=0.30, yes_ask=0.28, no_ask=0.70)  # ev_yes = +2c
    pick = kt.best_yes_pick([a, b])
    assert pick is not None and pick["ticker"] == "A"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_calibration.py -k "best_no_pick or best_yes_pick" -v`
Expected: FAIL — `best_yes_pick` missing; `best_no_pick` ranks on raw `ev_no` so `test_best_no_pick_suppresses_negative_calibrated_ev` fails (it would pick "A").

- [ ] **Step 3: Write minimal implementation**

Replace `best_no_pick` (`kalshi_temp.py:1704-1723`) with:

```python
def best_no_pick(markets):
    """Canonical 'Best EV NO' — single source of truth for the dashboard, CLI,
    T-24 card, and alerts.

    Ranks on CALIBRATED EV (cal_ev_no), so the haircut decides whether a pick
    surfaces at all. Subject to:
      - cal_ev_no >= MIN_BEST_EV       (calibrated edge worth taking)
      - _sanity_keep_no                (don't fight a highly-confident market)
      - (1 - prob) >= MIN_PRINTED_NO   (printed-NO floor; pre-haircut conviction)

    Computes cal_* lazily for markets missing them (e.g. historical log rows),
    so this re-scores live_picks_log.jsonl identically to the live pipeline.
    """
    for m in markets:
        if "cal_ev_no" not in m:
            apply_calibration(m)
    cands = [m for m in markets
             if m.get("cal_ev_no") is not None and m["cal_ev_no"] >= MIN_BEST_EV
             and m.get("prob") is not None and _sanity_keep_no(m)
             and (1 - m["prob"]) >= MIN_PRINTED_NO]
    return max(cands, key=lambda m: m["cal_ev_no"], default=None)


def best_yes_pick(markets):
    """Canonical 'Best EV YES' — argmax cal_ev_yes over the MIN_BEST_EV floor.
    Under the default identity YES haircut this equals argmax(ev_yes)."""
    for m in markets:
        if "cal_ev_yes" not in m:
            apply_calibration(m)
    cands = [m for m in markets
             if m.get("cal_ev_yes") is not None and m["cal_ev_yes"] >= MIN_BEST_EV]
    return max(cands, key=lambda m: m["cal_ev_yes"], default=None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_calibration.py -k "best_no_pick or best_yes_pick" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add kalshi_temp.py tests/test_calibration.py
git commit -m "Route best_no_pick/best_yes_pick through calibrated EV (lazy on raw rows)"
```

---

## Task 4: `predict_summary` in kalshi_temp + use it in `predict_event`

**Files:**
- Modify: `kalshi_temp.py` (add `predict_summary` after `best_yes_pick`; rewrite `predict_event` body at 1756-1771)
- Test: `tests/test_calibration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration.py`:

```python
# ---------- predict_summary ----------

def test_predict_summary_shape_and_picks(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    markets = [
        _mkt("TOP", prob=0.60, yes_ask=0.50, no_ask=0.45),   # highest prob + best YES
        _mkt("NO", prob=0.05, yes_ask=0.06, no_ask=0.78),    # survives NO floor+cal
    ]
    s = kt.predict_summary(markets)
    assert set(s) == {"highest_probability", "best_ev_yes", "best_ev_no"}
    assert s["highest_probability"]["ticker"] == "TOP"
    assert s["best_ev_yes"]["ticker"] == "TOP"
    assert s["best_ev_no"]["ticker"] == "NO"


def test_predict_summary_empty_markets():
    s = kt.predict_summary([])
    assert s == {"highest_probability": None, "best_ev_yes": None, "best_ev_no": None}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_calibration.py -k predict_summary -v`
Expected: FAIL — `AttributeError: module 'kalshi_temp' has no attribute 'predict_summary'`.

- [ ] **Step 3: Write minimal implementation**

In `kalshi_temp.py`, after `best_yes_pick`, add:

```python
def predict_summary(markets):
    """Canonical {highest_probability, best_ev_yes, best_ev_no} over a markets
    list. The one place the dashboard, CLI, T-24 card, and alerts derive picks."""
    for m in markets:
        if "cal_ev_no" not in m:
            apply_calibration(m)
    probs = [m for m in markets if m.get("prob") is not None]
    return {
        "highest_probability": (max(probs, key=lambda m: m["prob"]) if probs else None),
        "best_ev_yes": best_yes_pick(markets),
        "best_ev_no": best_no_pick(markets),
    }
```

Then rewrite the return dict in `predict_event` (`kalshi_temp.py:1756-1771`) to use it:

```python
    _log_nws_snapshot(data)
    summary = predict_summary(data["markets"])
    return {
        "event_ticker": data["event_ticker"],
        "title": data["title"],
        "target_date": data["target_date"],
        "station": data["station"],
        "forecasts": data["forecasts"],
        "model": data["model"],
        "settled": data["settled"],
        "settled_bucket": data["settled_bucket"],
        "highest_probability": summary["highest_probability"],
        "best_ev_yes": summary["best_ev_yes"],
        "best_ev_no": summary["best_ev_no"],
        "markets": data["markets"],
    }
```

(Delete the old inline `probs = [...]`, `highest_probability`, `best_ev_yes`, `best_ev_no` lines this replaces.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_calibration.py -k predict_summary -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add kalshi_temp.py tests/test_calibration.py
git commit -m "Add kalshi_temp.predict_summary; predict_event delegates to it"
```

---

## Task 5: Finalize markets through `apply_calibration` + serialize cal_* in snapshots

**Files:**
- Modify: `kalshi_temp.py:474` (after `out_markets.sort(...)` in `build_event_data`)
- Modify: `snapshot.py:34-47` (`_bucket`)
- Test: `tests/test_calibration.py`, `tests/test_snapshot_cal.py` (create)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration.py`:

```python
# ---------- finalize maps apply_calibration over a markets list ----------

def test_finalize_markets_adds_cal_fields(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    markets = [_mkt("A", 0.11, 0.13, 0.82), {"ticker": "S", "prob": None,
               "yes_ask": None, "no_ask": None}]
    kt.finalize_markets(markets)
    assert "cal_ev_no" in markets[0] and markets[0]["cal_ev_no"] is not None
    assert markets[1]["cal_ev_no"] is None  # settled/None-prob row tolerated
```

Create `tests/test_snapshot_cal.py`:

```python
"""snapshot._bucket carries the calibrated fields into the log row."""
import snapshot


def test_bucket_serializes_cal_fields():
    b = {"ticker": "T", "subtitle": "x", "strike_type": "between",
         "yes_bid": 0.1, "yes_ask": 0.12, "no_ask": 0.88, "prob": 0.2,
         "ev_yes": -0.1, "ev_no": 0.0, "vol_24h": 100,
         "cal_prob_yes": 0.2, "cal_prob_no": 0.69,
         "cal_ev_yes": 0.08, "cal_ev_no": -0.19}
    out = snapshot._bucket(b, {})
    assert out["cal_prob_no"] == 0.69
    assert out["cal_ev_no"] == -0.19
    assert out["cal_prob_yes"] == 0.2
    assert out["cal_ev_yes"] == 0.08
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_calibration.py -k finalize_markets tests/test_snapshot_cal.py -v`
Expected: FAIL — `finalize_markets` missing; `_bucket` output lacks `cal_*` keys (`KeyError`).

- [ ] **Step 3: Write minimal implementation**

In `kalshi_temp.py`, add a small helper after `apply_calibration`:

```python
def finalize_markets(markets, params=None):
    """Apply calibration to every market in a list (in place). Used to finalize
    build_event_data output so cal_* ship with the live dashboard and snapshots."""
    for m in markets:
        apply_calibration(m, params)
    return markets
```

Then in `build_event_data`, replace line 474 (`out_markets.sort(key=lambda x: x["_sort"])`) with:

```python
    out_markets.sort(key=lambda x: x["_sort"])
    finalize_markets(out_markets)
```

In `snapshot.py`, replace `_bucket` (lines 34-47) with:

```python
def _bucket(b: dict, raw: dict) -> dict:
    return {
        "ticker": b.get("ticker"),
        "subtitle": b.get("subtitle"),
        "strike_type": b.get("strike_type"),
        "yes_bid": b.get("yes_bid"),
        "yes_ask": b.get("yes_ask"),
        "no_bid": to_float(raw.get("no_bid_dollars")),
        "no_ask": b.get("no_ask"),
        "prob": b.get("prob"),
        "ev_yes": b.get("ev_yes"),
        "ev_no": b.get("ev_no"),
        "cal_prob_yes": b.get("cal_prob_yes"),
        "cal_prob_no": b.get("cal_prob_no"),
        "cal_ev_yes": b.get("cal_ev_yes"),
        "cal_ev_no": b.get("cal_ev_no"),
        "volume_24h": b.get("vol_24h"),
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_calibration.py -k finalize_markets tests/test_snapshot_cal.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add kalshi_temp.py snapshot.py tests/test_calibration.py tests/test_snapshot_cal.py
git commit -m "Finalize markets through apply_calibration; serialize cal_* in snapshots"
```

---

## Task 6: Collapse `alerts.py` onto the canonical functions

**Files:**
- Modify: `alerts.py` — delete `calibrate_yes` (59-69), `calibrate_no` (72-83), `best_yes` (86-88), `best_ev_no` (91-108) and its `MIN_PRINTED_NO` constant; rewrite `format_card` (119-176)
- Test: `tests/test_calibration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration.py`:

```python
# ---------- consolidation: alerts uses the canonical NO pick ----------

def test_alerts_no_pick_equals_canonical(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    import alerts
    buckets = [_mkt("A", 0.11, 0.13, 0.82), _mkt("B", 0.05, 0.06, 0.78)]
    canonical = kt.best_no_pick([dict(b) for b in buckets])
    card = alerts.format_card({"event_ticker": "KXHIGHNY-26MAY27", "series": "KXHIGHNY",
                               "target_date": "2026-05-27", "lead_hours": 24.0,
                               "model": {"mu": 70.0, "sigma": 1.0},
                               "buckets": [dict(b) for b in buckets]})
    # the surviving NO pick (B) subtitle must appear; the suppressed one (A) must not be the NO line
    assert "B" in card
    assert canonical["ticker"] == "B"


def test_alerts_has_no_private_calibration():
    import alerts
    assert not hasattr(alerts, "calibrate_no")
    assert not hasattr(alerts, "best_ev_no")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_calibration.py -k "alerts" -v`
Expected: FAIL — `alerts` still has `calibrate_no`/`best_ev_no`; `format_card` uses the private copy.

- [ ] **Step 3: Write minimal implementation**

In `alerts.py`, add the import near the top (after line 23, `import requests`):

```python
import kalshi_temp as kt
```

Delete `calibrate_yes`, `calibrate_no`, `best_yes`, `best_ev_no`, and the module-level `MIN_PRINTED_NO = 0.80` block (lines 59-108). Rewrite `format_card` (119-176) so the YES/NO blocks read canonical picks and cal_* fields:

```python
def format_card(row: dict) -> str:
    """5-line text card for one event row from live_picks_log.jsonl."""
    et = row.get("event_ticker", "?")
    series = (row.get("series") or "").replace("KXHIGH", "")
    target = row.get("target_date", "?")
    lead = row.get("lead_hours")
    model = row.get("model") or {}
    mu = model.get("mu")
    sigma = model.get("sigma")
    today_max = model.get("today_max")

    head = f"{series} {target} T-{lead:.0f}h"
    if mu is not None and sigma is not None:
        head += f"  μ{mu:.1f}°±{sigma:.1f}"
    if today_max is not None:
        head += f"  TM{today_max:.0f}"
    lines = [head]

    buckets = row.get("buckets") or []

    yes = kt.best_yes_pick(buckets)
    if yes is not None:
        p = yes["prob"]
        cal_p = yes["cal_prob_yes"]
        ya = yes.get("yes_ask")
        cal_ev = yes["cal_ev_yes"]
        action = "TAKE" if (cal_ev is not None and cal_ev >= TAKE_EV_THRESHOLD) else "SKIP-thin"
        lines.append(f"YES {yes['subtitle']}  {_fmt_pct(p)}→{_fmt_pct(cal_p)}  "
                     f"ask{_fmt_pct(ya)}  EV{_fmt_signed_cents(cal_ev)}¢  {action}")
    else:
        lines.append("YES (no pick)")

    no = kt.best_no_pick(buckets)
    if no is not None:
        printed_no = 1.0 - no["prob"]
        cal_p_no = no["cal_prob_no"]
        na = no.get("no_ask")
        cal_ev = no["cal_ev_no"]
        action = "TAKE" if (cal_ev is not None and cal_ev >= TAKE_EV_THRESHOLD) else "SKIP-thin"
        lines.append(f"NO  {no['subtitle']}  {_fmt_pct(printed_no)}→{_fmt_pct(cal_p_no)}  "
                     f"ask{_fmt_pct(na)}  EV{_fmt_signed_cents(cal_ev)}¢  {action}")
    else:
        lines.append("NO  (no pick)")

    if yes is not None:
        yb, ya = yes.get("yes_bid"), yes.get("yes_ask")
        if yb is not None and ya is not None and ya - yb > SPREAD_FLAG:
            lines.append(f"!  spread {(ya - yb) * 100:.0f}¢ on YES pick")

    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_calibration.py -k "alerts" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add alerts.py tests/test_calibration.py
git commit -m "Collapse alerts.py onto canonical best_no_pick/best_yes_pick + cal_* fields"
```

---

## Task 7: Collapse `generate_t24_card.py` onto the canonical functions

**Files:**
- Modify: `generate_t24_card.py` — delete `_sanity_keep_no` (69-78), `predict_summary` (81-98), and the duplicated constants `MIN_BEST_EV`/`SANITY_*`/`MIN_PRINTED_NO` (63-66); `build_event_entry` (294-319) calls `kalshi_temp.predict_summary`; `format_digest_event` (350-380) renders cal_*
- Test: `tests/test_calibration.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_calibration.py`:

```python
# ---------- consolidation: t24 card uses the canonical picks ----------

def test_t24_card_no_pick_equals_canonical(monkeypatch):
    monkeypatch.setattr(kt, "_CAL_PARAMS", PARAMS)
    import generate_t24_card as t24
    buckets = [_mkt("A", 0.11, 0.13, 0.82), _mkt("B", 0.05, 0.06, 0.78)]
    summary = t24.kt.predict_summary([dict(b) for b in buckets])
    assert summary["best_ev_no"] is not None
    assert summary["best_ev_no"]["ticker"] == kt.best_no_pick([dict(b) for b in buckets])["ticker"]


def test_t24_card_has_no_private_selection():
    import generate_t24_card as t24
    assert not hasattr(t24, "predict_summary") or t24.predict_summary is t24.kt.predict_summary
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_calibration.py -k "t24_card" -v`
Expected: FAIL — `generate_t24_card` still defines its own `predict_summary` and has no `kt` attribute.

- [ ] **Step 3: Write minimal implementation**

In `generate_t24_card.py`, add the import near the existing imports at the top of the file:

```python
import kalshi_temp as kt
```

Delete the duplicated constants `MIN_BEST_EV`, `SANITY_MARKET_CONFIDENT_YES`, `SANITY_MODEL_LOW_PROB`, `MIN_PRINTED_NO` (lines 63-66), the `_sanity_keep_no` function (69-78), and the `predict_summary` function (81-98). In `build_event_entry` (294-319), replace `summary = predict_summary(buckets)` with:

```python
    summary = kt.predict_summary(buckets)
```

Rewrite the NO/YES/top render lines in `format_digest_event` (350-380) to use calibrated fields:

```python
    if top:
        lines.append(f"{top['subtitle']} ({_fmt_pct(top['prob'])})")
        lines.append(
            f"  Y@{_fmt_money(top.get('yes_ask'))} → {_fmt_cents(top.get('cal_ev_yes'))}, "
            f"N@{_fmt_money(top.get('no_ask'))} → {_fmt_cents(top.get('cal_ev_no'))}"
        )
    else:
        lines.append("(no top pick)")
    if by and (not top or by.get("ticker") != top.get("ticker")):
        lines.append(
            f"Best EV YES: {by['subtitle']} @{_fmt_money(by.get('yes_ask'))} "
            f"→ {_fmt_cents(by.get('cal_ev_yes'))} (P={_fmt_pct(by.get('cal_prob_yes'))})"
        )
    if bn:
        lines.append(
            f"Best EV NO: {bn['subtitle']} @{_fmt_money(bn.get('no_ask'))} "
            f"→ {_fmt_cents(bn.get('cal_ev_no'))} (P_no={_fmt_pct(bn.get('cal_prob_no'))})"
        )
```

(The `top` block's `cal_ev_*` fields exist because `predict_summary` calls `apply_calibration` on every market.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_calibration.py -k "t24_card" tests/test_t24_card.py -v`
Expected: PASS — both new tests pass and the existing `test_t24_card.py` suite stays green. If an existing test asserts on the old `predict_summary` symbol or raw `ev_no` render text, update that test to call `kt.predict_summary` / assert on `cal_ev_no`; do not re-add the private copy.

- [ ] **Step 5: Commit**

```bash
git add generate_t24_card.py tests/test_calibration.py tests/test_t24_card.py
git commit -m "Collapse generate_t24_card onto kalshi_temp.predict_summary + cal_* render"
```

---

## Task 8: Lab `--emit-params` writes the calibration artifact

**Files:**
- Modify: `lab/live_calibration.py` (add `emit_params`)
- Modify: `lab/cli.py:167-210` (`cmd_live_calibration`) and `lab/cli.py:304-313` (subparser)
- Test: `tests/test_emit_params.py` (create)

- [ ] **Step 1: Write the failing test**

Create `tests/test_emit_params.py`:

```python
"""lab.live_calibration.emit_params writes a loadable calibration_params.json."""
import json

import kalshi_temp as kt
from lab.live_calibration import emit_params
from lab.calibration import CalibrationReport


class _Rec:
    def __init__(self, no_pred, no_won):
        self.event_ticker = "E"; self.lead_hours = 24.0
        self.yes_pred = None; self.yes_won = None
        self.no_pred = no_pred; self.no_won = no_won


def test_emit_params_writes_no_band_gap(tmp_path):
    # 4 NO obs: printed 0.90 each, 3 wins -> realized 0.75 -> gap +0.15
    recs = [_Rec(0.90, 1), _Rec(0.90, 1), _Rec(0.90, 1), _Rec(0.90, 0)]
    out = tmp_path / "calibration_params.json"
    emit_params(recs, str(out))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["yes"] == [{"lo": 0.0, "hi": 1.01, "h": 0.0}]   # identity per deployed decision
    assert data["no"][0]["lo"] == 0.80
    assert abs(data["no"][0]["h"] - 0.15) < 1e-6


def test_emitted_params_round_trip_through_loader(tmp_path):
    recs = [_Rec(0.90, 1), _Rec(0.90, 0)]   # gap +0.40
    out = tmp_path / "calibration_params.json"
    emit_params(recs, str(out))
    loaded = kt.load_calibration_params(str(out))
    assert kt.haircut_for("no", 0.90, loaded) == loaded["no"][0]["h"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_emit_params.py -v`
Expected: FAIL — `ImportError: cannot import name 'emit_params'`.

- [ ] **Step 3: Write minimal implementation**

In `lab/live_calibration.py`, add at the end of the file:

```python
def emit_params(records: list[LiveCalRecord], path: str = "calibration_params.json") -> dict:
    """Write calibration_params.json from the NO printed-vs-realized gap on the
    floored-pick set (one obs per event). YES is written as identity (h=0) per
    the deployed decision; turning it on is a separate, later change.

    Measures printed prob vs realized (the gap that DEFINES the haircut) — not
    cal_prob — so emission does not feed on its own output. One-pass, no
    fixed-point iteration.
    """
    rep = calibrate_live(records, "no")
    h_no = round(rep.mean_pred - rep.realized_rate, 4) if rep.n_bets else 0.0
    params = {
        "no":  [{"lo": 0.80, "hi": 1.01, "h": h_no}],
        "yes": [{"lo": 0.0, "hi": 1.01, "h": 0.0}],
        "_meta": {"n_no": rep.n_bets, "no_mean_pred": rep.mean_pred,
                  "no_realized": rep.realized_rate,
                  "generated": datetime.now(timezone.utc).isoformat()},
    }
    Path(path).write_text(json.dumps(params, indent=2), encoding="utf-8")
    return params
```

(`datetime`, `timezone`, `Path`, `json` are already imported at the top of `lab/live_calibration.py`.)

In `lab/cli.py`, add the flag to the `live-calibration` subparser (after line 312, `lc.add_argument("--json", ...)`):

```python
    lc.add_argument("--emit-params", metavar="PATH", nargs="?",
                    const="calibration_params.json", default=None,
                    help="write calibration_params.json from this cut and exit")
```

Then near the top of `cmd_live_calibration` (after line 191, `records, skips, leads = live_build_records(...)`), add:

```python
    if args.emit_params:
        from .live_calibration import emit_params
        params = emit_params(records, args.emit_params)
        print(f"Wrote {args.emit_params}: NO h={params['no'][0]['h']:+.4f} "
              f"(n={params['_meta']['n_no']}), YES identity")
        return
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_emit_params.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add lab/live_calibration.py lab/cli.py tests/test_emit_params.py
git commit -m "lab live-calibration --emit-params: write calibration_params.json"
```

---

## Task 9: Gitignore the artifact, run full suite, final commit

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Ignore the runtime artifact**

Add to `.gitignore` (so the lab-emitted file is never committed; the code default is the in-repo source of truth):

```
calibration_params.json
```

- [ ] **Step 2: Run the entire test suite**

Run: `python -m pytest -q`
Expected: all tests pass. If `test_t24_card.py` or `test_live_calibration.py` reference the deleted symbols or raw-EV render text, fix those tests to use the canonical functions / `cal_*` fields (never re-add a private copy). Re-run until green.

- [ ] **Step 3: Sanity-check historical re-scoring still works**

Run: `python -m lab.cli live-calibration --days 14`
Expected: prints the YES/NO headline without error (proves `best_no_pick` re-scores the 686 existing rows that lack `cal_*` via the lazy path).

Run: `python -m lab.cli live-calibration --days 14 --emit-params`
Expected: prints `Wrote calibration_params.json: NO h=...`; the file is created and gitignored.

- [ ] **Step 4: Final commit**

```bash
git add .gitignore
git commit -m "Gitignore calibration_params.json (runtime artifact; code default is canonical)"
```

---

## Spec coverage check

| Spec section | Task |
|---|---|
| §1 `apply_calibration`, prob-space, sign convention, exact identity | Task 2 |
| §2 selection routes through cal_ev; lazy compute; negative-cal suppressed | Task 3 |
| §3 delete the three copies; one place to set a haircut | Tasks 6, 7 (selection); Task 1 + §4 (the one number source) |
| §4 `calibration_params.json` + loader + code fallback | Task 1 |
| §5 lab emits the artifact; one-pass | Task 8 |
| §6 snapshot serializes cal_*; old rows lazy-compute; circularity resolved | Tasks 5, 3, 8 |
| Testing: identity, suppression, lazy, artifact, consolidation regression, YES-unchanged | Tasks 2, 3, 5, 6, 7, 8 |

`best_yes_pick` (Task 3) replaces the inline `best_ev_yes` max in `predict_event`; the YES-unchanged guarantee is covered by `test_best_yes_pick_matches_raw_ev_yes_under_identity` (Task 3) and `test_apply_calibration_yes_identity_is_exact` (Task 2).
