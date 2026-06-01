"""Problem 2b: live-path forecast resilience — retry on transient errors +
on-disk cache. These run fully offline (fake session)."""
import pytest


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


def _install_cache(monkeypatch, tmp_path):
    import kalshi_temp as kt
    from lab.data_cache import DataCache
    cache = DataCache(tmp_path / "fc.sqlite")
    monkeypatch.setattr(kt, "_forecast_cache", cache)   # bypass lazy init
    monkeypatch.setattr(kt.time, "sleep", lambda *_: None)  # no backoff delay in tests
    return kt


def test_open_meteo_retries_transient_then_caches(tmp_path, monkeypatch):
    kt = _install_cache(monkeypatch, tmp_path)
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise kt.requests.exceptions.ConnectionError("reset by peer")
        return _FakeResp({"daily": {"temperature_2m_max": [72.5]}})

    monkeypatch.setattr(kt.session, "get", fake_get)

    v1 = kt.fetch_open_meteo(40.0, -73.0, "2026-05-31", "gfs_seamless")
    assert v1 == 72.5
    assert calls["n"] == 2          # one transient failure, retried once -> success

    v2 = kt.fetch_open_meteo(40.0, -73.0, "2026-05-31", "gfs_seamless")
    assert v2 == 72.5
    assert calls["n"] == 2          # served from cache: NO new network call


def test_failed_fetch_is_not_cached(tmp_path, monkeypatch):
    kt = _install_cache(monkeypatch, tmp_path)
    calls = {"n": 0}
    state = {"fail": True}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if state["fail"]:
            raise kt.requests.exceptions.ConnectionError("down")
        return _FakeResp({"daily": {"temperature_2m_max": [80.1]}})

    monkeypatch.setattr(kt.session, "get", fake_get)

    # all attempts fail -> returns None, must NOT cache the failure
    assert kt.fetch_open_meteo(40.0, -73.0, "2026-05-31", "gfs_seamless") is None
    n_after_fail = calls["n"]
    assert n_after_fail >= 2        # retried, didn't give up after one attempt

    # network recovers -> next call must hit the network (None was not cached) and succeed
    state["fail"] = False
    assert kt.fetch_open_meteo(40.0, -73.0, "2026-05-31", "gfs_seamless") == 80.1
    assert calls["n"] > n_after_fail


def test_historical_path_bypasses_live_cache(tmp_path, monkeypatch):
    """historical=True is the backtest path (lab/inputs.py owns its own cache);
    it must not read/write the live forecast cache."""
    kt = _install_cache(monkeypatch, tmp_path)
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        # historical responses are model-suffixed
        return _FakeResp({"daily": {"temperature_2m_max_gfs_seamless": [55.0]}})

    monkeypatch.setattr(kt.session, "get", fake_get)
    assert kt.fetch_open_meteo(1.0, 2.0, "2026-05-01", "gfs_seamless", historical=True) == 55.0
    assert kt.fetch_open_meteo(1.0, 2.0, "2026-05-01", "gfs_seamless", historical=True) == 55.0
    assert calls["n"] == 2          # NOT cached: both calls hit the network


def test_metar_uses_short_ttl_and_caches(tmp_path, monkeypatch):
    kt = _install_cache(monkeypatch, tmp_path)
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return _FakeResp([{"temp": 20.0}])   # 20C -> 68F

    monkeypatch.setattr(kt.session, "get", fake_get)
    assert kt.fetch_metar_temp("KNYC") == pytest.approx(68.0)
    assert kt.fetch_metar_temp("KNYC") == pytest.approx(68.0)
    assert calls["n"] == 1          # cache hit on 2nd call
