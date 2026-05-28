# Hourly Signal Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** An hourly, strictly-gated report of the model's currently-qualifying picks (interim bar) rendered as complete order tickets — surfaced on the live dashboard (with a "last updated" time) and pushed to the phone.

**Architecture:** A pure filter module `signals.py` turns the live `/api/markets` event data into order tickets; a thin `hourly_signals.py` runner (Windows scheduled task) writes `hourly_signals.json`; the dashboard serves it at `/api/signals` and renders a panel; a session cron reads the JSON and pushes. Reuses `kalshi_temp.best_yes_pick/best_no_pick` so picks match the deployed model exactly.

**Tech Stack:** Python 3.13, pytest, the existing `kalshi_temp` module + HTTPS dashboard.

**Working directory:** worktree root `C:\Users\KrisKnecht\weatherbot\.claude\worktrees\signal-report`. Run `pytest`/`git` from there (`cd /c/Users/KrisKnecht/weatherbot/.claude/worktrees/signal-report && ...`); use `python -m pytest` (bare `pytest` can't find `kalshi_temp` on sys.path).

**Spec:** `docs/superpowers/specs/2026-05-28-hourly-signal-report-design.md`

**Reused facts (verified):**
- `kalshi_temp.best_yes_pick(markets)` → market dict with `cal_ev_yes, prob, ticker, subtitle, yes_ask, _sort` (or None); `best_no_pick(markets)` → market dict with `cal_ev_no, prob, ticker, subtitle, no_ask, _sort` (or None, enforces `(1-prob)>=0.80` + sanity cap).
- A market dict has: `ticker, subtitle, strike_type, yes_bid, yes_ask, no_ask, prob, cal_prob_yes, cal_prob_no, cal_ev_yes, cal_ev_no, _sort`.
- Spread (both sides) = `yes_ask - yes_bid` (no_bid is not exposed but equals 1-yes_ask, so NO spread == YES spread).
- Event-data dict from `/api/markets`: `{event_ticker, target_date, station, settled, model, markets:[...]}`. `event_ticker` like `KXHIGHLAX-26MAY28`; series = `event_ticker.split("-")[0]`; `kalshi_temp.CITIES[series]["tz"]` is the IANA tz.
- All KXHIGH events close ~01:00 local on the day AFTER `target_date`; lead is derived from that.

---

### Task 1: `signals.py` — interim bar constants + pure helpers (size, lead, favorite)

**Files:**
- Create: `signals.py`
- Test: `tests/test_signals.py` (create)

- [ ] **Step 1: Write failing tests.** Create `tests/test_signals.py`:

```python
import math
from signals import (
    YES_EV_MIN, EV_MAX, SPREAD_MAX, NO_PRINTED_MIN, NO_LEAD_MIN, KELLY_FRACTION,
    size_pct, lead_hours_for, market_favorite_index,
)


def test_size_pct_eighth_kelly():
    # 1/8-Kelly as % of bankroll = 100 * 0.125 * ev/price
    assert size_pct(ev=0.12, price=0.52) == round(100 * 0.125 * 0.12 / 0.52, 1)
    assert size_pct(ev=0.10, price=0.50) == 2.5


def test_size_pct_zero_price_is_zero():
    assert size_pct(ev=0.1, price=0.0) == 0.0


def test_market_favorite_index_picks_highest_mid():
    markets = [
        {"yes_bid": 0.05, "yes_ask": 0.07},   # mid .06
        {"yes_bid": 0.40, "yes_ask": 0.44},   # mid .42  <- favorite
        {"yes_bid": 0.20, "yes_ask": 0.24},   # mid .22
    ]
    assert market_favorite_index(markets) == 1


def test_market_favorite_index_none_when_no_prices():
    assert market_favorite_index([{"yes_bid": None, "yes_ask": None}]) is None


def test_lead_hours_for_uses_close_one_am_local_day_after():
    # target 2026-05-28, LA tz; close ~01:00 local 2026-05-29.
    # at 2026-05-28T09:00 local, lead ~16h. Use a fixed now for determinism.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime(2026, 5, 28, 9, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    lead = lead_hours_for("KXHIGHLAX", "2026-05-28", now=now)
    assert lead == math.isclose(lead, 16.0, abs_tol=0.05) or abs(lead - 16.0) < 0.05
```

- [ ] **Step 2: Run to verify fail.** `python -m pytest tests/test_signals.py -v` → FAIL (no module `signals`).

- [ ] **Step 3: Implement.** Create `signals.py`:

```python
"""Interim qualifying-signal filter: turns live event data into clean order
tickets that pass the probation bar (2026-05-27 weighted-sigma tightening).

Detection/reporting only — reads model output + prices, never trades, never
writes anything except via the hourly_signals.py runner. Filter logic lives
ONLY here (single source of truth). See spec
docs/superpowers/specs/2026-05-28-hourly-signal-report-design.md.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import kalshi_temp as kt

# Interim bar (tunable as the post-deploy live calibration comes in).
YES_EV_MIN = 0.10
EV_MAX = 0.25          # drop "huge" edges (likely sigma-artifacts post-tightening)
SPREAD_MAX = 0.05
NO_PRINTED_MIN = 0.90  # NO only when very confident (pre-haircut 1 - P_yes)
NO_LEAD_MIN = 18.0     # NO never shown near close
KELLY_FRACTION = 0.125  # 1/8-Kelly during probation


def size_pct(ev: float, price: float) -> float:
    """1/8-Kelly stake as a % of bankroll = 100 * 0.125 * ev/price."""
    if not price:
        return 0.0
    return round(100 * KELLY_FRACTION * ev / price, 1)


def market_favorite_index(markets: list[dict]) -> int | None:
    """Index of the bucket with the highest market mid (yes_bid+yes_ask)/2."""
    best_i, best_mid = None, None
    for i, m in enumerate(markets):
        yb, ya = m.get("yes_bid"), m.get("yes_ask")
        if yb is None or ya is None:
            continue
        mid = (yb + ya) / 2.0
        if best_mid is None or mid > best_mid:
            best_i, best_mid = i, mid
    return best_i


def lead_hours_for(series: str, target_date: str, *, now: datetime | None = None) -> float:
    """Hours from now until ~01:00 local on the day AFTER target_date
    (the verified KXHIGH close convention)."""
    tz = ZoneInfo(kt.CITIES[series]["tz"])
    now = now.astimezone(tz) if now else datetime.now(tz)
    close_day = datetime.fromisoformat(target_date).date() + timedelta(days=1)
    close = datetime(close_day.year, close_day.month, close_day.day, 1, 0, tzinfo=tz)
    return (close - now).total_seconds() / 3600.0
```

- [ ] **Step 4: Run to verify pass.** `python -m pytest tests/test_signals.py -v` → PASS (5 tests).

- [ ] **Step 5: Commit.**
```
git add signals.py tests/test_signals.py
git commit -m "signals: interim-bar constants + pure helpers (size, lead, favorite)"
```
(End commit body with `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`.)

---

### Task 2: `signals.py` — `qualify_event` + `qualifying_signals` (the gate + tickets)

**Files:**
- Modify: `signals.py`
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing tests.** Append to `tests/test_signals.py`:

```python
from signals import qualify_event, qualifying_signals


def _mkt(sub, prob, yb, ya, na, st="between"):
    # cal_* identity to prob so best_yes/no_pick work without network.
    return {"ticker": "T-" + sub, "subtitle": sub, "strike_type": st,
            "yes_bid": yb, "yes_ask": ya, "no_ask": na, "_sort": sub,
            "prob": prob, "cal_prob_yes": prob, "cal_prob_no": 1 - prob,
            "cal_ev_yes": (prob - ya) if ya else None,
            "cal_ev_no": ((1 - prob) - na) if na else None}


def _event(series, markets, target="2026-05-28"):
    return {"event_ticker": f"{series}-26MAY28", "target_date": target,
            "station": "x", "settled": False, "model": {"mu": 70.0},
            "markets": markets}


def test_qualify_event_yes_passes_clean(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    # favorite bucket (mid .57) is also the model's best YES, EV = .65-.52 = .13, tight spread
    mkts = [_mkt("64-65", 0.05, 0.04, 0.06, 0.95),
            _mkt("66-67", 0.65, 0.50, 0.52, 0.50)]   # model bucket = market favorite
    out = qualify_event(_event("KXHIGHNY", mkts))
    yes = [t for t in out if t["side"] == "YES"]
    assert len(yes) == 1
    assert yes[0]["bucket"] == "66-67" and yes[0]["ev"] == round(0.13, 4)
    assert yes[0]["market_price"] == 0.52 and yes[0]["size_pct"] > 0


def test_qualify_event_drops_yes_below_ev_bar(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    mkts = [_mkt("66-67", 0.55, 0.50, 0.52, 0.50)]  # EV .03 < .10
    assert qualify_event(_event("KXHIGHNY", mkts)) == []


def test_qualify_event_drops_yes_huge_edge(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    mkts = [_mkt("66-67", 0.90, 0.50, 0.52, 0.50)]  # EV .38 > .25
    assert qualify_event(_event("KXHIGHNY", mkts)) == []


def test_qualify_event_drops_yes_fighting_market(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    # model loves a cheap-flank bucket the market does NOT favor (>1 step away)
    mkts = [_mkt("60-61", 0.65, 0.50, 0.52, 0.50),   # model YES here (idx 0)
            _mkt("64-65", 0.10, 0.10, 0.12, 0.88),
            _mkt("70-71", 0.20, 0.55, 0.58, 0.42)]   # market favorite (idx 2), 2 steps away
    assert qualify_event(_event("KXHIGHNY", mkts)) == []


def test_qualify_event_drops_yes_wide_spread(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    mkts = [_mkt("66-67", 0.65, 0.46, 0.52, 0.50)]  # spread .06 > .05
    assert qualify_event(_event("KXHIGHNY", mkts)) == []


def test_qualify_event_no_passes_when_confident_and_early(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 24.0)
    # prob .05 -> printed NO .95 >= .90; no_ask .82 -> cal_ev_no = .95-.82 = .13
    mkts = [_mkt("80-81", 0.05, 0.02, 0.04, 0.82)]
    out = qualify_event(_event("KXHIGHMIA", mkts))
    no = [t for t in out if t["side"] == "NO"]
    assert len(no) == 1 and no[0]["market_price"] == 0.82


def test_qualify_event_drops_no_near_close(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 10.0)  # < 18
    mkts = [_mkt("80-81", 0.05, 0.02, 0.04, 0.82)]
    assert [t for t in qualify_event(_event("KXHIGHMIA", mkts)) if t["side"] == "NO"] == []


def test_qualifying_signals_skips_settled(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    ev = _event("KXHIGHNY", [_mkt("66-67", 0.65, 0.50, 0.52, 0.50)])
    ev["settled"] = True
    assert qualifying_signals([ev]) == []
```

- [ ] **Step 2: Run to verify fail.** `python -m pytest tests/test_signals.py -k "qualify_event or qualifying_signals" -v` → FAIL (names not defined).

- [ ] **Step 3: Implement.** Append to `signals.py`:

```python
def _ticket(side: str, pick: dict, event: dict, lead: float, price: float, ev: float) -> dict:
    series = event["event_ticker"].split("-")[0]
    return {
        "series": series,
        "city": kt.CITIES[series]["name"],
        "event_ticker": event["event_ticker"],
        "target_date": event["target_date"],
        "side": side,
        "bucket": pick["subtitle"],
        "ticker": pick["ticker"],
        "printed_prob": round(pick["cal_prob_yes"] if side == "YES" else pick["cal_prob_no"], 3),
        "market_price": round(price, 2),
        "ev": round(ev, 4),
        "size_pct": size_pct(ev, price),
        "lead_hours": round(lead, 1),
    }


def qualify_event(event: dict) -> list[dict]:
    """0-2 order tickets (YES and/or NO) for one event that pass the interim bar.
    Pure given the event dict (best_yes/no_pick are deterministic over markets)."""
    if event.get("settled"):
        return []
    markets = event.get("markets") or []
    series = event["event_ticker"].split("-")[0]
    lead = lead_hours_for(series, event["target_date"])
    tickets: list[dict] = []

    # YES
    y = kt.best_yes_pick(markets)
    if y is not None:
        ev = y.get("cal_ev_yes")
        ya, yb = y.get("yes_ask"), y.get("yes_bid")
        fav = market_favorite_index(markets)
        try:
            yi = markets.index(y)
        except ValueError:
            yi = None
        agree = fav is not None and yi is not None and abs(yi - fav) <= 1
        spread = (ya - yb) if (ya is not None and yb is not None) else 1.0
        if (ev is not None and YES_EV_MIN <= ev <= EV_MAX and agree
                and spread <= SPREAD_MAX and ya):
            tickets.append(_ticket("YES", y, event, lead, ya, ev))

    # NO
    n = kt.best_no_pick(markets)
    if n is not None and lead >= NO_LEAD_MIN:
        ev = n.get("cal_ev_no")
        na, prob = n.get("no_ask"), n.get("prob")
        ya, yb = n.get("yes_ask"), n.get("yes_bid")
        spread = (ya - yb) if (ya is not None and yb is not None) else 1.0
        if (ev is not None and YES_EV_MIN <= ev <= EV_MAX and prob is not None
                and (1 - prob) >= NO_PRINTED_MIN and spread <= SPREAD_MAX and na):
            tickets.append(_ticket("NO", n, event, lead, na, ev))

    return tickets


def qualifying_signals(events: list[dict]) -> list[dict]:
    """Flatten qualify_event over all (non-settled) events."""
    out: list[dict] = []
    for e in events:
        out.extend(qualify_event(e))
    return out
```

- [ ] **Step 4: Run to verify pass.** `python -m pytest tests/test_signals.py -v` → PASS (all). 

- [ ] **Step 5: Commit.**
```
git add signals.py tests/test_signals.py
git commit -m "signals: qualify_event gate + qualifying_signals (clean order tickets)"
```

---

### Task 3: `hourly_signals.py` runner + gitignore + JSON shape

**Files:**
- Create: `hourly_signals.py`
- Modify: `.gitignore`
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing test.** Append to `tests/test_signals.py`:

```python
def test_build_report_shape(monkeypatch):
    import signals
    monkeypatch.setattr(signals, "lead_hours_for", lambda *a, **k: 20.0)
    ev = _event("KXHIGHNY", [_mkt("64-65", 0.05, 0.04, 0.06, 0.95),
                             _mkt("66-67", 0.65, 0.50, 0.52, 0.50)])
    rep = signals.build_report([ev])
    assert rep["bar"] == "interim-2026-05-27"
    assert "generated_at" in rep and rep["n_open_events"] == 1
    assert isinstance(rep["picks"], list) and rep["picks"][0]["side"] == "YES"
```

- [ ] **Step 2: Run to verify fail.** `python -m pytest tests/test_signals.py -k build_report -v` → FAIL (no `build_report`).

- [ ] **Step 3a: Add `build_report` to `signals.py`.** Append:

```python
from datetime import timezone


def build_report(events: list[dict]) -> dict:
    """The report payload: only fully-qualifying picks (settled events skipped
    inside qualify_event)."""
    picks = qualifying_signals(events)
    n_open = sum(1 for e in events if not e.get("settled"))
    return {
        "bar": "interim-2026-05-27",
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "n_open_events": n_open,
        "picks": picks,
    }
```

- [ ] **Step 3b: Create `hourly_signals.py`** (the runner — fetches the live dashboard data, writes the JSON; read-only on the model):

```python
"""Hourly runner: pull live event data, apply the interim bar, write
hourly_signals.json. Started by the WeatherbotSignals scheduled task.
Read-only on the model — its ONLY write is hourly_signals.json."""
import json
import ssl
import sys
import urllib.request
from pathlib import Path

import signals

OUT_PATH = "hourly_signals.json"
MARKETS_URL = "https://127.0.0.1:8765/api/markets"


def fetch_events() -> list[dict]:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(MARKETS_URL, timeout=30, context=ctx) as r:
        return json.loads(r.read().decode())


def main() -> int:
    events = fetch_events()
    report = signals.build_report(events)
    Path(OUT_PATH).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"{report['generated_at']}: {len(report['picks'])} qualifying "
          f"({report['n_open_events']} open events)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3c: gitignore the runtime artifact.** Append `hourly_signals.json` on its own line to `.gitignore` (put a comment line `# hourly signal report (runtime artifact)` above it).

- [ ] **Step 4: Run to verify.** `python -m pytest tests/test_signals.py -v` → all PASS. Then full `python -m pytest -q` → green.

- [ ] **Step 5: Commit.**
```
git add signals.py hourly_signals.py tests/test_signals.py .gitignore
git commit -m "signals: build_report + hourly_signals.py runner (writes hourly_signals.json)"
```

---

### Task 4: `/api/signals` endpoint + dashboard panel

**Files:**
- Modify: `kalshi_temp.py` (`do_GET` routing; `DASHBOARD_HTML`)
- Test: `tests/test_signals.py`

- [ ] **Step 1: Write failing test** for the staleness helper. Append to `tests/test_signals.py`:

```python
def test_signals_payload_marks_stale(tmp_path, monkeypatch):
    import json as _json, signals
    from datetime import datetime, timezone, timedelta
    p = tmp_path / "hourly_signals.json"
    old = (datetime.now(timezone.utc) - timedelta(minutes=120)).astimezone().isoformat()
    p.write_text(_json.dumps({"bar": "interim-2026-05-27", "generated_at": old,
                              "n_open_events": 3, "picks": []}))
    payload = signals.read_report_payload(str(p), stale_after_min=90)
    assert payload["stale"] is True

    fresh = datetime.now(timezone.utc).astimezone().isoformat()
    p.write_text(_json.dumps({"bar": "interim-2026-05-27", "generated_at": fresh,
                              "n_open_events": 3, "picks": []}))
    assert signals.read_report_payload(str(p), stale_after_min=90)["stale"] is False


def test_signals_payload_missing_file():
    import signals
    payload = signals.read_report_payload("does_not_exist.json", stale_after_min=90)
    assert payload["stale"] is True and payload["picks"] == []
```

- [ ] **Step 2: Run to verify fail.** `python -m pytest tests/test_signals.py -k signals_payload -v` → FAIL (`read_report_payload` undefined).

- [ ] **Step 3a: Add `read_report_payload` to `signals.py`.** Append:

```python
def read_report_payload(path: str = "hourly_signals.json", *, stale_after_min: int = 90) -> dict:
    """Read the latest report for serving; mark stale if missing or old."""
    p = Path(path)
    if not p.exists():
        return {"bar": "interim-2026-05-27", "generated_at": None,
                "n_open_events": 0, "picks": [], "stale": True}
    data = json.loads(p.read_text(encoding="utf-8"))
    gen = data.get("generated_at")
    stale = True
    if gen:
        try:
            age_min = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(gen).astimezone(timezone.utc)).total_seconds() / 60.0
            stale = age_min > stale_after_min
        except Exception:
            stale = True
    data["stale"] = stale
    return data
```
(Add `import json` at the top of `signals.py` if not already present.)

- [ ] **Step 3b: Add the `/api/signals` route** in `kalshi_temp.py` `do_GET`, immediately after the `/api/markets` block (after line ~1547):

```python
        if path == "/api/signals":
            try:
                import signals as _signals
                self._send(200, "application/json",
                           json.dumps(_signals.read_report_payload()).encode())
            except Exception as e:
                self._send(500, "application/json",
                           json.dumps({"error": str(e)}).encode())
            return
```

- [ ] **Step 3c: Add the dashboard panel.** In `DASHBOARD_HTML`, find the main container the markets render into (search for the element that `render(...)` populates, e.g. the `<div id="app">`/`<main>` near the top of `<body>`). Insert this panel container as the FIRST child of that main area:

```html
<section id="signals" class="card" style="border:2px solid #2d7;margin-bottom:12px">
  <div style="display:flex;justify-content:space-between;align-items:center">
    <b>Qualifying Signals — interim bar</b>
    <span id="signals-updated" style="font-size:12px;color:#888"></span>
  </div>
  <div id="signals-body" style="margin-top:6px">…</div>
</section>
```

Add this JS to the dashboard `<script>` block and call `renderSignals()` on load and in the existing refresh cycle:

```javascript
async function renderSignals() {
  try {
    const r = await fetch('/api/signals'); const d = await r.json();
    const upd = document.getElementById('signals-updated');
    const body = document.getElementById('signals-body');
    if (d.generated_at) {
      const ago = Math.round((Date.now() - new Date(d.generated_at)) / 60000);
      upd.textContent = 'Last updated: ' + new Date(d.generated_at).toLocaleString() + ' (' + ago + 'm ago)';
      upd.style.color = d.stale ? '#c33' : '#888';
    } else { upd.textContent = 'no report yet'; upd.style.color = '#c33'; }
    if (!d.picks || !d.picks.length) { body.textContent = 'No qualifying picks this hour.'; return; }
    body.innerHTML = d.picks.map(p =>
      `<div style="padding:4px 0;border-top:1px solid #eee">
        <b>${p.city} ${p.target_date.slice(5)}</b> · BUY <b>${p.side}</b> ${p.bucket}
        @ <b>${Math.round(p.market_price*100)}¢</b> · EV ${Math.round(p.ev*100)}¢
        · size <b>${p.size_pct}%</b> · lead ${p.lead_hours}h
        <span style="font-size:11px;color:#999"> ${p.ticker}</span>
      </div>`).join('');
  } catch (e) { document.getElementById('signals-body').textContent = 'signals error: ' + e; }
}
renderSignals();
```
If the dashboard has a periodic refresh (e.g. a `setInterval` calling `load()`/`render()`), add a `renderSignals()` call there too. If unsure where the refresh loop is, add `setInterval(renderSignals, 60000);` after the `renderSignals();` call.

- [ ] **Step 4: Run tests + a syntax check.** `python -m pytest tests/test_signals.py -v` (all pass) and `python -c "import ast; ast.parse(open('kalshi_temp.py').read())"` (no syntax error). Full `python -m pytest -q` green.

- [ ] **Step 5: Commit.**
```
git add kalshi_temp.py signals.py tests/test_signals.py
git commit -m "signals: /api/signals endpoint + dashboard 'Qualifying Signals' panel"
```

---

### Task 5: scheduled-task bat + registration note

**Files:**
- Create: `run_signals.bat`

- [ ] **Step 1: Create `run_signals.bat`** (mirrors `run_dashboard.bat`; staggered to :05):

```bat
@echo off
REM Hourly qualifying-signal report. Started by the WeatherbotSignals task.
REM Read-only on the model; writes only hourly_signals.json. Not committed.
cd /d "C:\Users\KrisKnecht\weatherbot"
"C:\Users\KrisKnecht\AppData\Local\Programs\Python\Python313\python.exe" hourly_signals.py >> signals.log 2>&1
```

- [ ] **Step 2: Verify it runs** (server must be up). Run from the worktree root:
`cd /c/Users/KrisKnecht/weatherbot/.claude/worktrees/signal-report && python hourly_signals.py && head -20 hourly_signals.json`
Expected: prints a summary line; `hourly_signals.json` written with `generated_at` + `picks`. If `/api/markets` is unreachable from the worktree (the running server serves the live checkout), that's fine — the real run happens from the live checkout post-merge; just confirm no code error (a connection error is acceptable here, a traceback in `signals`/`hourly_signals` is not).

- [ ] **Step 3: Commit.**
```
git add run_signals.bat
git commit -m "signals: hourly run_signals.bat scheduled-task launcher"
```

- [ ] **Step 4: Registration note (operator runs once post-merge — NOT executed by the plan).** Daily-hourly task, staggered to :05:
```
schtasks /Create /TN WeatherbotSignals /TR "C:\Users\KrisKnecht\weatherbot\run_signals.bat" /SC HOURLY /MO 1 /ST 00:05 /RL LIMITED /F
```

---

## Post-build (controller, in-session — not plan tasks)
- Merge `signal-report` → `model-lab`; restart the dashboard (deploys the panel + `/api/signals`).
- Register `WeatherbotSignals` (schtasks above).
- Run `hourly_signals.py` once from the live checkout → first `hourly_signals.json`; verify the panel shows it.
- Arm the push: `CronCreate(durable, recurring, cron "12 * * * *")` whose prompt reads `hourly_signals.json` and `PushNotification`s the compact tickets (or "no qualifying picks"). Fire the first push now. (7-day cron expiry — re-arm as needed.)

## Self-review
- **Spec coverage:** interim bar incl. YES EV 10–25¢ + market-agreement + spread (Task 2); NO printed≥0.90 + EV 10–25¢ + lead≥18 + sanity (best_no_pick) (Task 2); clean order tickets w/ size% (Task 1 `size_pct` + Task 2 `_ticket`); strict gate / no contingent (qualify_event returns only passers; settled skipped); JSON + runner (Task 3); panel + "last updated" + stale (Task 4); Windows task (Task 5); push cron + first run (post-build). ✓
- **No placeholders:** all code concrete. The one findable anchor is the DASHBOARD_HTML main container (Task 4 Step 3c) — described with a search cue + fallback.
- **Type consistency:** ticket dict keys (`side, bucket, ticker, market_price, ev, size_pct, lead_hours, printed_prob, city, target_date`) consistent across `_ticket`, tests, and the panel JS. `read_report_payload`/`build_report`/`qualify_event` signatures consistent.
- **Guardrail:** `hourly_signals.py` writes only `hourly_signals.json`; `signals.py` has no writes; `/api/signals` is read-only. No trade calls anywhere.
