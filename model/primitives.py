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


def bucket_bounds(market) -> tuple[float, float] | None:
    """Continuous bounds for a Kalshi bucket. Lower/upper may be ±inf.

    The 0.5 offsets match Kalshi's bucket semantics: 'less cap=50' means
    integer high <= 49, encoded as the continuous interval (-inf, 49.5);
    'between floor=70 cap=72' means 70 <= high <= 72, encoded (69.5, 72.5).
    """
    st = market.get("strike_type")
    cap = market.get("cap_strike")
    floor = market.get("floor_strike")
    if st == "less" and cap is not None:
        return float("-inf"), cap - 0.5
    if st == "greater" and floor is not None:
        return floor + 0.5, float("inf")
    if st == "between" and floor is not None and cap is not None:
        return floor - 0.5, cap + 0.5
    return None


def bucket_probability(
    bounds: tuple[float, float],
    mu: float,
    sigma: float,
    *,
    lower_truncation: float | None = None,
) -> float | None:
    """Probability the day's high falls in `bounds`.

    With `lower_truncation` set (e.g. an observed afternoon peak), returns
    the conditional probability P(bucket | high >= lower_truncation),
    zeroing buckets that lie entirely below the truncation and
    renormalising over the surviving tail.
    """
    lower, upper = bounds
    if lower_truncation is None:
        return normal_cdf(upper, mu, sigma) - normal_cdf(lower, mu, sigma)
    if upper <= lower_truncation:
        return 0.0
    denom = 1.0 - normal_cdf(lower_truncation, mu, sigma)
    if denom <= 0:
        return 1.0 if lower_truncation < upper else 0.0
    eff_lower = max(lower, lower_truncation)
    raw = normal_cdf(upper, mu, sigma) - normal_cdf(eff_lower, mu, sigma)
    return max(0.0, raw / denom)
