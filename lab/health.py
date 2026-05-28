"""Model health loop: deterministic daily scan over the lab diagnostics.

Detection/reporting ONLY. Reads data and writes exactly two files
(docs/MODEL_HEALTH.md, docs/MODEL_CHANGES.md). Never edits model code/config/
calibration and never trades. See spec
docs/superpowers/specs/2026-05-27-model-health-loop-design.md.
"""
from dataclasses import dataclass, field

# Thresholds (initial, tunable). MIN_N encodes the n~15 lesson: below it,
# a metric reports INSUFFICIENT_DATA rather than flagging.
MIN_N = 30
CAL_WATCH_PP = 5.0
CAL_ALERT_PP = 10.0
K_OK = (0.8, 1.25)
K_WATCH = (0.65, 1.4)
BIAS_WATCH = 0.5
BIAS_ALERT = 1.0
OPP_EDGE_MIN = 0.05


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


import math
import statistics


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
