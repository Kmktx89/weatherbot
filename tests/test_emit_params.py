"""lab.live_calibration.emit_params writes a loadable calibration_params.json."""
import json

import kalshi_temp as kt
from lab.live_calibration import emit_params
from lab.calibration import CalibrationReport


class _Rec:
    def __init__(self, no_pred, no_won):
        self.event_ticker = "E"; self.lead_hours = 24.0
        self.yes_pred = None; self.yes_won = None
        self.no_pred = no_pred; self.no_won = no_won


def test_emit_params_writes_no_band_gap(tmp_path):
    # 4 NO obs: printed 0.90 each, 3 wins -> realized 0.75 -> gap +0.15
    recs = [_Rec(0.90, 1), _Rec(0.90, 1), _Rec(0.90, 1), _Rec(0.90, 0)]
    out = tmp_path / "calibration_params.json"
    emit_params(recs, str(out))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["yes"] == [{"lo": 0.0, "hi": 1.01, "h": 0.0}]   # identity per deployed decision
    assert data["no"][0]["lo"] == 0.80
    assert abs(data["no"][0]["h"] - 0.15) < 1e-6


def test_emitted_params_round_trip_through_loader(tmp_path):
    recs = [_Rec(0.90, 1), _Rec(0.90, 0)]   # gap +0.40
    out = tmp_path / "calibration_params.json"
    emit_params(recs, str(out))
    loaded = kt.load_calibration_params(str(out))
    assert kt.haircut_for("no", 0.90, loaded) == loaded["no"][0]["h"]
