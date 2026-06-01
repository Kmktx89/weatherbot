import pytest

import kalshi_temp as kt
from lab.calibration import report_from_pairs
from lab import live_calibration as lc


@pytest.fixture(autouse=True)
def _identity_calibration(monkeypatch):
    """These tests verify NO-pick selection mechanics, not the (provisional)
    production haircut. Pin calibration to identity so cal_ev_no == ev_no and
    best_no_pick reduces to its pre-calibration selection behavior."""
    identity = {"no": [{"lo": 0.0, "hi": 1.01, "h": 0.0}],
                "yes": [{"lo": 0.0, "hi": 1.01, "h": 0.0}]}
    monkeypatch.setattr(kt, "_CAL_PARAMS", identity)


# --- fixtures -------------------------------------------------------------

def bkt(ticker, subtitle, *, prob=None, ev_no=None, yes_ask=None):
    # Real rows always carry no_ask, and ev_no = (1 - prob) - no_ask. The new
    # best_no_pick derives cal_ev_no from no_ask, so reconstruct a consistent
    # no_ask from the test's stated prob/ev_no (under the identity calibration
    # fixture below, cal_ev_no == ev_no exactly).
    no_ask = None if (prob is None or ev_no is None) else (1 - prob) - ev_no
    return {"ticker": ticker, "subtitle": subtitle, "prob": prob,
            "ev_no": ev_no, "ev_yes": None, "yes_ask": yes_ask, "no_ask": no_ask}


def row(event, lead, buckets, *, settled_bucket=None, ts="2026-05-25T00:00:00+00:00"):
    return {"ts": ts, "event_ticker": event, "lead_hours": lead,
            "settled": settled_bucket is not None, "settled_bucket": settled_bucket,
            "buckets": buckets}


# --- report_from_pairs math ----------------------------------------------

def test_report_from_pairs_basic_math():
    rep = report_from_pairs("yes", [(0.5, 1), (0.5, 0)])
    assert rep.n_bets == 2
    assert rep.mean_pred == pytest.approx(0.5)
    assert rep.realized_rate == pytest.approx(0.5)
    assert rep.brier_score == pytest.approx(0.25)


def test_report_from_pairs_empty():
    rep = report_from_pairs("no", [])
    assert rep.n_bets == 0 and rep.log_loss is None


def test_report_gap_sign():
    # model says 80% but only 60% realized -> overconfident, gap negative
    rep = report_from_pairs("no", [(0.8, 1), (0.8, 1), (0.8, 1), (0.8, 0), (0.8, 0)])
    assert rep.mean_pred == pytest.approx(0.8)
    assert rep.realized_rate == pytest.approx(0.6)
    assert (rep.realized_rate - rep.mean_pred) == pytest.approx(-0.2)


# --- YES pick: argmax prob ------------------------------------------------

def test_yes_pick_argmax_prob():
    buckets = [bkt("A", "lo", prob=0.2), bkt("B", "mid", prob=0.5), bkt("C", "hi", prob=0.3)]
    assert lc._yes_pick(buckets)["ticker"] == "B"


def test_yes_pick_none_when_no_probs():
    assert lc._yes_pick([bkt("A", "lo"), bkt("B", "mid")]) is None


# --- NO pick: MIN_BEST_EV floor + sanity cap + argmax ev_no ---------------

def test_no_pick_argmax_ev_no_above_floor():
    buckets = [bkt("A", "lo", prob=0.1, ev_no=0.02, yes_ask=0.10),   # below MIN_BEST_EV
               bkt("B", "mid", prob=0.2, ev_no=0.10, yes_ask=0.10),
               bkt("C", "hi", prob=0.3, ev_no=0.07, yes_ask=0.10)]
    assert lc._no_pick(buckets)["ticker"] == "B"


def test_no_pick_none_when_all_below_floor():
    buckets = [bkt("A", "lo", prob=0.1, ev_no=0.02, yes_ask=0.10),
               bkt("B", "mid", prob=0.2, ev_no=0.04, yes_ask=0.10)]
    assert kt.MIN_BEST_EV == 0.05
    assert lc._no_pick(buckets) is None


def test_no_pick_sanity_cap_excludes_confident_market():
    # huge ev_no but market is confident YES (yes_ask>=0.85) and model says prob<=0.40
    # -> excluded; the next eligible bucket (printed-NO >= 0.80) wins instead.
    buckets = [bkt("CAP", "tail", prob=0.30, ev_no=0.50, yes_ask=0.90),
               bkt("OK", "mid", prob=0.15, ev_no=0.08, yes_ask=0.20)]  # printed-NO 0.85
    pick = lc._no_pick(buckets)
    assert pick["ticker"] == "OK"


def test_no_pick_none_when_only_capped_bucket():
    buckets = [bkt("CAP", "tail", prob=0.30, ev_no=0.50, yes_ask=0.90)]
    assert lc._no_pick(buckets) is None


def test_no_pick_printed_floor_excludes_low_conviction():
    # printed-NO = 1 - 0.25 = 0.75 < 0.80 floor -> excluded even though ev_no is fat
    buckets = [bkt("LOWCONV", "mid", prob=0.25, ev_no=0.40, yes_ask=0.20)]
    assert lc._no_pick(buckets) is None


def test_no_pick_printed_floor_keeps_high_conviction():
    # printed-NO = 1 - 0.15 = 0.85 >= 0.80 floor -> kept
    buckets = [bkt("HICONV", "tail", prob=0.15, ev_no=0.10, yes_ask=0.10)]
    assert lc._no_pick(buckets)["ticker"] == "HICONV"


# --- winner resolution: log subtitle -> ticker ----------------------------

def test_winner_ticker_from_settled_bucket():
    rows = [row("E", 24, [bkt("T-LOW", "70° or below", prob=0.2),
                          bkt("T-HI", "82° to 83°", prob=0.5)],
                settled_bucket="82° to 83°")]
    assert lc._winner_ticker(rows, "E", cache=None) == "T-HI"


def test_winner_ticker_kalshi_fallback(monkeypatch):
    rows = [row("E", 24, [bkt("T-LOW", "lo", prob=0.2), bkt("T-HI", "hi", prob=0.5)])]
    monkeypatch.setattr(lc, "fetch_event_markets", lambda ev, cache: [{"ticker": "T-HI", "result": "yes"}])
    monkeypatch.setattr(lc, "winner_of", lambda markets: markets[0])
    assert lc._winner_ticker(rows, "E", cache=object()) == "T-HI"


# --- build_records: nearest-lead selection + scoring ----------------------

def test_build_records_picks_nearest_lead_and_scores():
    buckets_near = [bkt("T-LOW", "lo", prob=0.2, ev_no=0.10, yes_ask=0.20),
                    bkt("WIN", "hi", prob=0.6, ev_no=0.02, yes_ask=0.55)]
    buckets_far = [bkt("T-LOW", "lo", prob=0.9, ev_no=0.10, yes_ask=0.20),
                   bkt("WIN", "hi", prob=0.1, ev_no=0.50, yes_ask=0.05)]
    rows = [
        row("E", 36.0, buckets_far),
        row("E", 23.0, buckets_near),
        row("E", 2.0, [], settled_bucket="hi"),  # settled row, null probs
    ]
    recs, skips, leads = lc.build_records(rows, target_lead=24.0, cache=None)
    assert len(recs) == 1 and not skips
    r = recs[0]
    assert r.lead_hours == 23.0            # nearest to 24
    assert r.yes_pred == pytest.approx(0.6)  # from the near row, not far row's 0.9
    assert r.yes_won == 1                   # WIN bucket is the winner
    # NO pick on near row: T-LOW (ev_no 0.10, sanity ok) is the only one >= floor
    assert r.no_pred == pytest.approx(0.8)  # 1 - 0.20
    assert r.no_won == 1                    # T-LOW != WIN


def test_build_records_skips_unsettled(monkeypatch):
    # No settled_bucket and Kalshi reports no winner -> event is skipped.
    monkeypatch.setattr(lc, "fetch_event_markets", lambda ev, cache: [])
    monkeypatch.setattr(lc, "winner_of", lambda markets: None)
    rows = [row("E", 24.0, [bkt("A", "lo", prob=0.5)])]
    recs, skips, leads = lc.build_records(rows, target_lead=24.0, cache=object())
    assert recs == [] and skips.get("not_settled") == 1


def test_build_records_skips_settled_but_no_pred_row():
    # winner known, but every pred-bearing row was nulled (only the settled row remains)
    rows = [row("E", 2.0, [bkt("WIN", "hi"), bkt("T-LOW", "lo")], settled_bucket="hi")]
    recs, skips, leads = lc.build_records(rows, target_lead=24.0, cache=None)
    assert recs == [] and skips.get("no_pred_row") == 1


def test_calibrate_by_lead_bins_rows(monkeypatch):
    buckets = [bkt("WIN", "hi", prob=0.6, ev_no=0.02, yes_ask=0.55),
               bkt("T-LOW", "lo", prob=0.4, ev_no=0.10, yes_ask=0.20)]
    rows = [row("E", 30.0, buckets, settled_bucket="hi"),
            row("E", 10.0, buckets, settled_bucket="hi")]
    out = lc.calibrate_by_lead(rows, cache=None)
    # one row each in [24-36) and [0-12)
    assert out[(24, 36)]["yes"].n_bets == 1
    assert out[(0, 12)]["yes"].n_bets == 1
    assert out[(12, 24)]["yes"].n_bets == 0
    assert out[(24, 36)]["yes"].realized_rate == pytest.approx(1.0)  # WIN won


def test_bucket_midpoint_interior_open_ended_and_junk():
    assert lc.bucket_midpoint("94° to 95°") == (94.5, "interior")
    assert lc.bucket_midpoint("70° or above") == (71.0, "open")
    assert lc.bucket_midpoint("69° or below") == (68.0, "open")
    assert lc.bucket_midpoint("") == (None, None)
    assert lc.bucket_midpoint(None) == (None, None)
    assert lc.bucket_midpoint("nonsense") == (None, None)
