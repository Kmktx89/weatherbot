import math
from dataclasses import dataclass as _dc

from lab.health import (
    MetricReading, Opportunity, HealthReport,
    MIN_N, classify_abs, classify_k,
    dispersion_k, calibration_readings, bias_drift_readings, bias_resid_live_readings,
    scan_opportunities, render_report, detect_deployed_change,
    run_health_scan, write_report, HEALTH_PATH, CHANGES_PATH,
    _opportunity_records,
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
    assert by["bias_drift_replay_KXHIGHNY"].status == "OK"      # |−0.50−(−0.44)|=0.06
    assert by["bias_drift_replay_KXHIGHDEN"].status == "ALERT"  # |−2.10−(−0.81)|=1.29 > 1.0
    assert "no-NWS replay" in by["bias_drift_replay_KXHIGHNY"].note


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


def test_opportunity_records_handles_taken_pick(monkeypatch):
    """Regression for the daily-health crash: _opportunity_records must reach the
    EV gate and emit a record when a YES pick has a yes_ask. Pre-fix this raised
    'float has no attribute split' because the loop var `ev` (event ticker) was
    rebound to the EV float before `ev.split('-')`. This is the one path the
    suite never exercised (run_health_scan monkeypatches it away)."""
    import lab.health as h
    # Avoid network winner-resolution; pin the winner to our pick's ticker.
    monkeypatch.setattr(h.lc, "_winner_ticker",
                        lambda rs, ev, cache: "KXHIGHLAX-WIN")
    rows = [{
        "event_ticker": "KXHIGHLAX-26MAY28",
        "lead_hours": 24.0,
        "settled_bucket": "70° to 71°",
        "buckets": [
            {"ticker": "KXHIGHLAX-WIN", "subtitle": "70° to 71°",
             "prob": 0.62, "yes_ask": 0.40, "cal_ev_yes": 0.22},
        ],
    }]
    recs = _opportunity_records(rows)
    assert len(recs) == 1
    assert recs[0]["series"] == "KXHIGHLAX"   # series parsed from the event ticker
    assert recs[0]["implied"] == 0.40
    assert recs[0]["won"] == 1                # pick ticker == winner
    assert recs[0]["taken"] is True           # cal_ev_yes 0.22 >= MIN_BEST_EV


def test_detect_deployed_change_record_false_does_not_write(tmp_path):
    marker = tmp_path / "m.txt"
    # record=False: reports changed but does NOT create/update the marker
    assert detect_deployed_change(str(marker), fingerprint="abc", record=False) is True
    assert not marker.exists()
    # record=True (default) still writes
    assert detect_deployed_change(str(marker), fingerprint="abc") is True
    assert marker.read_text().strip() == "abc"


def test_bias_resid_live_per_city_and_pooled(monkeypatch):
    import lab.health as h
    import kalshi_temp as kt
    monkeypatch.setattr(kt, "BIAS", {"KXHIGHAUS": -1.81, "KXHIGHMIA": -1.52})
    # 35 distinct AUS events: actual 94.5, mu 95.5 -> resid -1.0 each.
    rows = [{
        "event_ticker": f"KXHIGHAUS-26JUN{n:02d}", "lead_hours": 24.0,
        "settled_bucket": "94° to 95°", "model": {"mu": 95.5},
    } for n in range(1, 36)]
    monkeypatch.setattr(h.lc, "read_log", lambda path, since_days=None: rows)
    by = {r.name: r for r in bias_resid_live_readings(days=60, log_path="x")}
    assert by["bias_resid_live_KXHIGHAUS"].value == _approx(-1.0)
    assert by["bias_resid_live_KXHIGHAUS"].n == 35
    assert by["bias_resid_live_KXHIGHAUS"].status == "WATCH"   # 0.5 < 1.0 <= 1.5
    # MIA has no rows -> INSUFFICIENT_DATA, value None
    assert by["bias_resid_live_KXHIGHMIA"].status == "INSUFFICIENT_DATA"
    assert by["bias_resid_live_KXHIGHMIA"].value is None
    # pooled = mean of all interior resids (35 x -1.0), n=35 >= 30 -> WATCH
    assert by["bias_resid_live_pooled"].value == _approx(-1.0)
    assert by["bias_resid_live_pooled"].n == 35
    assert by["bias_resid_live_pooled"].status == "WATCH"
    assert "see per-city" in by["bias_resid_live_pooled"].note


def test_bias_resid_live_excludes_open_ended_and_off_lead(monkeypatch):
    import lab.health as h
    import kalshi_temp as kt
    monkeypatch.setattr(kt, "BIAS", {"KXHIGHLAX": -0.55})
    rows = [
        {"event_ticker": "KXHIGHLAX-26JUN01", "lead_hours": 24.0,
         "settled_bucket": "95° or above", "model": {"mu": 96.0}},   # open-ended -> excluded
        {"event_ticker": "KXHIGHLAX-26JUN02", "lead_hours": 40.0,
         "settled_bucket": "80° to 81°", "model": {"mu": 79.0}},     # |40-24|=16 > 12 -> excluded
    ]
    monkeypatch.setattr(h.lc, "read_log", lambda path, since_days=None: rows)
    by = {r.name: r for r in bias_resid_live_readings(days=60, log_path="x")}
    assert by["bias_resid_live_KXHIGHLAX"].n == 0
    assert by["bias_resid_live_KXHIGHLAX"].status == "INSUFFICIENT_DATA"
    assert by["bias_resid_live_pooled"].n == 0
    assert by["bias_resid_live_pooled"].status == "INSUFFICIENT_DATA"


def test_bias_resid_live_pooled_ok_and_alert(monkeypatch):
    import lab.health as h
    import kalshi_temp as kt
    monkeypatch.setattr(kt, "BIAS", {"KXHIGHAUS": -1.81})
    def rows_for(mu):
        return [{"event_ticker": f"KXHIGHAUS-26JUN{n:02d}", "lead_hours": 24.0,
                 "settled_bucket": "94° to 95°", "model": {"mu": mu}} for n in range(1, 36)]
    # actual 94.5; mu 94.5 -> resid 0.0 -> pooled OK
    monkeypatch.setattr(h.lc, "read_log", lambda path, since_days=None: rows_for(94.5))
    by = {r.name: r for r in bias_resid_live_readings(days=60, log_path="x")}
    assert by["bias_resid_live_pooled"].status == "OK"
    # actual 94.5; mu 92.5 -> resid +2.0 -> |2.0| > 1.5 -> ALERT
    monkeypatch.setattr(h.lc, "read_log", lambda path, since_days=None: rows_for(92.5))
    by = {r.name: r for r in bias_resid_live_readings(days=60, log_path="x")}
    assert by["bias_resid_live_pooled"].status == "ALERT"
    assert by["bias_resid_live_pooled"].value == _approx(2.0)


def test_bias_resid_live_lead_boundary_included_at_36(monkeypatch):
    import lab.health as h
    import kalshi_temp as kt
    monkeypatch.setattr(kt, "BIAS", {"KXHIGHAUS": -1.81})
    rows = [{"event_ticker": "KXHIGHAUS-26JUN01", "lead_hours": 36.0,
             "settled_bucket": "94° to 95°", "model": {"mu": 95.5}}]   # |36-24|=12, not > 12 -> included
    monkeypatch.setattr(h.lc, "read_log", lambda path, since_days=None: rows)
    by = {r.name: r for r in bias_resid_live_readings(days=60, log_path="x")}
    assert by["bias_resid_live_KXHIGHAUS"].n == 1
    assert by["bias_resid_live_KXHIGHAUS"].value == _approx(-1.0)
