import time

import pytest

from lab.data_cache import DataCache


@pytest.fixture
def cache(tmp_path):
    return DataCache(tmp_path / "cache.sqlite")


def test_set_get_roundtrip(cache):
    cache.set("k1", {"value": 42}, source="open_meteo:hist", target_date="2026-05-01")
    assert cache.get("k1", ttl=None) == {"value": 42}


def test_get_missing_returns_none(cache):
    assert cache.get("nope", ttl=None) is None


def test_ttl_expiry(cache):
    cache.set("k1", "v", source="t", target_date=None)
    assert cache.get("k1", ttl=300) == "v"
    # Force the row to look old
    cache._conn.execute("UPDATE fetches SET fetched_at = ?", (int(time.time()) - 1000,))
    cache._conn.commit()
    assert cache.get("k1", ttl=300) is None


def test_ttl_none_means_forever(cache):
    cache.set("k1", "v", source="t", target_date="2020-01-01")
    cache._conn.execute("UPDATE fetches SET fetched_at = ?", (0,))
    cache._conn.commit()
    assert cache.get("k1", ttl=None) == "v"


def test_purge_before_date(cache):
    cache.set("k_old", "old", source="t", target_date="2025-01-01")
    cache.set("k_new", "new", source="t", target_date="2026-06-01")
    n = cache.purge_before("2026-01-01")
    assert n == 1
    assert cache.get("k_old", ttl=None) is None
    assert cache.get("k_new", ttl=None) == "new"


def test_stats(cache):
    cache.set("k1", "v", source="t", target_date=None)
    cache.set("k2", "v", source="t", target_date=None)
    s = cache.stats()
    assert s["total"] == 2
