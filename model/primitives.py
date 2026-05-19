"""Pure-math primitives used by model.compute."""
import math
from collections.abc import Mapping


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    """CDF of N(mu, sigma^2) at x. Degenerate sigma=0 returns step function."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2))))


def weighted_mean(
    values: Mapping[str, float | None],
    weights: Mapping[str, float],
) -> float | None:
    """Weighted mean over keys present in both `values` and `weights`.

    Missing values (None) are dropped and weights are renormalised over
    surviving keys. Returns None if no key has a non-None value or if
    the surviving weights sum to <= 0.
    """
    parts: list[tuple[float, float]] = []
    for key, w in weights.items():
        v = values.get(key)
        if v is None:
            continue
        if w <= 0:
            continue
        parts.append((v, w))
    if not parts:
        return None
    total_w = sum(w for _, w in parts)
    if total_w <= 0:
        return None
    return sum(v * w for v, w in parts) / total_w
