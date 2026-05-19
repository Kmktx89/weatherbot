"""A shadow exception must NOT alter the live ModelOutput or crash the caller."""
import json
import time
from pathlib import Path

from dataclasses import replace

from model import ModelInputs, compute
from lab.configs import LIVE_TODAY, LIVE_MINUS_NWS


def _basic_inputs():
    return ModelInputs(
        series="KXHIGHNY", event_ticker="TEST-ISOLATION",
        target_date="2026-05-20",
        forecasts={"ecmwf": 72.0, "gfs": 70.0, "nws": None},
        metar_current=None, today_max=None,
        markets=[{"ticker": "T-MID", "strike_type": "between",
                  "floor_strike": 70, "cap_strike": 72}],
        decision_ts=int(time.time()), fetched_at=int(time.time()),
    )


def test_shadow_exception_doesnt_affect_live(tmp_path, monkeypatch):
    """If a shadow config raises, the live output must be unchanged."""
    from shadow import runner
    monkeypatch.setattr(runner, "SHADOW_PATH", str(tmp_path / "shadow.jsonl"))

    inputs = _basic_inputs()
    live_out = compute(inputs, LIVE_TODAY)

    # Craft a config that will blow up compute().
    bad_cfg = replace(LIVE_TODAY, name="will-explode",
                      source_weights=None)  # type: ignore[arg-type]
    runner.run_shadow(inputs, [bad_cfg])

    # Live recomputation must produce identical output to before.
    live_out_after = compute(inputs, LIVE_TODAY)
    assert live_out.mu == live_out_after.mu
    assert dict(live_out.probs) == dict(live_out_after.probs)


def test_shadow_writes_jsonl(tmp_path, monkeypatch):
    from shadow import runner
    monkeypatch.setattr(runner, "SHADOW_PATH", str(tmp_path / "shadow.jsonl"))
    inputs = _basic_inputs()
    runner.run_shadow(inputs, [LIVE_MINUS_NWS])
    rows = [json.loads(line) for line in
            Path(tmp_path / "shadow.jsonl").read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["config_name"] == "live-minus-nws"
    assert rows[0]["event_ticker"] == "TEST-ISOLATION"
    assert "code_version" in rows[0]
