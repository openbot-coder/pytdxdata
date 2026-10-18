"""ExPool 背压/并发语义回归（Issue #1：并发超限 0.15s 秒失败）。

用 mock 连接模拟：单服务器 + per_server_cap=1 时，
- 并发 acquire 超过有效并发 → 排队等待而不是快速失败；
- release 唤醒等待者；
- acquire 超时异常带池状态；
- ex_pool_cap 提升同主机并发。
"""
from __future__ import annotations

import asyncio
import time

import pytest

from pytdxdata.pool.ex_pool import ExPool, ExPoolTimeoutError


class MockConn:
    """Minimal stand-in for ExTdxConnection."""

    last_created = 0

    def __init__(self, host: str, port: int, timeout: float, handshake: str = "login"):
        self.host = host
        self.port = port
        self.alive = True
        self.last_used = time.monotonic()
        self.closed = False
        MockConn.last_created += 1

    async def connect(self) -> None:
        await asyncio.sleep(0)  # instant "connect"

    async def close(self) -> None:
        self.closed = True
        self.alive = False


@pytest.fixture
def patch_conn(monkeypatch):
    monkeypatch.setattr("pytdxdata.pool.ex_pool.ExTdxConnection", MockConn)
    MockConn.last_created = 0


async def test_acquire_queues_instead_of_fast_fail(patch_conn):
    """并发 > 有效并发时，后续 acquire 等待 release，而非 0.15s 抛错。"""
    pool = ExPool(["e1"], min_size=1, max_size=6, per_server_cap=1)
    await pool.start()
    c1 = await pool.acquire()          # 唯一连接被占用
    t0 = time.monotonic()

    async def holder():
        await asyncio.sleep(0.5)       # 持有 0.5s 后归还
        await pool.release(c1)

    asyncio.create_task(holder())
    c2 = await pool.acquire()          # 应在 ~0.5s 后拿到，而不是 0.15s 抛错
    waited = time.monotonic() - t0
    assert waited >= 0.4, f"acquire 没有等待释放（仅 {waited:.2f}s）"
    assert c2 is c1  # 归还的是同一条连接


async def test_acquire_timeout_carries_pool_state(patch_conn):
    """超时异常信息带池状态（idle/total/servers/cap）。"""
    pool = ExPool(["e1"], min_size=1, max_size=6, per_server_cap=1, acquire_timeout=0.3)
    await pool.start()
    c1 = await pool.acquire()
    with pytest.raises(ExPoolTimeoutError) as ei:
        await pool.acquire()
    msg = str(ei.value)
    assert "total=1" in msg and "per_server_cap=1" in msg and "servers=1" in msg
    await pool.release(c1)


async def test_ex_pool_cap_allows_parallel_conns_same_host(patch_conn):
    """per_server_cap=2 时单主机可并发持有 2 条连接。"""
    pool = ExPool(["e1"], min_size=1, max_size=6, per_server_cap=2)
    await pool.start()
    got = await asyncio.gather(pool.acquire(), pool.acquire())
    assert len(got) == 2 and got[0] is not got[1]
    assert MockConn.last_created == 2
    for c in got:
        await pool.release(c)


async def test_concurrent_acquire_all_succeed(patch_conn):
    """Issue 场景：单服务器 + cap=2，6 个并发任务应全部拿到连接（排队）而非部分失败。"""
    pool = ExPool(["e1"], min_size=2, max_size=6, per_server_cap=2)
    await pool.start()

    async def task(i):
        c = await pool.acquire()
        await asyncio.sleep(0.1)       # 模拟一次 1m 分页拉取
        await pool.release(c)
        return i

    results = await asyncio.gather(*[task(i) for i in range(6)])
    assert sorted(results) == list(range(6))  # 全部成功，零失败


async def test_release_notifies_waiters(patch_conn):
    """broken 归还（连接死亡）也要唤醒等待者（槽位回收后可扩容）。"""
    pool = ExPool(["e1"], min_size=1, max_size=1, per_server_cap=1)
    await pool.start()
    c1 = await pool.acquire()
    c1.alive = False

    async def broken_release():
        await asyncio.sleep(0.2)
        await pool.release(c1, broken=True)

    asyncio.create_task(broken_release())
    c2 = await pool.acquire()          # 槽位被回收→扩容新建
    assert c2 is not c1
    await pool.release(c2)
