"""缓存单元测试：TTL/LRU/双层/single-flight"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from pytdxdata.cache.disk import DiskCache
from pytdxdata.cache.key import CacheKey
from pytdxdata.cache.manager import CacheManager
from pytdxdata.cache.memory import MemoryCache
from pytdxdata.cache.ttl import ttl_for


# ---------- CacheKey ----------

def test_key_roundtrip():
    k = CacheKey("kline", 1, "600000", "DAY", 0, 2400, "QFQ")
    s = k.to_str()
    assert CacheKey.from_str(s) == k


def test_quote_key_order_independent():
    a = CacheKey.quote_key([(0, "000001"), (1, "600000")])
    b = CacheKey.quote_key([(1, "600000"), (0, "000001")])
    assert a.to_str() == b.to_str()


# ---------- TTL ----------

def test_ttl_quote():
    assert ttl_for("quote") == 5.0


def test_ttl_kline_minute():
    assert ttl_for("kline", period="MIN_1") == 60.0


def test_ttl_kline_day():
    t = ttl_for("kline", period="DAY")
    assert 0 < t <= 12 * 3600


def test_ttl_hist_transaction_permanent():
    assert ttl_for("transaction", date=20260101) == 0.0


def test_ttl_today_transaction():
    assert ttl_for("transaction") == 60.0


# ---------- MemoryCache ----------

def test_memory_ttl_expiry():
    mc = MemoryCache()
    mc.set("k", b"v", ttl=0.1)
    assert mc.get("k") == b"v"
    import time
    time.sleep(0.15)
    assert mc.get("k") is None


def test_memory_lru_eviction():
    mc = MemoryCache(max_entries=2)
    mc.set("a", b"1", 10)
    mc.set("b", b"2", 10)
    mc.set("c", b"3", 10)
    assert len(mc) <= 2
    assert mc.get("a") is None  # 最旧被逐出


def test_memory_permanent():
    mc = MemoryCache()
    mc.set("k", b"v", ttl=0)  # 永久
    assert mc.get("k") == b"v"


# ---------- CacheManager + single-flight ----------

@pytest.mark.asyncio
async def test_manager_double_layer():
    tmp = Path(tempfile.mkdtemp()) / "cache.db"
    mem = MemoryCache()
    disk = DiskCache(tmp)
    mgr = CacheManager(mem, disk)
    await mgr.start()

    key = CacheKey("kline", 1, "600000", "DAY", 0, 800)
    fetch_count = 0

    async def fetcher():
        nonlocal fetch_count
        fetch_count += 1
        return b"payload"

    v1 = await mgr.get_or_fetch(key, fetcher)
    v2 = await mgr.get_or_fetch(key, fetcher)
    assert v1 == v2 == b"payload"
    assert fetch_count == 1  # 第二次命中缓存

    # 磁盘持久：新管理器读同一磁盘库
    mgr2 = CacheManager(MemoryCache(), DiskCache(tmp))
    await mgr2.start()
    v3 = await mgr2.get(key)
    assert v3 == b"payload"
    await mgr.close()
    await mgr2.close()


@pytest.mark.asyncio
async def test_manager_single_flight():
    mem = MemoryCache()
    mgr = CacheManager(mem, None)
    key = CacheKey("quote", extra="0:000001")
    fetch_count = 0

    async def fetcher():
        nonlocal fetch_count
        fetch_count += 1
        await asyncio.sleep(0.05)
        return b"x"

    await asyncio.gather(
        mgr.get_or_fetch(key, fetcher),
        mgr.get_or_fetch(key, fetcher),
        mgr.get_or_fetch(key, fetcher),
    )
    assert fetch_count == 1  # single-flight 只回源一次
    await mgr.close()


@pytest.mark.asyncio
async def test_manager_invalidate():
    mem = MemoryCache()
    mgr = CacheManager(mem, None)
    key = CacheKey("kline", 1, "600000", "DAY", 0, 800)
    await mgr.set(key, b"data")
    assert await mgr.get(key) == b"data"
    n = await mgr.invalidate("kline", market=1, code="600000")
    assert n >= 1
    assert await mgr.get(key) is None
    await mgr.close()
