"""EX扩展市场连接池（固定连接数，借还+坏连接重建+login自动）。

与标准池 pool.py 的背压语义对齐：acquire 在无空闲连接且无法扩容时，
基于 asyncio.Condition 排队等待 release 唤醒（默认 acquire_timeout=30s），
而不是快速失败。per_server_cap 限制单台服务器连接数；单服务器场景下
有效并发 = servers × per_server_cap（默认 1），通过 TdxData(ex_pool_cap=N)
可对同一 EX 主机开多连接。
"""
from __future__ import annotations

import asyncio
import logging
import time

from .ex_connection import ExTdxConnection

log = logging.getLogger(__name__)

DEFAULT_ACQUIRE_TIMEOUT = 30.0


class ExPoolTimeoutError(ConnectionError):
    """acquire 等待超时（携带池状态快照，便于排障）。"""

    def __init__(self, pool: "ExPool", timeout: float) -> None:
        idle = len(pool._idle)
        total = pool._total
        servers = len(pool.servers)
        cap = pool.per_server_cap
        super().__init__(
            f"EX连接池获取超时（等了 {timeout:.1f}s）："
            f"idle={idle} total={total} servers={servers} per_server_cap={cap} "
            f"有效并发={servers * cap}"
        )


class ExPool:
    """EX通道连接池（港股/期货，端口7727，连接时自动login）。"""

    FAIL_COOLDOWN = 30.0
    """握手失败后拉黑该主机的秒数（EX 节点少，冷却比标准池短）。"""

    def __init__(self, servers: list[str], *, timeout: float = 10.0,
                 min_size: int = 2, max_size: int = 6,
                 per_server_cap: int = 1,
                 acquire_timeout: float | None = None,
                 handshake: str = "login",
                 handshake_map: dict | None = None) -> None:
        self.servers = servers
        self.timeout = timeout
        self.handshake = handshake
        self.handshake_map = handshake_map or {}
        self.min_size = min_size
        self.max_size = max_size
        self.per_server_cap = max(1, per_server_cap)  # 一台服务器最多几个连接（默认1=单连接）
        self.acquire_timeout = (
            DEFAULT_ACQUIRE_TIMEOUT if acquire_timeout is None else acquire_timeout
        )
        self._idle: list[tuple[str, ExTdxConnection]] = []
        self._server_count: dict[str, int] = {}
        self._total = 0
        self._closed = False
        self._cond = asyncio.Condition()
        # 死节点短期拉黑：一台 EX 主机握手失败后，后续 acquire 直接绕开它，
        # 而不是每次都再付一遍 ``self.timeout`` 的握手超时。
        self._fail_until: dict[str, float] = {}

    async def start(self) -> None:
        async with self._cond:
            for host in self.servers[: self.min_size]:
                if self._server_count.get(host, 0) >= self.per_server_cap:
                    continue
                try:
                    conn = ExTdxConnection(host, 7727, self.timeout, self.handshake_map.get(host, self.handshake))
                    await conn.connect()
                    self._idle.append((host, conn))
                    self._total += 1
                    self._server_count[host] = self._server_count.get(host, 0) + 1
                except Exception as e:
                    log.warning("EX连接失败 %s: %s", host, str(e)[:60])
                    self._fail_until[host] = time.monotonic() + self.FAIL_COOLDOWN

    async def close(self) -> None:
        self._closed = True
        async with self._cond:
            for _, conn in self._idle:
                try:
                    await conn.close()
                except Exception:
                    pass
            self._idle.clear()
            self._total = 0
            self._server_count.clear()
            self._cond.notify_all()

    async def acquire(self, timeout: float | None = None) -> ExTdxConnection:
        """借出一条连接；无空闲且无法扩容时排队等待 release 唤醒。

        timeout 默认取 self.acquire_timeout（30s）；传 None 表示无限等待。
        """
        if timeout is None:
            timeout = self.acquire_timeout
        deadline = None if timeout is None else time.monotonic() + timeout
        avoid: set[str] = set()   # 本次 acquire 里已经握手失败、别再试的主机
        while True:
            new_conn = None
            new_host = None
            async with self._cond:
                # 1. 优先借用空闲连接（过期则丢弃，槽位在锁内回收）
                if self._idle:
                    _, conn = self._idle.pop()
                    fresh = time.monotonic() - conn.last_used < 30.0
                    if conn.alive and fresh:
                        return conn
                    # 连接空闲过久（login会话可能失效）→ 丢弃，槽位让给扩容
                    self._total = max(0, self._total - 1)
                    self._server_count[conn.host] = max(0, self._server_count.get(conn.host, 0) - 1)
                    self._schedule_close(conn)

                # 2. 可扩容 → 锁内预留槽位，锁外建连（不能持锁做网络 IO）
                if self._total < self.max_size and self.servers:
                    host = self._next_available_server(avoid)
                    if host is not None:
                        self._total += 1
                        self._server_count[host] = self._server_count.get(host, 0) + 1
                        new_conn = ExTdxConnection(
                            host, 7727, self.timeout,
                            self.handshake_map.get(host, self.handshake))
                        new_host = host
                    elif avoid and all(h in avoid for h in self.servers):
                        # 所有主机本次都试过了、全失败：等下去也不会有结果
                        raise ExPoolTimeoutError(self, timeout)

                # 3. 没抢到槽位 → 在锁内排队等 release 唤醒（waiter 必须持锁）
                if new_conn is None:
                    now = time.monotonic()
                    if deadline is not None and now >= deadline:
                        raise ExPoolTimeoutError(self, timeout)
                    try:
                        if deadline is None:
                            await self._cond.wait()
                        else:
                            await asyncio.wait_for(self._cond.wait(), timeout=deadline - now)
                    except asyncio.TimeoutError:
                        raise ExPoolTimeoutError(self, timeout) from None
                    continue

            # ---- 锁外建连 ----
            try:
                await new_conn.connect()
                async with self._cond:
                    self._fail_until.pop(new_host, None)
                return new_conn
            except Exception as e:
                log.warning("EX扩容失败 %s: %s", new_host, str(e)[:60])
                avoid.add(new_host)   # type: ignore[arg-type]
                async with self._cond:
                    self._total = max(0, self._total - 1)
                    self._server_count[new_host] = max(0, self._server_count.get(new_host, 0) - 1)
                    self._fail_until[new_host] = time.monotonic() + self.FAIL_COOLDOWN
                    self._cond.notify_all()
                continue

    def _next_available_server(self, avoid: set[str] | None = None) -> str | None:
        """挑一台**未达并发上限**的主机：优先未冷却的，冷却中的只在别无选择时用。"""
        avoid = avoid or set()
        fallback: str | None = None
        now = time.monotonic()
        for h in self.servers:
            if h in avoid:
                continue
            if self._server_count.get(h, 0) >= self.per_server_cap:
                continue
            if now >= self._fail_until.get(h, 0.0):
                return h
            if fallback is None:
                fallback = h
        return fallback

    def _schedule_close(self, conn: ExTdxConnection) -> None:
        async def _close():
            try:
                await conn.close()
            except Exception:
                pass
        try:
            asyncio.get_running_loop().create_task(_close())
        except RuntimeError:
            pass

    async def release(self, conn: ExTdxConnection, *, broken: bool = False) -> None:
        async with self._cond:
            if broken or not conn.alive:
                self._total = max(0, self._total - 1)
                self._server_count[conn.host] = max(0, self._server_count.get(conn.host, 0) - 1)
                self._schedule_close(conn)
            else:
                self._idle.append((conn.host, conn))
            self._cond.notify_all()
