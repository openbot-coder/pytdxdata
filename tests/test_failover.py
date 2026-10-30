"""坏节点隔离回归：**一台服务器中途死掉，不能拖慢整批任务**。

三件事必须成立（缺一个就会把「单机故障」放大成「整批卡死」）：

1. **建连不能持池锁** —— 死节点握手要等满 ``timeout``，锁内做会把所有并发
   协程一起冻住。
2. **全节点失联要能退出** —— 扩容失败分支 ``continue`` 会无限重试，
   没有截止检查就永远不抛 ``PoolTimeoutError``。
3. **失败批次立刻回队 / 分页份额交给活着的 worker** —— 否则要么串行重试
   把长尾放大，要么那一整份数据没人做（静默缺页）。

替身全部在进程内，不连真实网络。
"""
from __future__ import annotations

import asyncio
import datetime
import time

import pytest

from pytdxdata import TdxData
from pytdxdata.models import KlinePeriod, SecurityBar
from pytdxdata.pool.pool import DynamicPool, PoolTimeoutError
from pytdxdata.scheduler.paginator import PageSpec, Paginator

NEWEST = datetime.datetime.now().replace(microsecond=0)  # noqa: DTZ001


# ====================================================================== #
# 一、建连不占池锁 + 全灭可退出
# ====================================================================== #

class _HostConn:
    """按 host 决定握手行为的替身连接。

    ``slow_hosts`` 里的主机握手会「卡住」``delay`` 秒后失败（模拟 SYN 被丢弃）；
    ``dead`` 为真时连握手都不做，立刻拒绝。
    """

    slow_hosts: dict[str, float] = {}
    dead: bool = False
    connects: list[str] = []

    def __init__(self, host, port=7709, timeout=10.0):
        self.host = host
        self._alive = True
        type(self).connects.append(host)

    @property
    def alive(self) -> bool:
        return self._alive

    async def connect(self) -> None:
        delay = type(self).slow_hosts.get(self.host)
        if type(self).dead:
            raise ConnectionRefusedError(f"{self.host} 拒绝连接")
        if delay is not None:
            await asyncio.sleep(delay)
            raise ConnectionRefusedError(f"{self.host} 握手超时")

    async def execute(self, cmd):
        return "ok"

    async def close(self) -> None:
        self._alive = False


@pytest.fixture
def conns(monkeypatch):
    _HostConn.slow_hosts = {}
    _HostConn.dead = False
    _HostConn.connects = []
    monkeypatch.setattr("pytdxdata.pool.pool.TdxConnection", _HostConn)
    return _HostConn


@pytest.mark.asyncio
async def test_slow_connect_does_not_freeze_other_borrowers(conns):
    """一台主机握手卡 0.5s 时，另一个协程借连接不能跟着等。

    老实现把 ``_new_conn`` 放在池锁里：所有 acquire/release 都被这 0.5s 挡住，
    几台坏节点叠加就是十几秒的全局停顿。
    """
    conns.slow_hosts = {"dead": 0.5}
    pool = DynamicPool(["dead", "live"], min_size=0, max_size=2,
                       per_server_cap=1, auto_probe=False)
    await pool.start()

    slow = asyncio.create_task(pool.acquire())      # 抢到 "dead"，开始卡握手
    await asyncio.sleep(0.05)                       # 确保它已进入 _new_conn

    t0 = time.monotonic()
    fast_pc = await pool.acquire()                  # 此时应立刻拿到 "live"
    elapsed = time.monotonic() - t0
    assert elapsed < 0.25, f"被坏节点的握手阻塞了 {elapsed:.2f}s"
    assert fast_pc.host == "live"

    slow_pc = await slow                            # 换到 "live" 后成功
    assert slow_pc.host == "live"
    for pc in (fast_pc, slow_pc):
        await pool.release(pc)
    await pool.close()


@pytest.mark.asyncio
async def test_acquire_gives_up_when_every_host_is_dead(conns):
    """全节点失联时必须抛 PoolTimeoutError，而不是无限重试。"""
    conns.dead = True
    pool = DynamicPool(["d1", "d2"], min_size=0, max_size=4,
                       per_server_cap=1, auto_probe=False)
    await pool.start()

    t0 = time.monotonic()
    with pytest.raises(PoolTimeoutError):
        # 外层 wait_for 只是防止老实现挂死把测试卡住
        await asyncio.wait_for(pool.acquire(timeout=0.3), timeout=5.0)
    assert time.monotonic() - t0 < 2.0
    await pool.close()


@pytest.mark.asyncio
async def test_blackout_stops_retrying_every_host_per_call(conns):
    """全灭后进入熔断期：后续 acquire 立刻失败，不再把每台主机握手一遍。

    没有熔断时，上层「按标的逐只重试」的循环会把 16 台 × 6000 只放大成
    9 万次握手（实测 6000 只跑 60s 都跑不完）。
    """
    conns.dead = True
    pool = DynamicPool(["d1", "d2", "d3"], min_size=0, max_size=8,
                       per_server_cap=1, auto_probe=False)
    await pool.start()
    tried = len(conns.connects)      # start() 的 min_size 预热（可能已试过 1 台）

    with pytest.raises(PoolTimeoutError):
        await asyncio.wait_for(pool.acquire(timeout=1.0), timeout=5.0)
    round1 = len(conns.connects) - tried
    assert round1 == 3, f"首轮应恰好试完 3 台，实际 {round1}"
    tried = len(conns.connects)

    # 熔断期内：不再产生任何建连尝试，且立即抛错
    t0 = time.monotonic()
    for _ in range(50):
        with pytest.raises(PoolTimeoutError):
            await pool.acquire()
    assert time.monotonic() - t0 < 0.2
    assert len(conns.connects) == tried, "熔断期内不该再握手"

    # 熔断到期后恢复探测
    pool._blackout_until = 0.0
    with pytest.raises(PoolTimeoutError):
        await pool.acquire()
    assert len(conns.connects) > tried
    await pool.close()


# ====================================================================== #
# 二、K 线批量：死节点只损失在途那一批，且不重复打扰
# ====================================================================== #

class _FleetConn:
    """MAC 通道替身：``m1`` 在成功服务 ``m1_life`` 次后「宕机」。"""

    m1_life = 2
    m1_calls = 0
    m2_calls = 0

    def __init__(self, host="m1", port=7709, timeout=10.0):
        self.host = host
        self._alive = True

    @property
    def alive(self) -> bool:
        return self._alive

    async def connect(self) -> None:
        pass

    async def close(self) -> None:
        self._alive = False

    def _dead(self) -> bool:
        cls = type(self)
        if self.host != "m1":
            cls.m2_calls += 1
            return False
        cls.m1_calls += 1
        return cls.m1_calls > cls.m1_life

    def _rows(self, cmd):
        if "MacKlineCmd" not in type(cmd).__name__:
            return []
        out = []
        for off in range(cmd.start, cmd.start + cmd.count):
            when = NEWEST - datetime.timedelta(minutes=off)
            out.append(SecurityBar(
                market=1, code="600000", open=10.0, high=10.1, low=9.9,
                close=10.0, vol=1000, amount=10000, year=when.year,
                month=when.month, day=when.day, hour=when.hour,
                minute=when.minute))
        return out

    async def execute(self, cmd):
        if self._dead():
            raise ConnectionError("m1 已宕机")
        return self._rows(cmd)

    async def execute_pipeline(self, cmds, **kwargs):
        if self._dead():
            raise ConnectionError("m1 已宕机")
        return [self._rows(c) for c in cmds]


def _syms(n: int) -> list[str]:
    return [f"sh60{i:04d}" for i in range(n)]


@pytest.mark.asyncio
async def test_bulk_bars_recovers_from_midrun_host_death(monkeypatch):
    """30 只K线，m1 服务 2 次后宕机：数据一根不少，且不反复打扰死节点。"""
    _FleetConn.m1_life = 2
    _FleetConn.m1_calls = 0
    _FleetConn.m2_calls = 0
    monkeypatch.setattr("pytdxdata.api.get_mac_hosts", lambda: ["m1", "m2"])
    monkeypatch.setattr("pytdxdata.pool.pool.TdxConnection",
                        lambda h, p=7709, t=3.0: _FleetConn(h))

    td = TdxData(cache_dir=None, pool_min=1, pool_max=4)
    await td.start()
    t0 = time.monotonic()
    try:
        bars = await td.get_bars(_syms(30), KlinePeriod.MIN_1, count=240)
    finally:
        elapsed = time.monotonic() - t0
        await td.close()

    assert len(bars) == 30 * 240, "死节点不能造成缺数据"
    # 死节点只该被「在途那一批」碰到，之后靠冷却隔离，不该被反复重试
    assert _FleetConn.m1_calls <= 2 * 4, f"死节点被打了 {_FleetConn.m1_calls} 次"
    assert _FleetConn.m2_calls > 0
    assert elapsed < 5.0, f"单台服务器宕机把整批拖到 {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_all_hosts_dead_fails_fast(monkeypatch):
    """全节点失联：**快速报错**，不能挂死，也不能每只都付满一次 30s 池超时。

    老实现里扩容失败分支 ``continue`` 且没有截止检查 → ``acquire`` 无限重试，
    十万次建连把事件循环饿死（协程连取消都投递不进去）。
    """
    _FleetConn.m1_life = 0
    _FleetConn.m1_calls = 0
    _FleetConn.m2_calls = 0
    monkeypatch.setattr("pytdxdata.api.get_mac_hosts", lambda: ["m1"])
    monkeypatch.setattr("pytdxdata.pool.pool.TdxConnection",
                        lambda h, p=7709, t=3.0: _FleetConn(h))

    td = TdxData(cache_dir=None, pool_min=1, pool_max=2)
    await td.start()
    t0 = time.monotonic()
    try:
        with pytest.raises(PoolTimeoutError):
            await asyncio.wait_for(
                td.get_bars(_syms(10), KlinePeriod.MIN_1, count=240), timeout=15.0)
    finally:
        elapsed = time.monotonic() - t0
        await td.close()
    assert elapsed < 5.0, f"全灭场景花了 {elapsed:.2f}s 才报错"
    # 每台主机只该试一次，不该疯狂重连
    assert _FleetConn.m1_calls < 40, f"重复建连 {_FleetConn.m1_calls} 次"


# ====================================================================== #
# 三、分页器的份额再分配
# ====================================================================== #

class _PageConn:
    def __init__(self, die_after: int | None = None, page_size: int = 100,
                 total: int = 600):
        self.die_after = die_after
        self.served = 0
        self.page_size = page_size
        self.total = total

    async def execute(self, cmd):
        start, count = cmd
        if self.die_after is not None and self.served >= self.die_after:
            raise ConnectionError("连接已断")
        self.served += 1
        return list(range(start, min(start + count, self.total)))


class _PagePool:
    def __init__(self, conns):
        self.conns = conns
        self._i = 0
        self.released = []

    async def acquire_many(self, n, *, timeout=None):
        return self.conns[:n]

    async def acquire(self, *, timeout=None):
        pc = self.conns[self._i % len(self.conns)]
        self._i += 1
        return pc

    async def release(self, pc, *, broken=False):
        self.released.append(broken)


@pytest.mark.asyncio
async def test_paginator_redistributes_pages_of_dead_worker():
    """1 号连接拉到第 2 页就断：它剩下的页要由其它连接补完，不能整份丢掉。"""
    conns = [_PageConn(die_after=2), _PageConn(), _PageConn()]
    pag = Paginator(_PagePool(conns), concurrency=3)  # type: ignore[arg-type]
    spec = PageSpec(page_size=100, total=600)          # 6 页，每 worker 2 页
    got = await pag.fetch_all(lambda s, c: (s, c), spec)
    assert len(got) == 600, f"丢了 {600 - len(got)} 条"


@pytest.mark.asyncio
async def test_paginator_still_distributes_work_across_conns():
    """份额再分配不能退化成「一条连接干完所有页」。"""
    conns = [_PageConn() for _ in range(3)]
    pag = Paginator(_PagePool(conns), concurrency=3)  # type: ignore[arg-type]
    spec = PageSpec(page_size=100, total=600)
    got = await pag.fetch_all(lambda s, c: (s, c), spec)
    assert len(got) == 600
    assert all(c.served >= 1 for c in conns), "每台连接都该分到活"
