"""Pure command handlers for the weatherbot Telegram bot.

Each function takes plain args and returns a human-readable string. They do NO
Telegram I/O, so they're unit-testable without a bot token (the stub-token gate).
All errors are returned as readable text, never raised — the transport must never
surface a stack trace to the chat.

Only cmd_predict touches the network (live model); cmd_status / cmd_health /
cmd_report read on-disk artifacts (hourly_signals.json, docs/MODEL_HEALTH.md).
"""
import json
import os
from collections import deque
from pathlib import Path

HEALTH_PATH = "docs/MODEL_HEALTH.md"
SIGNALS_PATH = "hourly_signals.json"
LIVE_LOG_PATH = "live_picks_log.jsonl"

# City token -> KXHIGH series ticker. Accepts common aliases; case-insensitive.
CITY_ALIASES = {
    "ny": "KXHIGHNY", "nyc": "KXHIGHNY", "newyork": "KXHIGHNY",
    "chi": "KXHIGHCHI", "chicago": "KXHIGHCHI",
    "mia": "KXHIGHMIA", "miami": "KXHIGHMIA",
    "lax": "KXHIGHLAX", "la": "KXHIGHLAX", "losangeles": "KXHIGHLAX",
    "den": "KXHIGHDEN", "denver": "KXHIGHDEN",
    "aus": "KXHIGHAUS", "austin": "KXHIGHAUS",
    "phil": "KXHIGHPHIL", "phl": "KXHIGHPHIL", "philly": "KXHIGHPHIL",
    "philadelphia": "KXHIGHPHIL",
}


def resolve_city(token: str):
    """Map a user token to a KXHIGH series ticker, or None if unrecognized."""
    if not token:
        return None
    t = token.strip().lower().replace("_", "").replace(" ", "")
    if t in CITY_ALIASES:
        return CITY_ALIASES[t]
    up = token.strip().upper()
    try:
        import kalshi_temp as kt
        if up in kt.CITIES:
            return up
    except Exception:
        pass
    return None


def _short(series: str) -> str:
    return (series or "").replace("KXHIGH", "")


def _pct(x):
    return "--" if x is None else f"{x * 100:.0f}%"


def _cents(x):
    return "--" if x is None else f"{x * 100:+.0f}c"


def _mtime_iso(path: str):
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)\
            .isoformat(timespec="seconds")
    except Exception:
        return None


def cmd_help() -> str:
    return ("weatherbot bot — commands:\n"
            "/predict <city> — live model + best EV picks (ny, chi, mia, lax, den, aus, phil)\n"
            "/report — latest hourly qualifying signals\n"
            "/status — system freshness (signals, last snapshot, notifier)\n"
            "/health — model health flags (calibration / dispersion / bias)")


def cmd_status() -> str:
    """System status from on-disk artifacts only (no network)."""
    lines = ["weatherbot status"]
    try:
        import signals
        rep = signals.read_report_payload(SIGNALS_PATH)
        freshness = "STALE" if rep.get("stale") else "fresh"
        lines.append(f"signals: {freshness} — {len(rep.get('picks', []))} picks, "
                     f"{rep.get('n_open_events', 0)} open events "
                     f"(generated {rep.get('generated_at') or 'never'})")
    except Exception as e:
        lines.append(f"signals: error reading report ({e})")
    snap = _mtime_iso(LIVE_LOG_PATH)
    lines.append(f"last snapshot write: {snap or '(no log)'}")
    try:
        if Path(HEALTH_PATH).exists():
            gen = next((ln for ln in Path(HEALTH_PATH).read_text(encoding="utf-8").splitlines()
                        if ln.startswith("_Generated")), "")
            lines.append("health doc: " + (gen.strip("_ ") or "see /health"))
        else:
            lines.append("health doc: (missing)")
    except Exception as e:
        lines.append(f"health doc: error ({e})")
    return "\n".join(lines)


def cmd_health() -> str:
    """Return the generated-timestamp + Flags section of docs/MODEL_HEALTH.md."""
    p = Path(HEALTH_PATH)
    if not p.exists():
        return "no MODEL_HEALTH.md yet (daily health task hasn't written one)."
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"couldn't read MODEL_HEALTH.md: {e}"
    lines = text.splitlines()
    gen = next((ln for ln in lines if ln.startswith("_Generated")), "")
    out = ["weatherbot model health"]
    if gen:
        out.append(gen.strip("_ "))
    # Capture from "## Flags" up to the next "## " heading.
    grab = False
    for ln in lines:
        if ln.startswith("## Flags"):
            grab = True
            continue
        if grab and ln.startswith("## "):
            break
        if grab and ln.strip():
            out.append(ln)
    if len(out) <= 2:
        out.append("(no flags — all metrics OK or insufficient data)")
    return "\n".join(out)


def cmd_report() -> str:
    """Format the latest hourly qualifying-signal report (hourly_signals.json)."""
    try:
        import signals
        rep = signals.read_report_payload(SIGNALS_PATH)
    except Exception as e:
        return f"couldn't read signals report: {e}"
    picks = rep.get("picks", []) or []
    head = (f"weatherbot signals {'(STALE)' if rep.get('stale') else ''} — "
            f"generated {rep.get('generated_at') or 'never'}\n"
            f"{rep.get('n_open_events', 0)} open events, {len(picks)} qualifying pick(s)")
    if not picks:
        return head + "\n(no qualifying picks right now)"
    out = [head]
    for p in picks:
        out.append(
            f"{_short(p.get('series'))} {p.get('target_date', '?')} "
            f"{p.get('side', '?')} {p.get('bucket', '?')}  "
            f"p{_pct(p.get('printed_prob'))} @{_cents(p.get('market_price')).lstrip('+')} "
            f"EV{_cents(p.get('ev'))} sz{p.get('size_pct', '?')}% "
            f"T-{p.get('lead_hours', '?')}h")
    return "\n".join(out)


def _fmt_pick(label: str, pick) -> str:
    if not pick:
        return f"{label}: (none)"
    sub = pick.get("subtitle", "?")
    if label.startswith("best YES"):
        ask, ev = pick.get("yes_ask"), pick.get("cal_ev_yes")
    else:
        ask, ev = pick.get("no_ask"), pick.get("cal_ev_no")
    return f"{label}: {sub}  ask{_cents(ask).lstrip('+')}  EV{_cents(ev)}"


def cmd_predict(city_token: str) -> str:
    """Live model + best EV picks for one city. Hits the network."""
    series = resolve_city(city_token)
    if series is None:
        return (f"unknown city '{city_token}'. Try: ny, chi, mia, lax, den, aus, phil."
                if city_token else
                "usage: /predict <city>  (ny, chi, mia, lax, den, aus, phil)")
    try:
        import kalshi_temp as kt
        data = kt.get_dashboard_data()
    except Exception as e:
        return f"couldn't fetch live data: {e}"
    events = [e for e in (data.get("events") or [])
              if (e.get("event_ticker") or "").startswith(series + "-")]
    if not events:
        return f"no open event for {_short(series)} right now."
    out = []
    for ev in events:
        model = ev.get("model") or {}
        mu, sigma = model.get("mu"), model.get("sigma")
        head = f"{ev.get('station') or _short(series)} — {ev.get('event_ticker')} ({ev.get('target_date', '?')})"
        out.append(head)
        if ev.get("settled"):
            out.append(f"SETTLED: {ev.get('settled_bucket', '?')}")
            continue
        if mu is not None and sigma is not None:
            src = model.get("sources")
            out.append(f"model: mu{mu:.1f} +/-{sigma:.1f}" + (f" ({src} src)" if src else ""))
        else:
            out.append("model: (no forecast)")
        try:
            summ = kt.predict_summary(ev.get("markets") or [])
            mlp = summ.get("highest_probability")
            if mlp:
                out.append(f"most likely: {mlp.get('subtitle', '?')}  p{_pct(mlp.get('prob'))}")
            out.append(_fmt_pick("best YES", summ.get("best_ev_yes")))
            out.append(_fmt_pick("best NO", summ.get("best_ev_no")))
        except Exception as e:
            out.append(f"(pick error: {e})")
    return "\n".join(out)
