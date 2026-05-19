import json
import sys
from datetime import datetime, timezone
from typing import Sequence

from model import ModelInputs, ModelConfig, compute


SHADOW_PATH = "shadow_picks.jsonl"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _top_pick(out, markets: Sequence[dict]):
    """Return (ticker, market_dict, prob) for the highest-prob bucket, or None."""
    if not out.probs:
        return None
    t = max(out.probs, key=lambda k: out.probs[k])
    m = next((m for m in markets if m["ticker"] == t), None)
    return t, m, out.probs[t]


def run_shadow(inputs: ModelInputs, configs: Sequence[ModelConfig]) -> None:
    """Compute each config against `inputs`; append one JSONL row per config.

    Any exception from compute() or pick-resolution is caught and logged to
    stderr. Never raises.
    """
    ts = _now_iso()
    for cfg in configs:
        try:
            out = compute(inputs, cfg)
            pick = _top_pick(out, inputs.markets)
            if pick is None:
                continue
            tkr, m, p = pick
            yes_ask = (m or {}).get("yes_ask_dollars")
            no_ask = (m or {}).get("no_ask_dollars")
            yes_ask_f = float(yes_ask) if yes_ask else None
            no_ask_f = float(no_ask) if no_ask else None
            ev_yes = (p - yes_ask_f) if yes_ask_f is not None else None
            ev_no = ((1 - p) - no_ask_f) if no_ask_f is not None else None
            row = {
                "ts": ts,
                "event_ticker": inputs.event_ticker,
                "config_name": cfg.name,
                "code_version": out.code_version,
                "mu": out.mu, "sigma": out.sigma, "mu_raw": out.mu_raw,
                "bias_applied": out.bias_applied,
                "truncation": out.truncation,
                "today_max_active": out.today_max_active,
                "pick_ticker": tkr, "pick_prob": p,
                "pick_yes_ask": yes_ask_f, "pick_no_ask": no_ask_f,
                "ev_yes": ev_yes, "ev_no": ev_no,
            }
            with open(SHADOW_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
        except Exception as e:
            sys.stderr.write(f"[shadow:{cfg.name}] {e}\n")
