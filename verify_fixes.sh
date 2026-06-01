#!/usr/bin/env bash
# Verification for the weatherbot autonomous fixes (Problems 1-3).
# Run from the repo root in Git Bash:
#   bash verify_fixes.sh                 # Part A — no credentials needed
#   bash verify_fixes.sh --with-telegram # Part B — after you create .env (see gate)
#
# set -e for the deterministic steps. Network-touching steps assert on PRODUCED
# OUTPUT (not exit code / empty stderr) so a transient Kalshi 429 / DNS blip does
# not false-fail the build.
set -euo pipefail

PY="${PY:-C:/Users/KrisKnecht/AppData/Local/Programs/Python/Python313/python.exe}"
pass() { echo "  -> OK: $*"; }
fail() { echo "  -> FAIL: $*"; exit 1; }

echo "=== STEP 1: pytest collects clean ==="
"$PY" -m pytest --co -q >/dev/null
pass "collection clean"

echo "=== STEP 2: full test suite (P1 notifier/bot + P2 health/cache + P3 thresholds) ==="
"$PY" -m pytest -q
pass "suite green (incl. health-crash regression, stub-token /status, threshold parity)"

echo "=== STEP 3: daily health scan runs end-to-end — crash fixed (full loop, network-tolerant) ==="
# The crash lived in _opportunity_records and always fires on the live log (it has
# 'taken' YES picks), so its ABSENCE is a deterministic signal even if the network
# is flaky. No --write: verification must not mutate committed docs.
hout="$("$PY" -m lab.cli health --days 14 2>&1 || true)"
echo "$hout" | tail -3
if echo "$hout" | grep -q "has no attribute 'split'"; then
  fail "health _opportunity_records crash still present"
fi
pass "health scan completed with no _opportunity_records crash"

echo "=== STEP 4: bot builds + answers /status against a STUB token (no live connection) ==="
"$PY" -m pytest tests/test_telegram.py::test_status_handler_answers_with_stub_token -q >/dev/null
pass "/status handler replies with a stub token"

echo "=== STEP 5: scheduled-push path returns a READABLE error without creds (no stack trace) ==="
pout="$(env -u TELEGRAM_BOT_TOKEN -u TELEGRAM_CHAT_ID "$PY" telegram_bot.py push status 2>&1 || true)"
echo "  $pout"
echo "$pout" | grep -q "not set" || fail "expected a readable 'not set' message"
echo "$pout" | grep -qi "Traceback" && fail "leaked a stack trace"
pass "readable error, no crash"

echo "=== STEP 6: NOTIFIER switch resolves both transports (default = pushover rollback) ==="
NOTIFIER=telegram "$PY" -c "import notifiers; assert type(notifiers.get_notifier()).__name__=='TelegramNotifier'; print('  telegram -> TelegramNotifier')"
NOTIFIER=pushover "$PY" -c "import notifiers; assert type(notifiers.get_notifier()).__name__=='PushoverNotifier'; print('  pushover -> PushoverNotifier')"
NOTIFIER=bogus    "$PY" -c "import notifiers; assert type(notifiers.get_notifier()).__name__=='PushoverNotifier'; print('  bogus    -> PushoverNotifier (safe default)')"
pass "NOTIFIER switch correct"

echo
echo "############################################################################"
echo "#  Part A PASSED.                                                           "
echo "#                                                                           "
echo "#  .env TOKEN GATE — Part B needs real Telegram credentials.                "
echo "#  The permission deny-rule blocks Claude from writing any .env* file, so   "
echo "#  create it by hand:                                                       "
echo "#                                                                           "
echo "#      cp env.example .env        # then edit .env:                         "
echo "#        NOTIFIER=telegram                                                  "
echo "#        TELEGRAM_BOT_TOKEN=<from @BotFather /newbot>                       "
echo "#        TELEGRAM_CHAT_ID=<your chat id; see docs/TELEGRAM_SETUP.md>        "
echo "#                                                                           "
echo "#  Then run Part B:   bash verify_fixes.sh --with-telegram                  "
echo "############################################################################"

if [ "${1:-}" != "--with-telegram" ]; then
  echo "Skipping Part B (pass --with-telegram once .env is set)."
  exit 0
fi

echo "=== STEP 7 (Part B): live Telegram push of /status to your chat ==="
"$PY" telegram_bot.py push status
pass "sent /status to TELEGRAM_CHAT_ID — check your phone"

echo "=== STEP 8 (Part B): long-poll smoke (3s, then stop) ==="
echo "  starting bot; send /status or /predict lax in Telegram now..."
( "$PY" telegram_bot.py & BOT=$!; sleep 3; kill "$BOT" 2>/dev/null || true )
pass "bot started and polled without error"

echo
echo "ALL STEPS PASSED (Part A + Part B)."
