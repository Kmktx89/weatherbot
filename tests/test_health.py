from lab.health import (
    MetricReading, Opportunity, HealthReport,
    MIN_N, classify_abs, classify_k,
)


def test_classify_abs_insufficient_data_below_min_n():
    assert classify_abs(99.0, n=MIN_N - 1) == "INSUFFICIENT_DATA"


def test_classify_abs_bands():
    assert classify_abs(3.0, n=50) == "OK"        # |3| <= 5
    assert classify_abs(7.0, n=50) == "WATCH"     # 5 < |7| <= 10
    assert classify_abs(-12.0, n=50) == "ALERT"   # |−12| > 10
    assert classify_abs(5.0, n=50) == "OK"        # boundary inclusive
    assert classify_abs(10.0, n=50) == "WATCH"    # boundary inclusive


def test_classify_k_bands():
    assert classify_k(1.0, n=50) == "OK"          # within [0.8, 1.25]
    assert classify_k(0.7, n=50) == "WATCH"       # [0.65, 0.8)
    assert classify_k(1.3, n=50) == "WATCH"       # (1.25, 1.4]
    assert classify_k(0.46, n=50) == "ALERT"      # < 0.65 (current LAX)
    assert classify_k(1.5, n=50) == "ALERT"       # > 1.4
    assert classify_k(1.0, n=MIN_N - 1) == "INSUFFICIENT_DATA"


def test_dataclasses_construct():
    r = MetricReading(name="x", value=1.0, threshold="|gap|<=5pp", status="OK", n=40, note="")
    o = Opportunity(kind="yes_edge", scope="KXHIGHLAX", value=0.07, n=40, note="")
    h = HealthReport(generated_at="t", days=14, readings=[r], opportunities=[o],
                     deployed_model_changed=False)
    assert h.readings[0].status == "OK" and h.opportunities[0].scope == "KXHIGHLAX"


import math
from lab.health import dispersion_k


def test_dispersion_k_calibrated_pairs_near_one():
    pairs = [(-10.0, 10.0), (10.0, 10.0)]  # z = [-1, 1], pvar(z)=1.0
    k = dispersion_k(pairs)
    # Q = (1/3)*mean(1/sigma^2) = (1/3)*(1/100); k=sqrt(1 - that)
    assert k == math.sqrt(1.0 - (1.0 / 3.0) * (1.0 / 100.0))


def test_dispersion_k_overdispersed_below_one():
    pairs = [(-0.1, 5.0), (0.1, 5.0)]   # z near 0 -> k < 1 (model too wide)
    assert dispersion_k(pairs) < 0.5


def test_dispersion_k_too_few_returns_none():
    assert dispersion_k([(1.0, 1.0)]) is None
    assert dispersion_k([]) is None
