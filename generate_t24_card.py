#!/usr/bin/env python3
"""Generate today's T-24h card archive from live_picks_log.jsonl.

For each KXHIGH series with target_date == today (ET), select the snapshot
row whose lead_hours is nearest 24.0, run QC against it, and (if it passes
or can be auto-resolved within +/-6h) write the chosen row to
t24_cards/<today-ET>.json.

Designed to run once daily at 13:00 UTC via Windows Task Scheduler, well
past every KXHIGH city's T-24h moment. Pushover fires only on hard errors
(skipped events) or global publish-block. Soft warnings render as badges
on the dashboard card.

Output:
  t24_cards/<today-ET>.json     - the day's frozen cards + QC summary
  t24_cards/qc_<today-ET>.log   - full QC trail per event
  t24_cards/blocked_<today-ET>.json (only if every event errored out)

Spec: docs/superpowers/specs/2026-05-21-t24-card-page-design.md
"""
import json
import os
import sys
import statistics
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


CARDS_DIR = Path("t24_cards")
LOG_PATH = Path("live_picks_log.jsonl")
ET = ZoneInfo("America/New_York")

# Cities the dashboard cares about. Mirrors CITIES in kalshi_temp.py and
# is the same set of series the snapshotter writes for.
KXHIGH_SERIES = (
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHLAX",
    "KXHIGHDEN", "KXHIGHAUS", "KXHIGHPHIL",
)
SERIES_CITY = {
    "KXHIGHNY": "NYC", "KXHIGHCHI": "CHI", "KXHIGHMIA": "MIA",
    "KXHIGHLAX": "LAX", "KXHIGHDEN": "DEN", "KXHIGHAUS": "AUS",
    "KXHIGHPHIL": "PHIL",
}

# QC tunables - see spec section "QC checks".
LEAD_BAND_HOURS = 6.0       # only try fallback candidates within +/-6h of T-24
LEAD_WARN_DELTA = 2.0       # warn if chosen row's |lead - 24| > this
MIN_SOURCES_WARN = 2        # warn if < 2 weather sources
PROB_SUM_BAND = (0.95, 1.05)
STALE_AFTER_HOURS = 36.0
FORECAST_SPREAD_WARN = 15.0
METAR_DIVERGE_WARN = 20.0


def today_et(now=None):
    """Today's calendar date in America/New_York as ISO string."""
    now = now or datetime.now(timezone.utc)
    return now.astimezone(ET).date().isoformat()


def read_log(path):
    """Yield JSON rows from live_picks_log.jsonl. Skip malformed lines."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def candidates_for_today(rows, today_iso):
    """Group rows by series for the day's target_date, sorted by |lead - 24|.

    Returns dict[series] -> list[row] (best candidate first).
    Only includes the 7 KXHIGH series; other series are dropped.
    """
    by_series = {s: [] for s in KXHIGH_SERIES}
    for r in rows:
        if r.get("target_date") != today_iso:
            continue
        series = r.get("series")
        if series not in by_series:
            continue
        lead = r.get("lead_hours")
        if lead is None:
            continue
        by_series[series].append(r)
    for s, lst in by_series.items():
        lst.sort(key=lambda r: abs(r["lead_hours"] - 24.0))
    return by_series


# -------- QC checks --------
# Each returns (status, reason) where status in {"ok", "warn", "error"}.
# A "warn" still publishes the row; an "error" forces fallback.

def check_lead_time(row):
    delta = abs(row["lead_hours"] - 24.0)
    if delta > LEAD_WARN_DELTA:
        return "warn", f"lead_off_by_{delta:.1f}h"
    return "ok", None


def check_model_populated(row):
    model = row.get("model") or {}
    if model.get("mu") is None:
        return "error", "no_model_at_t24"
    return "ok", None


def check_bucket_probs_sum(row):
    buckets = row.get("buckets") or []
    probs = [b.get("prob") for b in buckets if b.get("prob") is not None]
    if not probs:
        return "error", "bucket_prob_inconsistency"
    s = sum(probs)
    lo, hi = PROB_SUM_BAND
    if not (lo <= s <= hi):
        return "error", f"bucket_prob_sum_{s:.2f}"
    return "ok", None


def check_source_count(row):
    model = row.get("model") or {}
    n = model.get("sources")
    if n is None or n < MIN_SOURCES_WARN:
        return "warn", f"only_{n or 0}_sources"
    return "ok", None


def check_buckets_complete(row):
    buckets = row.get("buckets") or []
    if not buckets:
        return "error", "incomplete_market_data"
    for b in buckets:
        if b.get("yes_bid") is None or b.get("yes_ask") is None:
            return "error", "incomplete_market_data"
    return "ok", None


def check_snapshot_age(row, now):
    try:
        ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return "error", "stale_snapshot"
    age_h = (now - ts).total_seconds() / 3600.0
    if age_h > STALE_AFTER_HOURS:
        return "error", f"stale_snapshot_{age_h:.0f}h"
    return "ok", None


def check_forecast_spread(row):
    f = row.get("forecasts") or {}
    vals = [v for v in (f.get("ecmwf"), f.get("gfs"), f.get("nws")) if v is not None]
    if len(vals) < 2:
        return "ok", None
    spread = max(vals) - min(vals)
    if spread > FORECAST_SPREAD_WARN:
        return "warn", f"wide_forecast_spread_{spread:.1f}"
    return "ok", None


def check_metar_sanity(row):
    f = row.get("forecasts") or {}
    metar = f.get("metar")
    if metar is None:
        return "ok", None
    others = [v for v in (f.get("ecmwf"), f.get("gfs"), f.get("nws")) if v is not None]
    if not others:
        return "ok", None
    diverge = abs(metar - statistics.mean(others))
    if diverge > METAR_DIVERGE_WARN:
        return "warn", f"metar_diverges_{diverge:.1f}"
    return "ok", None


ALL_CHECKS = (
    check_lead_time,
    check_model_populated,
    check_bucket_probs_sum,
    check_source_count,
    check_buckets_complete,
    check_forecast_spread,
    check_metar_sanity,
)


def run_qc(row, now):
    """Run all checks against a row. Returns dict with warnings, errors."""
    warnings, errors = [], []
    # snapshot_age check is separate because it needs `now`.
    status, reason = check_snapshot_age(row, now)
    if status == "error":
        errors.append(reason)
    for fn in ALL_CHECKS:
        status, reason = fn(row)
        if status == "warn":
            warnings.append(reason)
        elif status == "error":
            errors.append(reason)
    return {"warnings": warnings, "errors": errors}


def pick_best_candidate(candidates, now, log_lines):
    """Try candidates in order of nearness-to-24h. Return (row, qc) for the
    first candidate that has no errors; if all candidates have errors, return
    (None, qc_of_last_tried) so the caller can record the failure reason.

    Only candidates with |lead - 24| <= LEAD_BAND_HOURS are considered.
    """
    in_band = [r for r in candidates if abs(r["lead_hours"] - 24.0) <= LEAD_BAND_HOURS]
    if not in_band:
        log_lines.append(f"  no candidates within +/-{LEAD_BAND_HOURS}h of T-24")
        return None, {"warnings": [], "errors": ["no_snapshot_in_band"], "tried_rows": 0}

    last_qc = None
    for i, row in enumerate(in_band, 1):
        qc = run_qc(row, now)
        log_lines.append(
            f"  candidate #{i}: lead={row['lead_hours']:.2f}h ts={row.get('ts','?')} "
            f"warnings={qc['warnings']} errors={qc['errors']}"
        )
        last_qc = qc
        if not qc["errors"]:
            qc["tried_rows"] = i
            return row, qc
    last_qc["tried_rows"] = len(in_band)
    return None, last_qc


def latest_settled_bucket(rows, target_date, series):
    """Scan rows in reverse for the latest settled row for (target_date, series)."""
    for r in reversed(rows):
        if r.get("target_date") == target_date and r.get("series") == series \
                and r.get("settled") and r.get("settled_bucket"):
            return r["settled_bucket"]
    return None


def build_event_entry(series, row, qc, settled_bucket):
    """Shape the per-event dict written to the daily archive."""
    return {
        "series": series,
        "city": SERIES_CITY[series],
        "event_ticker": row.get("event_ticker"),
        "snapshot_ts": row.get("ts"),
        "lead_hours": row.get("lead_hours"),
        "close_time": row.get("close_time"),
        "model": row.get("model"),
        "forecasts": row.get("forecasts"),
        "buckets": row.get("buckets"),
        "settled": bool(settled_bucket),
        "settled_bucket": settled_bucket,
        "qc": qc,
    }


def missing_entry(series, reason):
    return {
        "series": series,
        "city": SERIES_CITY[series],
        "status": "missing",
        "reason": reason,
    }


def write_atomic(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, path)


def maybe_pushover(qc_summary, events, today_iso, blocked=False):
    """Fire one Pushover only if there are errors or a global block.
    Quietly no-ops if pushover_config.json is absent (matches alerts.py)."""
    if not blocked and qc_summary["errors"] == 0:
        return
    try:
        from alerts import load_config, send_pushover
    except ImportError:
        return
    cfg = load_config()
    if cfg is None:
        return
    if blocked:
        title = f"weatherbot T-24h QC: BLOCKED {today_iso}"
        body = "All events errored. Previous day's archive kept. See qc log."
    else:
        title = f"weatherbot T-24h QC: {qc_summary['errors']} error(s)"
        lines = []
        for ev in events:
            if ev.get("status") == "missing":
                lines.append(f"{ev['city']}: {ev.get('reason')}")
            elif ev.get("qc", {}).get("errors"):
                lines.append(f"{ev['city']}: {', '.join(ev['qc']['errors'])}")
        body = "\n".join(lines) if lines else "(no detail)"
    try:
        send_pushover(title, body, cfg)
    except Exception as e:
        print(f"[t24-card] pushover failed: {e}", file=sys.stderr)


def main():
    now = datetime.now(timezone.utc)
    today_iso = today_et(now)
    log_lines = [f"=== T-24 card QC for {today_iso} (generated {now.isoformat()}) ==="]

    rows = list(read_log(LOG_PATH))
    log_lines.append(f"read {len(rows)} rows from {LOG_PATH}")

    cands_by_series = candidates_for_today(rows, today_iso)
    events = []

    for series in KXHIGH_SERIES:
        log_lines.append(f"[{series}]")
        cands = cands_by_series.get(series, [])
        if not cands:
            log_lines.append("  no candidates for today")
            events.append(missing_entry(series, "no_snapshot_today"))
            continue
        row, qc = pick_best_candidate(cands, now, log_lines)
        if row is None:
            reason = qc["errors"][-1] if qc["errors"] else "qc_failed"
            log_lines.append(f"  -> SKIPPED ({reason})")
            events.append(missing_entry(series, reason))
            continue
        settled_bucket = latest_settled_bucket(rows, today_iso, series)
        log_lines.append(
            f"  -> CHOSEN lead={row['lead_hours']:.2f}h "
            f"warnings={qc['warnings']} settled={bool(settled_bucket)}"
        )
        events.append(build_event_entry(series, row, qc, settled_bucket))

    ok_count   = sum(1 for e in events if "status" not in e and not e["qc"]["warnings"])
    warn_count = sum(1 for e in events if "status" not in e and e["qc"]["warnings"])
    err_count  = sum(1 for e in events if e.get("status") == "missing")
    qc_summary = {"ok": ok_count, "warnings": warn_count, "errors": err_count}
    log_lines.append(f"qc_summary: {qc_summary}")

    CARDS_DIR.mkdir(parents=True, exist_ok=True)
    qc_log_path = CARDS_DIR / f"qc_{today_iso}.log"
    qc_log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    # Global publish-block: every event errored.
    blocked = err_count == len(KXHIGH_SERIES) and len(events) == len(KXHIGH_SERIES)
    if blocked:
        sentinel = CARDS_DIR / f"blocked_{today_iso}.json"
        write_atomic(sentinel, {
            "target_date": today_iso,
            "generated_at": now.isoformat(),
            "qc_summary": qc_summary,
            "reason": "every event errored; previous day's archive kept",
            "events": events,
        })
        print(f"[t24-card] BLOCKED for {today_iso}: every event errored", file=sys.stderr)
        maybe_pushover(qc_summary, events, today_iso, blocked=True)
        return 0

    archive = CARDS_DIR / f"{today_iso}.json"
    write_atomic(archive, {
        "target_date": today_iso,
        "generated_at": now.isoformat(),
        "qc_summary": qc_summary,
        "events": events,
    })
    print(
        f"[t24-card] {today_iso}: {ok_count} ok, {warn_count} warning(s), "
        f"{err_count} error(s) -> {archive}",
        file=sys.stderr,
    )
    maybe_pushover(qc_summary, events, today_iso, blocked=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
