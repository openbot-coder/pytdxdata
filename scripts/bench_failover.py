"""坏节点隔离压测：N 台服务器，其中几台**跑到一半突然死掉**。

关注两个指标：
1. **总耗时** —— 死节点不能把整批任务拖长（老实现会因为持锁建连 + 串行补
   重试把长尾放大成倍）。
2. **数据完整度** —— 死节点不能造成缺数据。

用法::

    python scripts/bench_failover.py --syms 3000 --hosts 16 --dead 5,9

输出一行 JSON，便于对比改前/改后。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import pytdxdata.api as api
import pytdxdata.pool.pool as poolmod
from pytdxdata import TdxData
from pytdxdata.models import KlinePeriod, SecurityBar

NOW = time.monotonic()
PIPE_LATENCY = 0.012      # 一次流水线往返（模拟公网 12ms）
CONNECT_LATENCY = 0.05    # 正常主机 TCP 握手 + 握手命令
DEAD_HANG = 0.8           # 死节点：请求发出去没响应，最终超时
CONNECT_HANG = 0.0        # 黑洞握手：连上了但握手永不完（0=直接拒绝）

DEAD_AT = 0.0             # 死节点从第几秒开始不响应


class FakeConn:
    dead: set[int] = set()
    calls = 0
    pipes = 0
    dead_pipe_hits = 0     # 打到死节点的流水线次数（含"半开等待"）

    def __init__(self, host, port=7709, timeout=3.0):
        self.host = host
        self.idx = int(host[1:])
        self._alive = True

    @property
    def alive(self) -> bool:
        return self._alive

    async def connect(self) -> None:
        if self._is_dead() and CONNECT_HANG:
            # 黑洞节点：TCP 连上但握手包永远不回 —— 真实宕机最常见的样子
            await asyncio.sleep(CONNECT_HANG)
            raise TimeoutError(f"{self.host} 握手超时")
        await asyncio.sleep(CONNECT_LATENCY)
        if self._is_dead():
            raise ConnectionRefusedError(f"{self.host} 已宕机")

    def _is_dead(self) -> bool:
        return self.idx in type(self).dead and time.monotonic() - NOW >= DEAD_AT

    async def execute(self, cmd):
        await asyncio.sleep(PIPE_LATENCY)
        if self._is_dead():
            raise ConnectionError(f"{self.host} 无响应")
        type(self).calls += 1
        return self._rows(cmd)

    async def execute_pipeline(self, cmds, **kwargs):
        await asyncio.sleep(PIPE_LATENCY)
        if self._is_dead():
            type(self).dead_pipe_hits += 1
            await asyncio.sleep(DEAD_HANG)      # 半开连接：等到超时才报错
            raise ConnectionError(f"{self.host} 无响应")
        type(self).pipes += 1
        return [self._rows(c) for c in cmds]

    async def close(self) -> None:
        self._alive = False

    @staticmethod
    def _rows(cmd):
        if "MacKlineCmd" not in type(cmd).__name__:
            return []
        bar = SecurityBar(
            market=1, code="600000", open=10.0, high=10.1, low=9.9, close=10.0,
            vol=1000, amount=10000, year=2026, month=1, day=1, hour=9, minute=30)
        return [bar] * int(getattr(cmd, "count", 1))   # 同一个对象复用，便于控内存


async def run(n_syms: int, n_hosts: int, dead: set[int]) -> dict:
    global NOW
    NOW = time.monotonic()
    FakeConn.dead = dead
    FakeConn.calls = 0
    FakeConn.pipes = 0
    FakeConn.dead_pipe_hits = 0
    hosts = [f"h{i}" for i in range(n_hosts)]
    api.get_mac_hosts = lambda: hosts
    api.get_standard_hosts = lambda: hosts      # 别让标准池去碰真实公网主机
    api.get_ex_hosts = lambda: []
    poolmod.schedule_probe = lambda: None       # 关掉后台全量测速
    poolmod.TdxConnection = FakeConn    # 池内部按名字取，必须在建池之前替身
    # cap=1：一台主机一条连接，坏一台就少一条链路（最坏情况）
    td = TdxData(cache_dir=None, pool_min=4, pool_max=24, mac_pool_cap=1)
    await td.start()
    syms = [f"sh60{i:04d}" for i in range(n_syms)]
    t0 = time.monotonic()
    try:
        bars = await td.get_bars(syms, KlinePeriod.MIN_1, count=240)
    finally:
        elapsed = time.monotonic() - t0
        await td.close()
    return {
        "syms": n_syms,
        "hosts": n_hosts,
        "dead": sorted(dead),
        "elapsed_s": round(elapsed, 3),
        "bars": len(bars),
        "expect_bars": n_syms * 240,
        "complete": len(bars) == n_syms * 240,
        "pipeline_calls": FakeConn.pipes,
        "dead_pipe_hits": FakeConn.dead_pipe_hits,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--syms", type=int, default=3000)
    ap.add_argument("--hosts", type=int, default=16)
    ap.add_argument("--dead", default="5,9",
                    help="中途死掉的主机下标，逗号分隔；空串=全健康")
    ap.add_argument("--die-at", type=float, default=0.25,
                    help="开跑后第几秒这些主机开始不响应（0=一开始就死）")
    ap.add_argument("--connect-hang", type=float, default=0.0,
                    help="死节点的握手卡顿秒数（>0 模拟黑洞节点，0=直接拒绝）")
    args = ap.parse_args()
    dead = {int(x) for x in args.dead.split(",") if x.strip()}
    global DEAD_AT, CONNECT_HANG
    DEAD_AT = args.die_at
    CONNECT_HANG = args.connect_hang
    print(json.dumps(asyncio.run(run(args.syms, args.hosts, dead)),
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
