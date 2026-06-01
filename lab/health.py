"""Model health loop: deterministic daily scan over the lab diagnostics.

Detection/reporting ONLY. Reads data and writes only docs/MODEL_HEALTH.md,
docs/MODEL_CHANGES.md, and the docs/.health_marker fingerprint. Never edits
model code/config/calibration and never trades. See spec
docs/superpowers/specs/2026-05-27-model-health-loop-design.md.
"""
from dataclasses import dataclass, field
import math
import statistics
from collections import defaultdict
from pathlib import Path
from datetime import datetime, timezone

import lab.live_calibration as lc

# Thresholds (single source of truth in wb_thresholds; shared with the live pick
# path and the dashboard backtest model). Re-exported here so `lab.health.MIN_N`
# etc. stay importable. MIN_N encodes the n~15 lesson: below it a metric reports
# INSUFFICIENT_DATA rather than flagging.
from wb_thresholds import (   # noqa: E402  (kept beside the constants they document)
    MIN_N,
    CAL_WATCH_PP,
    CAL_ALERT_PP,
    K_OK,
    K_WATCH,
    BIAS_WATCH,
    BIAS_ALERT,
    OPP_EDGE_MIN,
)


@dataclass
class MetricReading:
    name: str
    value: float | None
    threshold: str
    status: str          # OK | WATCH | ALERT | INSUFFICIENT_DATA
    n: int
    note: str = ""


@dataclass
class Opportunity:
    kind: str
    scope: str
    value: float
    n: int
    note: str = ""


@dataclass
class HealthReport:
    generated_at: str
    days: int
    readings: list[MetricReading] = field(default_factory=list)
    opportunities: list[Opportunity] = field(default_factory=list)
    deployed_model_changed: bool = False


def classify_abs(value_pp: float, *, n: int,
                 watch: float = CAL_WATCH_PP, alert: float = CAL_ALERT_PP) -> str:
    """Status for a signed pp value judged on its magnitude."""
    if n < MIN_N:
        return "INSUFFICIENT_DATA"
    a = abs(value_pp)
    if a <= watch:
        return "OK"
    if a <= alert:
        return "WATCH"
    return "ALERT"


def classify_k(k: float, *, n: int) -> str:
    """Status for a dispersion k (1.0 = calibrated)."""
    if n < MIN_N:
        return "INSUFFICIENT_DATA"
    if K_OK[0] <= k <= K_OK[1]:
        return "OK"
    if K_WATCH[0] <= k <= K_WATCH[1]:
        return "WATCH"
    return "ALERT"


def dispersion_k(pairs: list[tuple[float, float]]) -> float | None:
    """Quantization-adjusted dispersion multiplier from (err, sigma) pairs.

    z = err/sigma; k = sqrt(max(pvar(z) - Q, 0)) with quantization term
    Q = (1/3)*mean(1/sigma^2) (interior 2°F bucket midpoint noise). Needs >= 2
    pairs. k~1 calibrated, k<1 over-dispersed, k>1 under-dispersed.
    """
    if len(pairs) < 2:
        return None
    z = [e / s for e, s in pairs]
    var_z = statistics.pvariance(z)
    q = (1.0 / 3.0) * statistics.mean(1.0 / (s * s) for _, s in pairs)
    return math.sqrt(max(var_z - q, 0.0))


def dispersion_by_city(days: int, cache=None) -> dict[str, tuple[float | None, int]]:
    """Per-series interior-bucket dispersion k over settled events.

    Returns {series: (k, n_interior_events)}. Reuses build_historical_inputs +
    compute(LIVE_TODAY); restricts to strike_type=="between" winners for the
    clean quantization cut. No network beyond the cached diagnostics.
    """
    import kalshi_temp as kt
    from lab.configs import LIVE_TODAY
    from lab.inputs import build_historical_inputs, winner_of
    from lab.refit_bias import actual_high_midpoint
    from model import compute

    out: dict[str, tuple[float | None, int]] = {}
    for series in kt.CITIES:
        pairs: list[tuple[float, float]] = []
        for e in kt.list_events_for_series(series, days):
            inp = build_historical_inputs(e["event_ticker"], cache=cache)
            if inp is None:
                continue
            w = winner_of(list(inp.markets))
            if w is None or w.get("strike_type") != "between":
                continue
            actual = actual_high_midpoint(w)
            if actual is None:
                continue
            res = compute(inp, LIVE_TODAY)
            if res.mu is None or res.sigma is None:
                continue
            pairs.append((actual - res.mu, res.sigma))
        out[series] = (dispersion_k(pairs), len(pairs))
    return out


def _calibrate_side(records, side):
    """Indirection over live_calibration.calibrate_live (monkeypatchable)."""
    from lab.live_calibration import calibrate_live
    return calibrate_live(records, side)


def _refit_bias(days):
    """Indirection over refit_bias.refit on LIVE_TODAY (monkeypatchable)."""
    import kalshi_temp as kt
    from lab.configs import LIVE_TODAY
    from lab.refit_bias import refit
    events: list[str] = []
    for s in kt.CITIES:
        events.extend(e["event_ticker"] for e in kt.list_events_for_series(s, days))
    return refit(events, LIVE_TODAY)


def calibration_readings(records, deployed_no_haircut: float) -> list[MetricReading]:
    """YES gap (realized−pred) and NO gap scored NET of the deployed haircut."""
    out: list[MetricReading] = []
    y = _calibrate_side(records, "yes")
    y_gap = (y.realized_rate - y.mean_pred) * 100.0
    out.append(MetricReading(
        name="calibration_yes", value=round(y_gap, 1), threshold="|gap| <= 5pp",
        status=classify_abs(y_gap, n=y.n_bets), n=y.n_bets,
        note=f"pred {y.mean_pred*100:.1f}% realized {y.realized_rate*100:.1f}%"))
    n = _calibrate_side(records, "no")
    no_resid = ((n.mean_pred - n.realized_rate) - deployed_no_haircut) * 100.0
    out.append(MetricReading(
        name="calibration_no_net_haircut", value=round(no_resid, 1),
        threshold="|residual after haircut| <= 5pp",
        status=classify_abs(no_resid, n=n.n_bets), n=n.n_bets,
        note=(f"NO known structural offset; pred {n.mean_pred*100:.1f}% "
              f"realized {n.realized_rate*100:.1f}% net of {deployed_no_haircut:.2f} haircut")))
    return out


def bias_drift_readings(days: int) -> list[MetricReading]:
    """Per-series |freshly-fit bias − deployed BIAS|."""
    import kalshi_temp as kt
    fresh = _refit_bias(days)
    out: list[MetricReading] = []
    for series, deployed in kt.BIAS.items():
        row = fresh.get(series)
        if row is None:
            out.append(MetricReading(name=f"bias_drift_{series}", value=None,
                                     threshold="|Δ| <= 0.5°F", status="INSUFFICIENT_DATA",
                                     n=0, note="no fresh fit"))
            continue
        delta = row["bias"] - deployed
        n = row["n"]
        if n < MIN_N:
            status = "INSUFFICIENT_DATA"
        elif abs(delta) <= BIAS_WATCH:
            status = "OK"
        elif abs(delta) <= BIAS_ALERT:
            status = "WATCH"
        else:
            status = "ALERT"
        out.append(MetricReading(
            name=f"bias_drift_{series}", value=round(delta, 2),
            threshold="|Δ| <= 0.5°F", status=status, n=n,
            note=f"deployed {deployed:+.2f} fresh {row['bias']:+.2f}"))
    return out


def scan_opportunities(records: list[dict]) -> list[Opportunity]:
    """Flag per-series unexploited YES edge.

    `records`: dicts with series, implied (market prob on the pick), won (0/1),
    taken (bool — did the bot actually take it). An opportunity = over >= MIN_N
    UNTAKEN picks in a series, realized win-rate exceeds mean implied prob by
    >= OPP_EDGE_MIN. Deterministic; no network.
    """
    by_series: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        if not r.get("taken", False):
            by_series[r["series"]].append(r)
    out: list[Opportunity] = []
    for series, rs in by_series.items():
        n = len(rs)
        if n < MIN_N:
            continue
        realized = sum(r["won"] for r in rs) / n
        implied = sum(r["implied"] for r in rs) / n
        edge = realized - implied
        if edge >= OPP_EDGE_MIN:
            out.append(Opportunity(
                kind="unexploited_yes_edge", scope=series,
                value=round(edge, 4), n=n,
                note=(f"untaken YES picks won {realized*100:.1f}% vs implied "
                      f"{implied*100:.1f}% (+{edge*100:.1f}pp)")))
    return out


def render_report(report: HealthReport) -> str:
    lines: list[str] = []
    lines.append("# Model Health")
    lines.append("")
    lines.append(f"_Generated {report.generated_at} · window {report.days}d · "
                 "auto-regenerated daily; do not hand-edit (see MODEL_CHANGES.md "
                 "for the change journal)._")
    lines.append("")
    if report.deployed_model_changed:
        lines.append("> ⚠️ **Deployed model changed since last run** — add a "
                     "`docs/MODEL_CHANGES.md` entry (what/why/validation/deployed-or-held).")
        lines.append("")
    flagged = [r for r in report.readings if r.status in ("ALERT", "WATCH")]
    lines.append("## Flags")
    if flagged:
        for r in sorted(flagged, key=lambda x: 0 if x.status == "ALERT" else 1):
            lines.append(f"- **{r.status}** `{r.name}` = {r.value} "
                         f"({r.threshold}; n={r.n}) — {r.note}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Opportunities")
    if report.opportunities:
        for o in report.opportunities:
            lines.append(f"- `{o.kind}` **{o.scope}** {o.value:+} (n={o.n}) — {o.note}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## All readings")
    lines.append("")
    lines.append("| metric | value | status | n | threshold | note |")
    lines.append("|---|---|---|---|---|---|")
    for r in report.readings:
        lines.append(f"| {r.name} | {r.value} | {r.status} | {r.n} | "
                     f"{r.threshold} | {r.note} |")
    lines.append("")
    return "\n".join(lines)


def model_fingerprint() -> str:
    """Fingerprint of the deployed model: git HEAD of model/ + hash of
    calibration_params.json (if present)."""
    import hashlib
    import subprocess
    try:
        head = subprocess.check_output(
            ["git", "log", "-1", "--format=%H", "--", "model", "kalshi_temp.py"],
            text=True).strip()
    except Exception:
        head = "nogit"
    cp = Path("calibration_params.json")
    cp_hash = hashlib.sha256(cp.read_bytes()).hexdigest()[:12] if cp.exists() else "none"
    return f"{head[:12]}:{cp_hash}"


def detect_deployed_change(marker_path: str, *, fingerprint: str | None = None,
                           record: bool = True) -> bool:
    """True if the deployed-model fingerprint changed since the last run.

    When `record` is True (default), persists the new fingerprint to
    `marker_path`; when False, reports the change WITHOUT consuming the marker
    (so a read-only dry run doesn't suppress the next --write run's journal
    stub). First run (no marker) counts as changed. `fingerprint` injectable
    for testing."""
    fp = fingerprint if fingerprint is not None else model_fingerprint()
    p = Path(marker_path)
    prev = p.read_text(encoding="utf-8").strip() if p.exists() else None
    changed = prev != fp
    if changed and record:
        p.write_text(fp, encoding="utf-8")
    return changed


HEALTH_PATH = "docs/MODEL_HEALTH.md"
CHANGES_PATH = "docs/MODEL_CHANGES.md"
MARKER_PATH = "docs/.health_marker"


def _opportunity_records(rows, cache=None) -> list[dict]:
    """Build scan_opportunities input from log rows: each settled event's
    nearest-T24 pred row -> the YES pick's implied prob, won, and whether it was
    taken (cal_ev_yes/ev_yes >= MIN_BEST_EV). Reuses live_calibration helpers."""
    import kalshi_temp as kt
    out: list[dict] = []
    by_ev = lc._by_event(rows)
    for ev, rs in by_ev.items():
        wt = lc._winner_ticker(rs, ev, cache)
        if wt is None:
            continue
        preds = [r for r in rs if lc._has_pred(r) and r.get("lead_hours") is not None]
        if not preds:
            continue
        row = min(preds, key=lambda r: abs(r["lead_hours"] - 24.0))
        pick = lc._yes_pick(row.get("buckets", []))
        if pick is None:
            continue
        implied = pick.get("yes_ask")
        if implied is None:
            continue
        cal = pick.get("cal_ev_yes")
        ev_val = cal if cal is not None else (pick.get("ev_yes") or 0.0)
        taken = ev_val >= kt.MIN_BEST_EV
        out.append({"series": ev.split("-")[0], "implied": implied,
                    "won": 1 if pick["ticker"] == wt else 0, "taken": bool(taken)})
    return out


def run_health_scan(days: int = 14, log_path: str = lc.LOG_PATH, cache=None,
                    record_change: bool = True) -> HealthReport:
    import kalshi_temp as kt
    rows = lc.read_log(log_path, since_days=days)
    records, _skips, _leads = lc.build_records(rows, cache=cache)
    readings: list[MetricReading] = []
    readings += calibration_readings(records, deployed_no_haircut=kt.haircut_for("no", 0.85))
    readings += bias_drift_readings(days=max(days, 60))
    for series, (k, n) in dispersion_by_city(days=max(days, 60), cache=cache).items():
        readings.append(MetricReading(
            name=f"dispersion_k_{series}",
            value=(round(k, 2) if k is not None else None),
            threshold="k in [0.8, 1.25]",
            status=(classify_k(k, n=n) if k is not None else "INSUFFICIENT_DATA"),
            n=n, note="interior-bucket dispersion, archive/backtest formula (1.0 = calibrated)"))
    opportunities = scan_opportunities(_opportunity_records(rows, cache=cache))
    changed = detect_deployed_change(MARKER_PATH, record=record_change)
    return HealthReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        days=days, readings=readings, opportunities=opportunities,
        deployed_model_changed=changed)


def write_report(report: HealthReport, *, health_path: str = HEALTH_PATH,
                 changes_path: str = CHANGES_PATH) -> None:
    """Write the health report; append a change-journal stub iff the deployed
    model changed. write_report writes only the two report docs; the scan also
    persists docs/.health_marker via detect_deployed_change — those three are
    the module's only writes."""
    Path(health_path).write_text(render_report(report), encoding="utf-8")
    if report.deployed_model_changed:
        stub = (f"\n## {report.generated_at[:10]} — deployed model changed (stub)\n"
                "- change: _fill in_\n- why: _fill in_\n- validation: _fill in_\n"
                "- deployed-or-held: _fill in_\n- commit: _fill in_\n")
        with open(changes_path, "a", encoding="utf-8") as f:
            f.write(stub)
