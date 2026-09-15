"""连接池/健康分单元测试（mock服务器，不连真实网络）"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock

import pytest

from pytdxdata.pool.health import HealthEngine
from pytdxdata.pool.pool import DynamicPool, PoolTimeoutError


# ---------- HealthEngine ----------

def test_health_default_score():
    h = HealthEngine()
    assert h.score("server1") == 1.0


def test_health_failure_penalty():
    h = HealthEngine()
    h.record_failure("s1")
    assert h.score("s1") == 0.5
    h.record_failure("s1")
    h.record_failure("s1")
    assert h.in_cooldown("s1")  # 连续3次失败进入冷却


def test_health_success_recovery():
    h = HealthEngine()
    h.record_failure("s1")
    h.record_success("s1")
    assert h.score("s1") == pytest.approx(0.7)
    assert not h.in_cooldown("s1")


def test_health_rank():
    h = HealthEngine()
    h.record_success("fast", latency=0.05)
    h.record_success("slow", latency=0.5)
    ranked = h.rank(["slow", "fast"])
    assert ranked[0] == "fast"


# ---------- DynamicPool ----------

class FakeConn:
    def __init__(self, host):
        self.host = host
        self.alive_flag = True

    @property
    def alive(self):
        return self.alive_flag

    async def connect(self):
        pass

    async def ping(self):
        return 0.01

    async def close(self):
        self.alive_flag = False

    async def execute(self, cmd):
        return "ok"


@pytest.fixture
def fake_pool():
    with mock.patch("pytdxdata.pool.pool.TdxConnection", side_effect=lambda h, p=7709, t=10.0: FakeConn(h)):
        pool = DynamicPool(["s1", "s2"], min_size=1, max_size=3, per_server_cap=2,
                           idle_ttl=0.2, scale_down_grace=0, reaper_interval=0.05)
        yield pool


@pytest.mark.asyncio
async def test_pool_acquire_release(fake_pool):
    await fake_pool.start()
    pc = await fake_pool.acquire()
    assert pc.server in ("s1", "s2")
    assert fake_pool.stats.borrowed == 1
    await fake_pool.release(pc)
    assert fake_pool.stats.borrowed == 0
    await fake_pool.close()


@pytest.mark.asyncio
async def test_pool_acquire_many(fake_pool):
    await fake_pool.start()
    conns = await fake_pool.acquire_many(2)
    assert len(conns) == 2
    servers = {c.server for c in conns}
    for c in conns:
        await fake_pool.release(c)
    await fake_pool.close()


@pytest.mark.asyncio
async def test_pool_broken_release(fake_pool):
    await fake_pool.start()
    pc = await fake_pool.acquire()
    await fake_pool.release(pc, broken=True)
    assert fake_pool.stats.total <= fake_pool.min_size
    await fake_pool.close()


@pytest.mark.asyncio
async def test_pool_scale_down(fake_pool):
    """空闲连接超时后缩容到 min_size。"""
    await fake_pool.start()
    pc1 = await fake_pool.acquire()
    await fake_pool.release(pc1)
    pc2 = await fake_pool.acquire()
    await fake_pool.release(pc2)
    total_before = fake_pool.stats.total
    await asyncio.sleep(0.3)  # 等 reaper 缩容
    assert fake_pool.stats.total <= total_before
    await fake_pool.close()
