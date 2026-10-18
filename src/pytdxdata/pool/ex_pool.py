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
        async with self._cond:
            while True:
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

                # 2. 可扩容 → 锁内预留槽位，锁外建连
                new_conn = None
                new_host = None
                if self._total < self.max_size and self.servers:
                    host = self._next_available_server()
                    if host is not None:
                        self._total += 1
                        self._server_count[host] = self._server_count.get(host, 0) + 1
                        new_conn = ExTdxConnection(host, 7727, self.timeout, self.handshake_map.get(host, self.handshake))
                        new_host = host
                if new_conn is not None:
                    # 锁外网络 IO：连接失败回滚槽位后继续等待
                    try:
                        await new_conn.connect()
                        return new_conn
                    except Exception as e:
                        log.warning("EX扩容失败 %s: %s", new_host, str(e)[:60])
                        async with self._cond:
                            self._total = max(0, self._total - 1)
                            self._server_count[new_host] = max(0, self._server_count.get(new_host, 0) - 1)

                # 3. 池满：排队等待 release 唤醒（或超时）
                now = time.monotonic()
                if deadline is not None and now >= deadline:
                    raise ExPoolTimeoutError(self, timeout)
                try:
                    if deadline is None:
                        await self._cond.wait()
                    else:
                        await asyncio.wait_for(self._cond.wait(), timeout=deadline - now)
                except asyncio.TimeoutError:
                    raise ExPoolTimeoutError(self, timeout)

    def _next_available_server(self) -> str | None:
        for h in self.servers:
            if self._server_count.get(h, 0) < self.per_server_cap:
                return h
        return None

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
