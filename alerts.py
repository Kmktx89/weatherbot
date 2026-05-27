"""alerts.py — Batched T-24h Pushover alerts for active KXHIGH events.

Called from snapshot.py after each hourly fire. Filters the just-written
rows to those whose lead_hours sits in the T-24h window (23.5h to 24.5h),
formats a per-event card with calibration-adjusted YES + NO recommendations
following MODEL_NOTES.md tables, and POSTs a single batched alert.

Token config (gitignored): pushover_config.json at project root —
    {"user_key": "...", "api_token": "..."}
Get the user_key from https://pushover.net dashboard; create an application
token at https://pushover.net/apps/build.

Dedup: alerts_sent_tickers.txt — append-only, one event_ticker per line.
Each event_ticker fires at most one alert in its lifetime.

If pushover_config.json is absent or invalid the module logs and no-ops; it
never raises out to the caller.
"""
import json
import sys
from pathlib import Path

import requests

import kalshi_temp as kt


PUSHOVER_URL = "https://api.pushover.net/1/messages.json"
CONFIG_PATH = Path("pushover_config.json")
SENT_PATH = Path("alerts_sent_tickers.txt")
T24_WINDOW = (23.5, 24.5)
TAKE_EV_THRESHOLD = 0.05    # ≥ 5¢ calibrated EV → TAKE
SPREAD_FLAG = 0.05          # yes spread > 5¢ → flag in alert


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


def _sent_tickers() -> set[str]:
    if not SENT_PATH.exists():
        return set()
    return {ln.strip() for ln in SENT_PATH.read_text(encoding="utf-8").splitlines() if ln.strip()}


def _record_sent(ticker: str) -> None:
    with SENT_PATH.open("a", encoding="utf-8") as f:
        f.write(ticker + "\n")


def _fmt_pct(x: float | None) -> str:
    return "--" if x is None else f"{x * 100:.0f}"


def _fmt_signed_cents(x: float | None) -> str:
    return "--" if x is None else f"{x * 100:+.0f}"


def format_card(row: dict) -> str:
    """5-line text card for one event row from live_picks_log.jsonl."""
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
        # SKIP-thin is a guard for if TAKE_EV_THRESHOLD is ever raised above
        # MIN_BEST_EV; today they're equal so surfaced picks are always TAKE.
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


def build_message(rows: list[dict]) -> str:
    return "\n\n".join(format_card(r) for r in rows)


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


def send_t24_alerts(rows: list[dict], *, logger=None) -> int:
    """Filter rows to T-24h window, dedup by ticker, fire one batched alert.

    Returns the number of events alerted on (0 on no-op / config-missing).
    """
    log = logger or (lambda m: print(m, file=sys.stderr))
    cfg = load_config()
    if cfg is None:
        log("[alerts] no pushover_config.json; skipping")
        return 0

    sent = _sent_tickers()
    qualifying = []
    for r in rows:
        lead = r.get("lead_hours")
        et = r.get("event_ticker")
        if lead is None or not et or r.get("settled"):
            continue
        if et in sent:
            continue
        if T24_WINDOW[0] <= lead <= T24_WINDOW[1]:
            qualifying.append(r)

    if not qualifying:
        return 0

    title = f"weatherbot T-24h ({len(qualifying)})"
    body = build_message(qualifying)
    try:
        ok, resp = send_pushover(title, body, cfg)
    except Exception as e:
        log(f"[alerts] pushover request failed: {e}")
        return 0

    if not ok:
        log(f"[alerts] pushover non-200: {resp}")
        return 0

    for r in qualifying:
        try:
            _record_sent(r["event_ticker"])
        except Exception as e:
            log(f"[alerts] failed to record sent ticker {r['event_ticker']}: {e}")
    log(f"[alerts] sent T-24h batch for {len(qualifying)} event(s)")
    return len(qualifying)


if __name__ == "__main__":
    # Smoke utility: format the most recent N rows from the snapshot log to
    # stdout (no Pushover send). Use this to eyeball the card format.
    import sys as _sys
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    n = int(_sys.argv[1]) if len(_sys.argv) > 1 else 7
    rows = []
    try:
        with open("live_picks_log.jsonl", "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    rows.append(json.loads(ln))
    except FileNotFoundError:
        print("no live_picks_log.jsonl yet", file=_sys.stderr)
        _sys.exit(1)
    print(build_message(rows[-n:]))
