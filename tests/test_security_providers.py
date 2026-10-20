import asyncio
import time

import pytest

from geoosint.config import Settings
from geoosint.core.models import Bounds
from geoosint.errors import GeoError
from geoosint.providers.cache import Cache
from geoosint.providers.osm import OSMProvider, query, retry_delay
from geoosint.security.network import pinned_endpoint, validate_addresses
from geoosint.security.paths import artifact_path


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "::1",
        "fc00::1",
        "0.0.0.0",
        "::ffff:127.0.0.1",
        "192.168.1.1",
    ],
)
def test_private_addresses(address):
    with pytest.raises(GeoError):
        validate_addresses(["8.8.8.8", address])


async def test_arbitrary_urls():
    for url in [
        "http://127.0.0.1",
        "https://overpass-api.de@127.0.0.1/api/interpreter",
        "http://2130706433",
        "file:///etc/passwd",
    ]:
        with pytest.raises(GeoError):
            await pinned_endpoint(url)


def test_path_traversal(tmp_path):
    for name in ["../secret", "C:\\Windows", "../../etc/passwd", "abc", "a/../b"]:
        with pytest.raises(ValueError):
            artifact_path(tmp_path, name)


@pytest.mark.parametrize(
    "config",
    [{"mode": "production"}, {"host": "0.0.0.0"}, {"host": "192.168.0.1"}, {"mode": "public"}],
)
def test_public_startup_blocked(config, tmp_path):
    with pytest.raises(ValueError):
        Settings(data_dir=tmp_path, **config).validate_startup()


def test_queries_and_cache(tmp_path):
    bounds = Bounds(south=0, west=179.99, north=0.01, east=-179.99)
    ql = query(bounds, "roads", 100_000_000)
    assert ql.count("nwr") == 2
    with pytest.raises(GeoError):
        query(bounds, 'roads"]);out;(', 100_000_000)
    with pytest.raises(GeoError):
        query(Bounds(south=-80, west=-170, north=80, east=170), "roads", 100_000_000)
    cache = Cache(tmp_path / "cache.sqlite", 1024)
    key = cache.key({"a": 1})
    cache.put(key, {"features": [1], "source": "test"})
    assert cache.get(key)["features"] == [1]
    cache.cx.execute("UPDATE entries SET expires=?", (time.time() - 1,))
    assert cache.get(key) is None
    for i in range(20):
        cache.put(str(i), {"features": ["x" * 100]})
    assert (
        cache.cx.execute("SELECT sum(length(CAST(value AS BLOB))) FROM entries").fetchone()[0]
        <= 1024
    )
    cache.close()


async def test_stampede_with_fixture(tmp_path, monkeypatch):
    settings = Settings(
        data_dir=tmp_path, osm_enabled=True, osm_user_agent="GeoOSINT/0.1 (fixture, no network)"
    )
    cache = Cache(tmp_path / "cache.sqlite", 8192)
    osm = OSMProvider(settings, cache)
    count = 0

    async def fetch(ql, bounds):
        nonlocal count
        count += 1
        await asyncio.sleep(0.01)
        return {"features": [{"source_id": "fixture:1"}], "source": "TEST_FIXTURE"}

    monkeypatch.setattr(osm, "_fetch", fetch)
    bounds = Bounds(south=0, west=0, north=0.01, east=0.01)
    results = await asyncio.gather(*(osm.features(bounds, "roads") for _ in range(10)))
    assert count == 1
    assert all(r["features"][0]["source_id"] == "fixture:1" for r in results)
    assert retry_delay("120", 0) == 120
    cache.close()
