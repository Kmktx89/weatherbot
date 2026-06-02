# Telegram-only notifier consolidation — design

**Date:** 2026-06-01 · **Status:** approved design, pending implementation plan · **Type:** notifier-layer refactor

## Problem

The notification layer supports two transports (Pushover, Telegram) selected by
the `NOTIFIER` env switch, defaulting to Pushover. The operator's Pushover
connection does not work and Telegram is the preferred (and operational)
transport. Maintaining a dual-transport switch with a broken default is dead
weight and a foot-gun (a stray/unset `NOTIFIER` silently routes to the broken
transport).

## Goal

Make Telegram the sole notification transport. Remove Pushover entirely. Keep the
`get_notifier()` abstraction so callers are unchanged in shape. All transport
selection in code — never touch `.env` (it is deny-guarded; Telegram credentials
already live there because the Telegram bot runs).

## Non-goals (YAGNI)

- No change to `signals`, the qualifying-mark logic, the dashboard, or the T-24
  card *content* — that is sub-project B (`2026-06-01-t24-qualifying-mark-design`).
- No new transports, no broadcast/multi-transport fan-out (single transport now).
- (Decision reversed per operator: `pushover_config.json` IS deleted — see §7.)

## Architecture

`get_notifier()` always returns `TelegramNotifier`. The `Notifier` base interface
(`available()` / `send(title, message)`) and every call site (`alerts.py`,
`generate_t24_card.py`) are unchanged in shape — they keep calling
`get_notifier()`; only the returned transport changes. Pushover code is deleted,
not merely defaulted away.

## Changes

### 1. `notifiers/__init__.py`
- `get_notifier(name=None)` always returns `TelegramNotifier()`.
- Remove the `from notifiers.pushover import PushoverNotifier` import, the
  name-switch body, and `PushoverNotifier` from `__all__`.
- Keep the `name` parameter (callers pass it; tests pass explicit names) but
  ignore it for selection — there is only one transport.
- No longer imports or calls `wb_config.notifier_name()` (removed in §4).

### 2. `notifiers/pushover.py`
- Delete the file.

### 3. `alerts.py`
- Remove the now-dead Pushover-only members: `send_pushover`, `PUSHOVER_URL`,
  `CONFIG_PATH`, and the `pushover_config.json` loader (`load_config` — its only
  consumer is the deleted `notifiers/pushover.py`, confirmed; `send_t24_alerts`
  does not use it).
- Update the module docstring (header + the "pushover_config.json" notes) to
  describe Telegram.
- `send_t24_alerts` already sends via `get_notifier()` — unchanged in logic, now
  routes to Telegram. Update its stale inline comment ("Transport is chosen by the
  NOTIFIER env switch (telegram | pushover); default pushover preserves the legacy
  path") to reflect the single Telegram transport. Keep `format_card` /
  `build_message` (transport-agnostic).

### 4. `wb_config.py`
- Remove `notifier_name()` entirely. After §1 (`get_notifier` stops using it) and
  §4b (`/status` line dropped), it has no consumers — so the function and the
  `NOTIFIER` env-switch concept are fully retired. No vestigial default, no dead
  code, one fewer foot-gun.

### 4b. `telegram_commands.py`
- In `cmd_status`, drop the `f"notifier: {wb_config.notifier_name()}"` line (and
  the now-unused `import wb_config` if it is local to that function). With a single
  transport the line is noise.

### 5. `generate_t24_card.py`
- Rename `maybe_pushover` → `maybe_notify` and update its two call sites
  (`main()` blocked-path and normal-path).
- Update Pushover-referencing docstrings/comments (module header, the digest
  docstrings, "BLOCKED Pushover" → "BLOCKED notify"). Logic unchanged — already
  routes through `get_notifier()`.

### 6. `notifiers/base.py`
- Update the transport-swap docstring comment to reflect a single (Telegram)
  transport.

### 7. `pushover_config.json`
- Delete it — it is a dead **credentials file** (Pushover user-key + app token)
  once the code path is gone; orphaned secrets should not linger. It is gitignored,
  so it lives only in the operating checkout, not in git/worktrees — delete it as a
  **deploy-step filesystem cleanup on the main checkout**, not a tracked change.

## Error handling

No behavioral change. `TelegramNotifier.available()` already guards on
`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`; when unavailable, the existing call
sites no-op exactly as before (the digest and alerts both check
`notifier.available()`). Removing Pushover removes a failure mode (the broken
default), it does not add one.

## Testing (TDD — update existing pins first)

`tests/test_telegram.py`:
- `test_get_notifier_switch` — rewrite (rename to `test_get_notifier_is_telegram`):
  assert `get_notifier()` returns `TelegramNotifier` with no arg and with an
  explicit `name`; remove the `wb_config.notifier_name` monkeypatching and the
  `PushoverNotifier` import/assertions (both removed).
- Add `test_get_notifier_pushover_name_now_telegram` — `get_notifier("pushover")`
  returns `TelegramNotifier` (proves Pushover is removed, not just de-defaulted).
- `test_cmd_status_reads_artifacts` — remove the `assert "notifier:" in out`
  assertion (the `/status` notifier line is dropped); keep the other status
  assertions (`"weatherbot status"`, signals freshness).
- The digest tests that call `maybe_pushover` (the "uses get_notifier" recorder
  test and the "no-raise when transport unavailable" test) — rename the calls to
  `maybe_notify`; keep their assertions (digest routes through `get_notifier`; no
  raise when the transport is unavailable).
- Confirm no remaining test imports `PushoverNotifier` or references
  `send_pushover` / `notifier_name`.

Full-suite gate: `python -m pytest -q` green. Grep gate: no `pushover` /
`PushoverNotifier` / `send_pushover` / `notifier_name` references remain in `.py`.

## Deploy

Gated pipeline: TDD → spec/code review → validation → human-gated deploy. Held on
the lab branch; PR into `main`, then cherry-pick onto `model-lab` (the branch the
scheduled tasks run from), matching the bias-monitoring deploy flow. On deploy,
append a `MODEL_CHANGES.md`-style note is NOT required (this is infra, not a model
change) — but note the transport change in the PR description.

## Follow-up (separate)

Sub-project B — `2026-06-01-t24-qualifying-mark-design`: mark `qualify_event`-passing
picks in the T-24 card (dashboard + the now-Telegram digest). Depends on this.
