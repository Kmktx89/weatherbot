"""Shared pytest fixtures and the --update-baseline flag for test_baseline.py."""
import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--update-baseline",
        action="store_true",
        default=False,
        help="Rewrite baseline fixtures with current model outputs (intentional drift only).",
    )


@pytest.fixture
def update_baseline(request):
    return request.config.getoption("--update-baseline")
