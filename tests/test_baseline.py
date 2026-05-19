"""Frozen-output regression on real recorded events.

If a change to model/compute.py is intentional, run:
    pytest --update-baseline
review the JSON diff in `git diff`, and commit.
"""
import json
from pathlib import Path

import pytest

from lab.configs import LIVE_TODAY
from model import ModelInputs, compute


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "baseline"
TOLERANCE = 1e-4  # 4 decimal places


def _fixture_paths():
    return sorted(FIXTURE_DIR.glob("*.json"))


@pytest.mark.parametrize("path", _fixture_paths(), ids=lambda p: p.stem)
def test_live_today_matches_baseline(path, update_baseline):
    fixture = json.loads(path.read_text())
    inputs = ModelInputs(
        series=fixture["series"],
        event_ticker=fixture["event_ticker"],
        target_date=fixture["target_date"],
        forecasts=fixture["forecasts"],
        metar_current=fixture.get("metar_current"),
        today_max=fixture.get("today_max"),
        markets=fixture["markets"],
        decision_ts=fixture["captured_at"],
        fetched_at=fixture["captured_at"],
    )
    out = compute(inputs, LIVE_TODAY)
    actual = {
        "mu": out.mu, "sigma": out.sigma, "mu_raw": out.mu_raw,
        "bias_applied": out.bias_applied, "truncation": out.truncation,
        "today_max_active": out.today_max_active,
        "probs": dict(out.probs),
    }
    expected = fixture["expected"]

    if update_baseline:
        fixture["expected"] = actual
        path.write_text(json.dumps(fixture, indent=2))
        pytest.skip(f"baseline rewritten for {path.stem}")

    # numeric fields with tolerance
    for key in ("mu", "sigma", "mu_raw", "bias_applied", "truncation"):
        if expected[key] is None or actual[key] is None:
            assert expected[key] == actual[key], f"{key}: {expected[key]} vs {actual[key]}"
        else:
            assert actual[key] == pytest.approx(expected[key], abs=TOLERANCE), \
                f"{path.stem}/{key}"

    assert actual["today_max_active"] == expected["today_max_active"], \
        f"{path.stem}/today_max_active"

    # probs: keys must match, values match to tolerance
    assert set(actual["probs"]) == set(expected["probs"]), \
        f"{path.stem} probs key mismatch"
    for ticker, ep in expected["probs"].items():
        assert actual["probs"][ticker] == pytest.approx(ep, abs=TOLERANCE), \
            f"{path.stem}/probs/{ticker}"
