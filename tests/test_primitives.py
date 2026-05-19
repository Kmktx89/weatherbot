import math

from model.primitives import normal_cdf, weighted_mean


def test_normal_cdf_at_mean_is_half():
    assert normal_cdf(0.0, 0.0, 1.0) == 0.5

def test_normal_cdf_monotonic():
    samples = [normal_cdf(x, 0.0, 1.0) for x in (-3, -1, 0, 1, 3)]
    assert all(a <= b for a, b in zip(samples, samples[1:]))

def test_normal_cdf_extremes():
    assert normal_cdf(-10.0, 0.0, 1.0) < 1e-15
    assert normal_cdf(10.0, 0.0, 1.0) > 1 - 1e-15

def test_normal_cdf_sigma_zero_degenerate():
    # σ=0 is a point mass at μ; CDF is a step function.
    assert normal_cdf(5.0, 5.0, 0.0) == 1.0
    assert normal_cdf(4.9, 5.0, 0.0) == 0.0
    assert normal_cdf(5.1, 5.0, 0.0) == 1.0


def test_weighted_mean_all_present():
    out = weighted_mean({"a": 10.0, "b": 20.0}, {"a": 0.3, "b": 0.7})
    assert out == pytest_approx(17.0)

def test_weighted_mean_one_missing():
    out = weighted_mean({"a": 10.0, "b": None}, {"a": 0.3, "b": 0.7})
    assert out == pytest_approx(10.0)  # renormalised to weight 'a' alone

def test_weighted_mean_all_missing_returns_none():
    assert weighted_mean({"a": None, "b": None}, {"a": 0.3, "b": 0.7}) is None

def test_weighted_mean_zero_total_weight_returns_none():
    assert weighted_mean({"a": 10.0}, {"a": 0.0}) is None

def test_weighted_mean_ignores_unknown_keys():
    out = weighted_mean({"a": 1.0, "z": 99.0}, {"a": 1.0})
    assert out == pytest_approx(1.0)


def pytest_approx(v, tol=1e-9):
    from pytest import approx
    return approx(v, abs=tol)
