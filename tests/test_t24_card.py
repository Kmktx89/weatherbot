"""Tests for generate_t24_card.py — selection, QC, fallback, archive shape."""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import generate_t24_card as t24


# -------- fixtures --------

def make_row(series="KXHIGHNY", target_date="2026-05-21", lead=24.0,
             ts="2026-05-21T04:55:59+00:00", mu=66.88, sigma=3.23,
             sources=3, settled=False, settled_bucket=None,
             forecasts=None, buckets=None):
    """Build a row that mirrors live_picks_log.jsonl shape."""
    if forecasts is None:
        forecasts = {"ecmwf": 70.5, "gfs": 66.2, "nws": 63.0, "metar": 66.0}
    if buckets is None:
        buckets = [
            {"ticker": f"{series}-26MAY21-T67", "subtitle": "67° or below",
             "yes_bid": 0.86, "yes_ask": 0.88, "no_bid": 0.12, "no_ask": 0.14,
             "prob": 0.576, "ev_yes": -0.30, "ev_no": 0.28, "volume_24h": 1000},
            {"ticker": f"{series}-26MAY21-B68.5", "subtitle": "68° to 69°",
             "yes_bid": 0.11, "yes_ask": 0.12, "no_bid": 0.88, "no_ask": 0.89,
             "prob": 0.215, "ev_yes": 0.10, "ev_no": -0.11, "volume_24h": 1000},
            {"ticker": f"{series}-26MAY21-B70.5", "subtitle": "70° to 71°",
             "yes_bid": 0.01, "yes_ask": 0.02, "no_bid": 0.98, "no_ask": 0.99,
             "prob": 0.133, "ev_yes": 0.11, "ev_no": -0.12, "volume_24h": 1000},
            {"ticker": f"{series}-26MAY21-B72.5", "subtitle": "72° to 73°",
             "yes_bid": 0.00, "yes_ask": 0.01, "no_bid": 0.99, "no_ask": 1.0,
             "prob": 0.056, "ev_yes": 0.05, "ev_no": -0.06, "volume_24h": 1000},
            {"ticker": f"{series}-26MAY21-B74.5", "subtitle": "74° to 75°",
             "yes_bid": 0.00, "yes_ask": 0.01, "no_bid": 0.99, "no_ask": 1.0,
             "prob": 0.016, "ev_yes": 0.0, "ev_no": -0.02, "volume_24h": 1000},
            {"ticker": f"{series}-26MAY21-T76", "subtitle": "76° or above",
             "yes_bid": 0.00, "yes_ask": 0.01, "no_bid": 0.99, "no_ask": 1.0,
             "prob": 0.004, "ev_yes": -0.01, "ev_no": -0.01, "volume_24h": 1000},
        ]
    return {
        "ts": ts,
        "event_ticker": f"{series}-26MAY21",
        "series": series,
        "target_date": target_date,
        "close_time": "2026-05-22T04:59:00Z",
        "lead_hours": lead,
        "settled": settled,
        "settled_bucket": settled_bucket,
        "model": {"mu": mu, "sigma": sigma, "sources": sources, "bias": -0.44},
        "forecasts": forecasts,
        "buckets": buckets,
    }


NOW = datetime(2026, 5, 21, 13, 0, 0, tzinfo=timezone.utc)


# -------- today_et --------

def test_today_et_in_new_york():
    # 03:00 UTC on May 21 is 23:00 ET on May 20.
    early = datetime(2026, 5, 21, 3, 0, 0, tzinfo=timezone.utc)
    assert t24.today_et(early) == "2026-05-20"
    # 13:00 UTC on May 21 is 09:00 ET on May 21.
    midmorning = datetime(2026, 5, 21, 13, 0, 0, tzinfo=timezone.utc)
    assert t24.today_et(midmorning) == "2026-05-21"


# -------- candidates_for_today --------

def test_filters_by_target_date_and_kxhigh():
    rows = [
        make_row(series="KXHIGHNY", target_date="2026-05-21", lead=24.0),
        make_row(series="KXHIGHNY", target_date="2026-05-20", lead=24.0),  # wrong date
        make_row(series="KXOTHER",   target_date="2026-05-21", lead=24.0),  # wrong series
        make_row(series="KXHIGHCHI", target_date="2026-05-21", lead=25.0),
    ]
    out = t24.candidates_for_today(rows, "2026-05-21")
    assert len(out["KXHIGHNY"]) == 1
    assert len(out["KXHIGHCHI"]) == 1
    assert all(len(out[s]) == 0 for s in t24.KXHIGH_SERIES if s not in ("KXHIGHNY", "KXHIGHCHI"))


def test_candidates_sorted_by_nearness_to_24():
    rows = [
        make_row(lead=28.0, ts="2026-05-21T00:55:59+00:00"),
        make_row(lead=24.1, ts="2026-05-21T04:55:59+00:00"),
        make_row(lead=22.0, ts="2026-05-21T06:55:59+00:00"),
        make_row(lead=29.5, ts="2026-05-20T23:25:59+00:00"),
    ]
    out = t24.candidates_for_today(rows, "2026-05-21")
    leads = [r["lead_hours"] for r in out["KXHIGHNY"]]
    assert leads == [24.1, 22.0, 28.0, 29.5]  # closest-to-24 first


# -------- individual QC checks --------

def test_check_lead_time_ok():
    assert t24.check_lead_time(make_row(lead=24.05)) == ("ok", None)
    assert t24.check_lead_time(make_row(lead=23.0))[0] == "ok"


def test_check_lead_time_warn_when_off():
    status, reason = t24.check_lead_time(make_row(lead=27.0))
    assert status == "warn"
    assert "lead_off_by_3.0h" in reason


def test_check_model_populated_error_on_null_mu():
    row = make_row(mu=None)
    assert t24.check_model_populated(row) == ("error", "no_model_at_t24")


def test_check_bucket_probs_sum_ok():
    assert t24.check_bucket_probs_sum(make_row())[0] == "ok"


def test_check_bucket_probs_sum_error_when_skewed():
    row = make_row()
    # Halve every prob -> sum ~0.5, out of band.
    for b in row["buckets"]:
        b["prob"] = b["prob"] / 2
    status, reason = t24.check_bucket_probs_sum(row)
    assert status == "error"
    assert reason.startswith("bucket_prob_sum_")


def test_check_source_count_warn_on_low():
    row = make_row(sources=1)
    status, reason = t24.check_source_count(row)
    assert status == "warn"
    assert reason == "only_1_sources"


def test_check_buckets_complete_error_on_empty():
    row = make_row(buckets=[])
    assert t24.check_buckets_complete(row) == ("error", "incomplete_market_data")


def test_check_buckets_complete_error_on_null_price():
    row = make_row()
    row["buckets"][0]["yes_ask"] = None
    assert t24.check_buckets_complete(row)[0] == "error"


def test_check_snapshot_age_ok():
    row = make_row(ts="2026-05-21T04:55:59+00:00")
    assert t24.check_snapshot_age(row, NOW)[0] == "ok"


def test_check_snapshot_age_stale():
    row = make_row(ts="2026-05-18T04:55:59+00:00")  # ~3 days old
    status, reason = t24.check_snapshot_age(row, NOW)
    assert status == "error"
    assert reason.startswith("stale_snapshot_")


def test_check_forecast_spread_warn():
    row = make_row(forecasts={"ecmwf": 80.0, "gfs": 60.0, "nws": 70.0, "metar": 65.0})
    status, reason = t24.check_forecast_spread(row)
    assert status == "warn"
    assert "wide_forecast_spread_20.0" in reason


def test_check_metar_sanity_warn():
    # METAR 82, forecasts mean ~66 -> diverge ~16, below 20 threshold (ok).
    row = make_row(forecasts={"ecmwf": 70.5, "gfs": 66.2, "nws": 63.0, "metar": 82.0})
    assert t24.check_metar_sanity(row)[0] == "ok"
    # METAR 92, forecasts mean ~66 -> diverge ~26, above threshold.
    row = make_row(forecasts={"ecmwf": 70.5, "gfs": 66.2, "nws": 63.0, "metar": 92.0})
    status, reason = t24.check_metar_sanity(row)
    assert status == "warn"
    assert "metar_diverges_" in reason


# -------- predict_summary (mirror of predict_event picks) --------

def test_predict_summary_highest_probability_is_max_prob():
    row = make_row()
    s = t24.predict_summary(row["buckets"])
    assert s["highest_probability"]["subtitle"] == "67° or below"  # 57.6%


def test_predict_summary_best_ev_no_respects_min_threshold():
    # Bump all EV_NO below the 5c threshold -> no best_ev_no surfaced.
    row = make_row()
    for b in row["buckets"]:
        b["ev_no"] = 0.01
    s = t24.predict_summary(row["buckets"])
    assert s["best_ev_no"] is None


def test_predict_summary_sanity_filter_drops_confident_no():
    # If market is at YES >= 0.85 AND model prob <= 0.40, drop the NO pick
    # (mirrors kalshi_temp._sanity_keep_no).
    row = make_row()
    target = row["buckets"][0]
    target["yes_ask"] = 0.88
    target["prob"]    = 0.35
    target["ev_no"]   = 0.30
    # Zero out other NO EVs so target would otherwise win.
    for b in row["buckets"][1:]:
        b["ev_no"] = 0.0
    s = t24.predict_summary(row["buckets"])
    assert s["best_ev_no"] is None  # sanity-filtered


def test_predict_summary_matches_predict_event_logic():
    # Build a row, then compare predict_summary output against the inline
    # logic predict_event uses in kalshi_temp.py.
    row = make_row()
    bs = row["buckets"]
    expected_top = max(bs, key=lambda b: b["prob"])
    expected_by  = max((b for b in bs if b["ev_yes"] >= 0.05),
                       key=lambda b: b["ev_yes"], default=None)
    expected_bn  = max((b for b in bs if b["ev_no"]  >= 0.05
                        and not (b["yes_ask"] >= 0.85 and b["prob"] <= 0.40)),
                       key=lambda b: b["ev_no"],  default=None)
    s = t24.predict_summary(bs)
    assert s["highest_probability"] == expected_top
    assert s["best_ev_yes"]         == expected_by
    assert s["best_ev_no"]          == expected_bn


# -------- pick_best_candidate fallback --------

def test_pick_best_uses_first_passing():
    cands = [make_row(lead=24.05)]
    log = []
    row, qc = t24.pick_best_candidate(cands, NOW, log)
    assert row is not None
    assert qc["tried_rows"] == 1
    assert qc["errors"] == []


def test_pick_best_falls_back_on_hard_error():
    cands = [
        make_row(lead=24.05, mu=None),  # first fails (model null)
        make_row(lead=25.0),              # second passes
    ]
    log = []
    row, qc = t24.pick_best_candidate(cands, NOW, log)
    assert row is not None
    assert row["lead_hours"] == 25.0
    assert qc["tried_rows"] == 2


def test_pick_best_returns_none_when_all_fail():
    cands = [make_row(lead=24.05, mu=None), make_row(lead=25.0, mu=None)]
    log = []
    row, qc = t24.pick_best_candidate(cands, NOW, log)
    assert row is None
    assert "no_model_at_t24" in qc["errors"]


def test_pick_best_ignores_outside_band():
    # Lead of 35h is outside +/-6h band -> no candidates.
    cands = [make_row(lead=35.0)]
    log = []
    row, qc = t24.pick_best_candidate(cands, NOW, log)
    assert row is None
    assert "no_snapshot_in_band" in qc["errors"]


# -------- settlement overlay --------

def test_latest_settled_bucket_found():
    rows = [
        make_row(lead=24.0, ts="2026-05-21T04:55:59+00:00"),
        make_row(lead=2.0,  ts="2026-05-22T02:55:59+00:00",
                 settled=True, settled_bucket="67° to 68°"),
    ]
    out = t24.latest_settled_bucket(rows, "2026-05-21", "KXHIGHNY")
    assert out == "67° to 68°"


def test_latest_settled_bucket_none_if_unsettled():
    rows = [make_row(lead=24.0, ts="2026-05-21T04:55:59+00:00")]
    assert t24.latest_settled_bucket(rows, "2026-05-21", "KXHIGHNY") is None


# -------- end-to-end main() --------

def _write_log(tmp_path, rows):
    log = tmp_path / "live_picks_log.jsonl"
    with log.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return log


def _run_main(monkeypatch, tmp_path, rows, now=NOW):
    log = _write_log(tmp_path, rows)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(t24, "LOG_PATH", log)
    monkeypatch.setattr(t24, "CARDS_DIR", tmp_path / "t24_cards")
    fake_dt = type("FakeDT", (), {
        "now": staticmethod(lambda tz=None: now if tz is None else now.astimezone(tz))
    })
    # We don't monkey-patch datetime in t24 because main() calls datetime.now()
    # via the import. Instead, write a known-recent ts on the rows so age check
    # passes regardless of real wall-clock when this test runs.
    return t24.main()


def test_main_writes_archive_with_all_seven_events(monkeypatch, tmp_path):
    # One snapshot per series, all today (in ET).
    rows = []
    for series in t24.KXHIGH_SERIES:
        ts = datetime.now(timezone.utc).isoformat()  # fresh ts -> age check ok
        rows.append(make_row(series=series, target_date=t24.today_et(), lead=24.05, ts=ts))
    rc = _run_main(monkeypatch, tmp_path, rows)
    assert rc == 0
    archive = tmp_path / "t24_cards" / f"{t24.today_et()}.json"
    assert archive.exists()
    data = json.loads(archive.read_text(encoding="utf-8"))
    assert data["qc_summary"]["ok"] + data["qc_summary"]["warnings"] == 7
    assert data["qc_summary"]["errors"] == 0
    assert len(data["events"]) == 7
    for ev in data["events"]:
        assert ev.get("series") in t24.KXHIGH_SERIES
        assert ev.get("status") != "missing"


def test_main_marks_missing_when_no_snapshot(monkeypatch, tmp_path):
    rows = []  # empty log
    rc = _run_main(monkeypatch, tmp_path, rows)
    assert rc == 0
    archive = tmp_path / "t24_cards" / f"{t24.today_et()}.json"
    blocked = tmp_path / "t24_cards" / f"blocked_{t24.today_et()}.json"
    # All 7 missing -> blocked sentinel, no archive.
    assert blocked.exists()
    assert not archive.exists()


def test_main_writes_qc_log(monkeypatch, tmp_path):
    ts = datetime.now(timezone.utc).isoformat()
    rows = [make_row(series="KXHIGHNY", target_date=t24.today_et(), lead=24.05, ts=ts)]
    _run_main(monkeypatch, tmp_path, rows)
    qc_log = tmp_path / "t24_cards" / f"qc_{t24.today_et()}.log"
    assert qc_log.exists()
    text = qc_log.read_text(encoding="utf-8")
    assert "[KXHIGHNY]" in text
    assert "qc_summary" in text


def test_atomic_write_uses_temp_then_rename(monkeypatch, tmp_path):
    # If the rename worked, the .tmp file should not be left behind.
    ts = datetime.now(timezone.utc).isoformat()
    rows = [make_row(series=s, target_date=t24.today_et(), lead=24.05, ts=ts)
            for s in t24.KXHIGH_SERIES]
    _run_main(monkeypatch, tmp_path, rows)
    archive = tmp_path / "t24_cards" / f"{t24.today_et()}.json"
    tmp_file = archive.with_suffix(archive.suffix + ".tmp")
    assert archive.exists()
    assert not tmp_file.exists()
