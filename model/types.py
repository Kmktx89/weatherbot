"""Input / output dataclasses for model.compute."""
from dataclasses import dataclass
from collections.abc import Mapping, Sequence


@dataclass(frozen=True)
class ModelInputs:
    series: str                              # "KXHIGHNY"
    event_ticker: str                        # "KXHIGHNY-26MAY13"
    target_date: str                         # "2026-05-13"
    forecasts: Mapping[str, float | None]    # {"ecmwf": 72.4, "gfs": 71.8, "nws": 73.1}
    metar_current: float | None              # latest METAR (informational)
    today_max: float | None                  # max METAR observed today
    markets: Sequence[Mapping]               # raw Kalshi market dicts
    decision_ts: int                         # unix seconds at decision time
    fetched_at: int                          # unix seconds when captured


@dataclass(frozen=True)
class ModelOutput:
    mu: float | None
    sigma: float | None
    mu_raw: float | None                     # pre-bias mu (diagnostics)
    bias_applied: float                      # the BIAS value used
    truncation: float | None                 # active lower-truncation
    today_max_active: bool
    probs: Mapping[str, float]               # {ticker: P(bucket)}
    config_name: str
    code_version: str
