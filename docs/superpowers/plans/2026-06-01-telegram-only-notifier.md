# Telegram-only Notifier Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Telegram the sole notification transport and remove Pushover entirely (code, config, the `NOTIFIER` switch, and the `/status` notifier line).

**Architecture:** `get_notifier()` always returns `TelegramNotifier`; the `Notifier` interface and all call sites (`alerts.py`, `generate_t24_card.py`) are unchanged in shape. Pushover code is deleted, not defaulted away. Transport selection is entirely in code — `.env` is never touched.

**Tech Stack:** Python 3.13, pytest. Files: `notifiers/{__init__,base,pushover}.py`, `alerts.py`, `wb_config.py`, `generate_t24_card.py`, `telegram_commands.py`, `tests/test_telegram.py`.

**Branch:** worktree `worktree-telegram-only-notifier`. Gated deploy — PR into `main`, then cherry-pick onto `model-lab`. Do NOT merge to a deploy branch directly.

**Ordering note:** tasks are sequenced so the full suite stays green after every commit. Pushover is unimported (Task 1) before its file is deleted (Task 2); `notifier_name` loses its consumers (Tasks 1, 4) before removal (Task 4).

---

## File Structure

- `notifiers/__init__.py` — **Modify.** `get_notifier()` → always Telegram; drop Pushover import + switch.
- `notifiers/pushover.py` — **Delete.**
- `notifiers/base.py` — **Modify.** Docstring: single transport.
- `alerts.py` — **Modify.** Remove `send_pushover`/`load_config`/`PUSHOVER_URL`/`CONFIG_PATH` + now-unused `json`/`requests` imports; fix docstring + stale comment.
- `wb_config.py` — **Modify.** Remove `notifier_name()`.
- `telegram_commands.py` — **Modify.** Drop the `notifier:` line from `cmd_status`.
- `generate_t24_card.py` — **Modify.** Rename `maybe_pushover` → `maybe_notify`; Pushover→Telegram docstrings.
- `tests/test_telegram.py` — **Modify.** Pin Telegram-only; rename call sites; drop the `/status` notifier assertion.

---

## Task 1: `get_notifier()` is Telegram-only

**Files:**
- Modify: `notifiers/__init__.py`
- Modify: `notifiers/base.py:2` (docstring)
- Test: `tests/test_telegram.py:105-114`

- [ ] **Step 1: Replace the test**

In `tests/test_telegram.py`, replace the entire `test_get_notifier_switch` function (lines 105-114) with:

```python
def test_get_notifier_is_telegram():
    from notifiers import get_notifier, TelegramNotifier
    assert isinstance(get_notifier(), TelegramNotifier)
    assert isinstance(get_notifier("telegram"), TelegramNotifier)


def test_get_notifier_pushover_name_now_telegram():
    # Pushover removed: even the legacy name resolves to Telegram.
    from notifiers import get_notifier, TelegramNotifier
    assert isinstance(get_notifier("pushover"), TelegramNotifier)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_telegram.py::test_get_notifier_pushover_name_now_telegram -q`
Expected: FAIL — currently `get_notifier("pushover")` returns a `PushoverNotifier`, not `TelegramNotifier`.

- [ ] **Step 3: Rewrite the factory**

Replace the entire contents of `notifiers/__init__.py` with:

```python
"""Notifier factory. Telegram is the sole transport; `get_notifier()` always
returns it. (Pushover was removed 2026-06-01 — it was non-functional.)"""
from notifiers.base import Notifier
from notifiers.telegram import TelegramNotifier

__all__ = ["Notifier", "TelegramNotifier", "get_notifier"]


def get_notifier(name: str | None = None) -> Notifier:
    """Return the notification transport. Single transport (Telegram); the
    optional `name` is accepted for call-site compatibility but ignored."""
    return TelegramNotifier()
```

Also update the `notifiers/base.py` docstring (line 2) so the package is consistent. Change:
```python
"""Notifier interface — the single contract scheduled pushes go through, so the
transport (Pushover vs Telegram) is swappable via the NOTIFIER env switch."""
```
to:
```python
"""Notifier interface — the single contract scheduled pushes go through.
Telegram is the only transport (Pushover was removed 2026-06-01)."""
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/test_telegram.py -k get_notifier -q`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add notifiers/__init__.py notifiers/base.py tests/test_telegram.py
git commit -m "refactor(notifiers): get_notifier always returns Telegram"
```

---

## Task 2: Delete `notifiers/pushover.py`

**Files:**
- Delete: `notifiers/pushover.py`

- [ ] **Step 1: Confirm nothing imports it**

Run: `grep -rn "PushoverNotifier\|notifiers.pushover\|notifiers import.*Pushover" --include=*.py .`
Expected: no matches (Task 1 removed the factory import and the test import).

- [ ] **Step 2: Delete the file**

```bash
git rm notifiers/pushover.py
```

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS (no import errors; `pushover.py` was unreferenced).

- [ ] **Step 4: Commit**

```bash
git commit -m "refactor(notifiers): delete pushover transport"
```

---

## Task 3: Strip Pushover members from `alerts.py`

**Files:**
- Modify: `alerts.py` (docstring; remove `json`/`requests` imports, `PUSHOVER_URL`, `CONFIG_PATH`, `load_config`, `send_pushover`; fix `send_t24_alerts` comment)

- [ ] **Step 1: Replace the module docstring**

Replace `alerts.py` lines 1-18 (the docstring) with:

```python
"""alerts.py — Batched T-24h alerts for active KXHIGH events.

Called from snapshot.py after each hourly fire. Filters the just-written
rows to those whose lead_hours sits in the T-24h window (23.5h to 24.5h),
formats a per-event card with calibration-adjusted YES + NO recommendations
following MODEL_NOTES.md tables, and sends a single batched alert via the
Telegram transport (notifiers.get_notifier()).

Dedup: alerts_sent_tickers.txt — append-only, one event_ticker per line.
Each event_ticker fires at most one alert in its lifetime.

If the transport is unavailable (no TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
the module logs and no-ops; it never raises out to the caller.
"""
```

- [ ] **Step 2: Remove the now-unused imports**

In `alerts.py`, delete the line `import json` and the line `import requests`. (After Step 3/4 nothing in the file uses them — `json`/`requests` were only used by `load_config`/`send_pushover`.) Keep `import sys`, `from pathlib import Path`, and `import kalshi_temp as kt`.

- [ ] **Step 3: Remove the Pushover constants and `load_config`**

Delete these lines:
```python
PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
CONFIG_PATH = Path("pushover_config.json")
```
and the entire `load_config` function:
```python
def load_config():
    if not CONFIG_PATH.exists():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(cfg, dict):
        return None
    if not cfg.get("user_key") or not cfg.get("api_token"):
        return None
    return cfg
```
Keep `SENT_PATH`, `T24_WINDOW`, `TAKE_EV_THRESHOLD`, `SPREAD_FLAG`.

- [ ] **Step 4: Remove `send_pushover`**

Delete the entire `send_pushover` function:
```python
def send_pushover(title: str, message: str, cfg: dict,
                  *, priority: int = 0, timeout: float = 10.0) -> tuple[bool, str]:
    resp = requests.post(PUSHOVER_URL, data={
        "token": cfg["api_token"],
        "user": cfg["user_key"],
        "title": title,
        "message": message,
        "priority": priority,
    }, timeout=timeout)
    return resp.status_code == 200, resp.text
```

- [ ] **Step 5: Fix the stale comment in `send_t24_alerts`**

Replace:
```python
    # Transport is chosen by the NOTIFIER env switch (telegram | pushover);
    # default pushover preserves the legacy path exactly (rollback).
    from notifiers import get_notifier
```
with:
```python
    # Single transport (Telegram) via the notifier factory.
    from notifiers import get_notifier
```

- [ ] **Step 6: Run the full suite**

Run: `python -m pytest -q`
Expected: PASS. (`test_send_t24_alerts_routes_through_notifier` still passes — it mocks `get_notifier`; `test_notify_paths_no_crash_when_unavailable` still passes — `send_t24_alerts([])` returns 0.)

- [ ] **Step 7: Commit**

```bash
git add alerts.py
git commit -m "refactor(alerts): remove dead Pushover send path"
```

---

## Task 4: Remove `notifier_name()` + drop the `/status` notifier line

**Files:**
- Modify: `wb_config.py:36-39` (remove `notifier_name`)
- Modify: `telegram_commands.py` (`cmd_status`)
- Test: `tests/test_telegram.py:35`

- [ ] **Step 1: Update the status test (RED)**

In `tests/test_telegram.py`, in `test_cmd_status_reads_artifacts`, change:
```python
    assert "notifier:" in out
```
to:
```python
    assert "notifier:" not in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_telegram.py::test_cmd_status_reads_artifacts -q`
Expected: FAIL — `cmd_status` still emits a `notifier:` line.

- [ ] **Step 3: Drop the line from `cmd_status`**

In `telegram_commands.py` `cmd_status`, delete the local `import wb_config` line and change:
```python
    lines = ["weatherbot status", f"notifier: {wb_config.notifier_name()}"]
```
to:
```python
    lines = ["weatherbot status"]
```

- [ ] **Step 4: Remove `notifier_name` from `wb_config.py`**

Delete the entire function (and its preceding blank line) from `wb_config.py`:
```python
def notifier_name() -> str:
    """Which transport scheduled pushes use: 'telegram' or 'pushover'.
    Defaults to 'pushover' so an unset/typo'd value is a safe no-change."""
    return (get("NOTIFIER", "pushover") or "pushover").strip().lower()
```

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_telegram.py::test_cmd_status_reads_artifacts -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add wb_config.py telegram_commands.py tests/test_telegram.py
git commit -m "refactor: remove notifier_name + NOTIFIER switch, drop /status notifier line"
```

---

## Task 5: Rename `maybe_pushover` → `maybe_notify`

**Files:**
- Modify: `generate_t24_card.py` (def + 2 call sites + docstrings)
- Test: `tests/test_telegram.py:152-162`

- [ ] **Step 1: Update the test (RED)**

In `tests/test_telegram.py`, replace `test_notify_paths_no_crash_when_unavailable` (lines 152-162) with:

```python
def test_notify_paths_no_crash_when_unavailable(monkeypatch, tmp_path):
    """Both rewritten notify paths no-op cleanly when the Telegram transport is
    unavailable (no .env creds here) — no raise, no cycle."""
    monkeypatch.chdir(tmp_path)
    import alerts
    import generate_t24_card as g
    assert alerts.send_t24_alerts([]) == 0
    g.maybe_notify(None, [], "2026-06-01")        # must not raise
    g.maybe_notify(None, [], "2026-06-01", blocked=True)
    g.push_catchup([], "2026-06-01")              # must not raise
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_telegram.py::test_notify_paths_no_crash_when_unavailable -q`
Expected: FAIL — `AttributeError: module 'generate_t24_card' has no attribute 'maybe_notify'`.

- [ ] **Step 3: Rename the function and call sites**

In `generate_t24_card.py`:
- Change `def maybe_pushover(qc_summary, events, today_iso, blocked=False):` to `def maybe_notify(qc_summary, events, today_iso, blocked=False):`.
- Change the blocked-path call `maybe_pushover(qc_summary, events, today_iso, blocked=True)` to `maybe_notify(qc_summary, events, today_iso, blocked=True)`.
- Change the normal-path call `maybe_pushover(qc_summary, events, today_iso, blocked=False)` to `maybe_notify(qc_summary, events, today_iso, blocked=False)`.

- [ ] **Step 4: Update the Pushover-referencing docstrings**

In `generate_t24_card.py`, replace these docstring fragments (functional text unchanged, only "Pushover" → "Telegram"/"notify"):

- Module docstring: `Pushover fires with a digest` → `a Telegram notification fires with a digest`; `A separate "BLOCKED" Pushover fires only` → `A separate "BLOCKED" notification fires only`.
- `format_digest_event` docstring: `the daily Pushover digest` → `the daily notification digest`.
- `build_digest_body` docstring: `the full Pushover body` → `the full notification body`.
- `maybe_notify` docstring: `"""Daily Pushover:` → `"""Daily notification:`; `Quietly no-ops if pushover_config.json is absent (matches alerts.py)."""` → `Quietly no-ops if the Telegram transport is unavailable (matches alerts.py)."""`.
- `push_catchup` docstring: `One Pushover summarizing` → `One notification summarizing`; and `fire one Pushover with the newly-filled events.` → `fire one notification with the newly-filled events.`.

- [ ] **Step 5: Run to verify it passes**

Run: `python -m pytest tests/test_telegram.py::test_notify_paths_no_crash_when_unavailable -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add generate_t24_card.py tests/test_telegram.py
git commit -m "refactor(t24): rename maybe_pushover -> maybe_notify"
```

---

## Task 6: Validation gate

**Files:** none (verification only)

- [ ] **Step 1: Full suite green**

Run: `python -m pytest -q`
Expected: PASS, zero failures.

- [ ] **Step 2: Grep gate — no Pushover/notifier_name residue in code**

Run: `grep -rin "pushover\|PushoverNotifier\|send_pushover\|notifier_name" --include=*.py .`
Expected: no matches. (If any docstring/comment still says "pushover", fix it — the only acceptable remaining `pushover` string anywhere is the gitignored `pushover_config.json` file itself, which is not a `.py`.)

- [ ] **Step 3: Import smoke test**

Run: `python -c "import alerts, generate_t24_card, telegram_commands, wb_config; from notifiers import get_notifier; print(type(get_notifier()).__name__)"`
Expected: prints `TelegramNotifier`, no ImportError.

- [ ] **Step 4: Note the deploy-step config deletion**

`pushover_config.json` is gitignored and absent from this worktree, so there is nothing to delete here. It must be deleted from the **main operating checkout** at deploy time (dead credentials file). Record this in the PR description as a manual deploy step:
> Deploy step: `rm C:/Users/KrisKnecht/weatherbot/pushover_config.json` (dead Pushover credentials).

---

## Self-Review notes (reconciled)

- **Spec coverage:** §1 get_notifier → Task 1; §2 delete pushover.py → Task 2; §3 alerts.py → Task 3; §4 remove notifier_name → Task 4; §4b /status line → Task 4; §5 rename maybe_notify → Task 5; §6 base.py docstring → *see note below*; §7 pushover_config.json delete → Task 6 Step 4 (deploy step); tests → folded into Tasks 1/4/5; grep gate → Task 6.
- **base.py docstring (§6):** done in Task 1 Step 3 (committed with the factory change) so the notifiers package is internally consistent in one commit.
- **Green-at-each-commit:** verified per the ordering note.
- **Type/name consistency:** `maybe_notify(qc_summary, events, today_iso, blocked=False)` signature identical to the old `maybe_pushover`; `get_notifier(name=None)` signature unchanged; `push_catchup` NOT renamed (only its docstring updated).
- **No new behavior:** every task is deletion/rename/docstring; the only logic touched is `get_notifier`'s return (now constant).
