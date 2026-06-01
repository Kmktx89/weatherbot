"""snapshot._bucket carries the calibrated fields into the log row."""
import snapshot


def test_bucket_serializes_cal_fields():
    b = {"ticker": "T", "subtitle": "x", "strike_type": "between",
         "yes_bid": 0.1, "yes_ask": 0.12, "no_ask": 0.88, "prob": 0.2,
         "ev_yes": -0.1, "ev_no": 0.0, "vol_24h": 100,
         "cal_prob_yes": 0.2, "cal_prob_no": 0.69,
         "cal_ev_yes": 0.08, "cal_ev_no": -0.19}
    out = snapshot._bucket(b, {})
    assert out["cal_prob_no"] == 0.69
    assert out["cal_ev_no"] == -0.19
    assert out["cal_prob_yes"] == 0.2
    assert out["cal_ev_yes"] == 0.08
