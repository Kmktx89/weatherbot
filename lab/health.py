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
