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


def calibrate_yes(p: float) -> tuple[float, float]:
    """Return (calibrated_prob, adjustment_pp) per MODEL_NOTES YES table.
    Midpoint of stated ranges: ≥80 +0, 60-80 +5, 40-60 +9, <40 +12.
    """
    if p >= 0.80:
        return p, 0.0
    if p >= 0.60:
        return min(p + 0.05, 1.0), 0.05
    if p >= 0.40:
        return min(p + 0.09, 1.0), 0.09
    return min(p + 0.12, 1.0), 0.12


def calibrate_no(p_no: float) -> tuple[float, float, str | None]:
    """Return (calibrated_p_no, adjustment_pp, skip_reason_or_None) per
    MODEL_NOTES NO table: ≥90 -3, 75-90 -4 (midpoint of -3 to -5), 60-75
    SKIP danger-zone, <60 SKIP unreliable.
    """
    if p_no >= 0.90:
        return p_no - 0.03, -0.03, None
    if p_no >= 0.75:
        return p_no - 0.04, -0.04, None
    if p_no >= 0.60:
        return p_no, 0.0, "danger-zone"
    return p_no, 0.0, "unreliable"


def best_yes(buckets: list[dict]) -> dict | None:
    cs = [b for b in buckets if b.get("prob") is not None]
    return max(cs, key=lambda b: b["prob"]) if cs else None


MIN_PRINTED_NO = 0.80   # printed-NO floor; mirrors kalshi_temp.best_no_pick
                        # (2026-05-26-no-selection-fix.md)


def best_ev_no(buckets: list[dict],
               sanity_yes_ask_min: float = 0.85,
               sanity_prob_max: float = 0.40) -> dict | None:
    cs = []
    for b in buckets:
        prob, ya, ev = b.get("prob"), b.get("yes_ask"), b.get("ev_no")
        if prob is None or ya is None or ev is None:
            continue
        if ya >= sanity_yes_ask_min and prob <= sanity_prob_max:
            continue
        if (1 - prob) < MIN_PRINTED_NO:   # printed-NO floor — drop low-conviction NO
            continue
        cs.append(b)
    return max(cs, key=lambda b: b["ev_no"]) if cs else None


def _fmt_pct(x: float | None) -> str:
    return "--" if x is None else f"{x * 100:.0f}"


def _fmt_signed_cents(x: float | None) -> str:
    return "--" if x is None else f"{x * 100:+.0f}"


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

    yes = best_yes(buckets)
    if yes is not None:
        p = yes["prob"]
        cal_p, _ = calibrate_yes(p)
        ya = yes.get("yes_ask")
        cal_ev = cal_p - ya if ya is not None else None
        action = "SKIP-thin"
        if cal_ev is not None and cal_ev >= TAKE_EV_THRESHOLD:
            action = "TAKE"
        lines.append(f"YES {yes['subtitle']}  {_fmt_pct(p)}→{_fmt_pct(cal_p)}  "
                     f"ask{_fmt_pct(ya)}  EV{_fmt_signed_cents(cal_ev)}¢  {action}")
    else:
        lines.append("YES (no pick)")

    no = best_ev_no(buckets)
    if no is not None:
        p_yes = no["prob"]
        p_no = 1.0 - p_yes
        cal_p_no, _, skip_reason = calibrate_no(p_no)
        na = no.get("no_ask")
        cal_ev = cal_p_no - na if na is not None else None
        if skip_reason:
            action = f"SKIP-{skip_reason}"
        elif cal_ev is not None and cal_ev >= TAKE_EV_THRESHOLD:
            action = "TAKE"
        else:
            action = "SKIP-thin"
        lines.append(f"NO  {no['subtitle']}  {_fmt_pct(p_no)}→{_fmt_pct(cal_p_no)}  "
                     f"ask{_fmt_pct(na)}  EV{_fmt_signed_cents(cal_ev)}¢  {action}")
    else:
        lines.append("NO  (sanity-capped)")

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
