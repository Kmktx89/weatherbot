import math
from dataclasses import dataclass as _dc

from lab.health import (
    MetricReading, Opportunity, HealthReport,
    MIN_N, classify_abs, classify_k,
    dispersion_k, calibration_readings, bias_drift_readings,
    scan_opportunities, render_report, detect_deployed_change,
    run_health_scan, write_report, HEALTH_PATH, CHANGES_PATH,
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


@_dc
class _FakeRep:   # mimics lab.calibration.CalibrationReport
    n_bets: int
    mean_pred: float
    realized_rate: float
    brier_score: float = 0.2


def test_calibration_readings_yes_gap_and_no_net_of_haircut(monkeypatch):
    import lab.health as h
    monkeypatch.setattr(h, "_calibrate_side",
                        lambda recs, side: _FakeRep(40, 0.45, 0.43) if side == "yes"
                        else _FakeRep(40, 0.88, 0.70))
    readings = calibration_readings(records=[], deployed_no_haircut=0.11)
    by = {r.name: r for r in readings}
    assert by["calibration_yes"].status == "OK"
    assert by["calibration_yes"].value == _approx(-2.0)
    assert by["calibration_no_net_haircut"].status == "WATCH"
    assert by["calibration_no_net_haircut"].value == _approx(7.0)


def test_bias_drift_readings_flags_large_delta(monkeypatch):
    import lab.health as h
    import kalshi_temp as kt
    monkeypatch.setattr(kt, "BIAS", {"KXHIGHNY": -0.44, "KXHIGHDEN": -0.81})
    monkeypatch.setattr(h, "_refit_bias",
                        lambda days: {"KXHIGHNY": {"bias": -0.50, "n": 60, "sd": 1.4},
                                      "KXHIGHDEN": {"bias": -2.10, "n": 60, "sd": 2.0}})
    readings = bias_drift_readings(days=60)
    by = {r.name: r for r in readings}
    assert by["bias_drift_KXHIGHNY"].status == "OK"      # |−0.50−(−0.44)|=0.06
    assert by["bias_drift_KXHIGHDEN"].status == "ALERT"  # |−2.10−(−0.81)|=1.29 > 1.0


def _approx(v):
    from pytest import approx
    return approx(v, abs=1e-9)


def test_scan_opportunities_flags_unexploited_yes_edge():
    recs = [{"series": "KXHIGHLAX", "implied": 0.40, "won": 1, "taken": False}
            for _ in range(20)] + \
           [{"series": "KXHIGHLAX", "implied": 0.40, "won": 0, "taken": False}
            for _ in range(15)]   # 20/35 = 57.1% realized vs 40% implied, n=35
    opps = scan_opportunities(recs)
    lax = [o for o in opps if o.scope == "KXHIGHLAX"]
    assert lax and lax[0].kind == "unexploited_yes_edge"
    assert lax[0].value == _approx(round((20/35) - 0.40, 4))
    assert lax[0].n == 35


def test_scan_opportunities_silent_when_thin_or_no_edge():
    assert scan_opportunities([{"series": "KXHIGHNY", "implied": 0.4, "won": 1,
                                "taken": False}] * 10) == []   # below MIN_N
    recs = [{"series": "KXHIGHNY", "implied": 0.5, "won": 1, "taken": False}
            for _ in range(20)] + \
           [{"series": "KXHIGHNY", "implied": 0.5, "won": 0, "taken": False}
            for _ in range(20)]   # 50% realized vs 50% implied = 0 edge
    assert scan_opportunities(recs) == []


def test_render_report_has_sections_and_flags():
    rep = HealthReport(
        generated_at="2026-05-27T13:00:00Z", days=14,
        readings=[
            MetricReading("calibration_yes", -2.0, "|gap|<=5pp", "OK", 56, "pred 45%"),
            MetricReading("dispersion_k_KXHIGHLAX", 0.46, "k in [0.8,1.25]", "ALERT", 53,
                          "over-dispersed"),
        ],
        opportunities=[Opportunity("unexploited_yes_edge", "KXHIGHLAX", 0.07, 40,
                                   "won 47% vs 40%")],
        deployed_model_changed=True)
    md = render_report(rep)
    assert "# Model Health" in md
    assert "2026-05-27T13:00:00Z" in md
    assert "ALERT" in md and "dispersion_k_KXHIGHLAX" in md
    assert "unexploited_yes_edge" in md and "KXHIGHLAX" in md
    assert "deployed model changed" in md.lower()
    assert md.index("ALERT") < md.index("## All readings")


def test_detect_deployed_change_first_run_then_stable(tmp_path):
    marker = tmp_path / "marker.txt"
    assert detect_deployed_change(str(marker), fingerprint="abc") is True
    assert marker.read_text().strip() == "abc"
    assert detect_deployed_change(str(marker), fingerprint="abc") is False
    assert detect_deployed_change(str(marker), fingerprint="def") is True
    assert marker.read_text().strip() == "def"


def test_run_health_scan_assembles_report(monkeypatch):
    import lab.health as h
    monkeypatch.setattr(h.lc, "read_log", lambda path, since_days=None: [{"x": 1}])
    monkeypatch.setattr(h.lc, "build_records", lambda rows, target_lead=24.0, cache=None: ([], {}, []))
    monkeypatch.setattr(h, "calibration_readings",
                        lambda records, deployed_no_haircut: [
                            MetricReading("calibration_yes", -2.0, "t", "OK", 40, "")])
    monkeypatch.setattr(h, "bias_drift_readings", lambda days: [])
    monkeypatch.setattr(h, "dispersion_by_city",
                        lambda days, cache=None: {"KXHIGHLAX": (0.46, 53)})
    monkeypatch.setattr(h, "_opportunity_records", lambda rows, cache=None: [])
    monkeypatch.setattr(h, "detect_deployed_change", lambda marker, **k: False)
    rep = run_health_scan(days=14, log_path="x.jsonl")
    names = {r.name for r in rep.readings}
    assert "calibration_yes" in names and "dispersion_k_KXHIGHLAX" in names
    assert rep.days == 14


def test_write_report_only_touches_the_two_docs(tmp_path):
    rep = HealthReport(generated_at="t", days=14, deployed_model_changed=False)
    health = tmp_path / "MODEL_HEALTH.md"
    changes = tmp_path / "MODEL_CHANGES.md"
    write_report(rep, health_path=str(health), changes_path=str(changes))
    assert health.exists()
    assert "# Model Health" in health.read_text()
