"""ModelConfig — every knob that varies between live, backtest, and variants."""
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Literal


TodayMaxMode = Literal["off", "truncate", "push", "both"]


@dataclass(frozen=True)
class ModelConfig:
    name: str
    source_weights: Mapping[str, Mapping[str, float]]   # series → {source: weight}
    nws_blend: float                                     # 0.0 = no NWS overlay
    bias_table: Mapping[str, float]                      # series → bias offset (subtracted)
    base_sigma: float                                    # σ floor in °F
    sigma_sources: tuple[str, ...]                       # which keys feed pstdev
    today_max_mode: TodayMaxMode
    today_max_headroom: float                            # °F (e.g. 0.5)
    today_max_push: float                                # °F (only used if mode in {"push","both"})
    sanity_no_yes_ask_min: float                         # e.g. 0.85
    sanity_no_prob_max: float                            # e.g. 0.40
    decision_lead_hours: float                           # e.g. 24.0
