"""EX扩展市场连接池（简化版：固定连接数，借还+坏连接重建+login自动）"""
from __future__ import annotations

import asyncio
import logging
import time

from .ex_connection import ExTdxConnection

log = logging.getLogger(__name__)


class ExPool:
    """EX通道连接池（港股/期货，端口7727，连接时自动login）。"""

    def __init__(self, servers: list[str], *, timeout: float = 10.0,
                 min_size: int = 2, max_size: int = 6,
                 per_server_cap: int = 1,
                 handshake: str = "login",
                 handshake_map: dict | None = None) -> None:
        self.servers = servers
        self.timeout = timeout
        self.handshake = handshake
        self.handshake_map = handshake_map or {}
        self.min_size = min_size
        self.max_size = max_size
        self.per_server_cap = max(1, per_server_cap)  # 一台服务器最多几个连接（默认1=单连接）
        self._idle: list[tuple[str, ExTdxConnection]] = []
        self._server_count: dict[str, int] = {}
        self._total = 0
        self._closed = False
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        async with self._lock:
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
        async with self._lock:
            for _, conn in self._idle:
                try:
                    await conn.close()
                except Exception:
                    pass
            self._idle.clear()
            self._total = 0
            self._server_count.clear()

    async def acquire(self) -> ExTdxConnection:
        for _ in range(3):
            stale_conn = None
            new_conn = None
            new_host = None
            async with self._lock:
                if self._idle:
                    _, conn = self._idle.pop()
                    fresh = time.monotonic() - conn.last_used < 30.0
                    if conn.alive and fresh:
                        return conn
                    # 连接空闲过久（login会话可能失效）→ 丢弃重连
                    self._total = max(0, self._total - 1)
                    self._server_count[conn.host] = max(0, self._server_count.get(conn.host, 0) - 1)
                    stale_conn = conn
                # 扩容：在锁内预留槽位，锁外执行网络IO
                elif self._total < self.max_size and self.servers:
                    host = self._next_available_server()
                    if host:
                        self._total += 1
                        self._server_count[host] = self._server_count.get(host, 0) + 1
                        new_conn = ExTdxConnection(host, 7727, self.timeout, self.handshake_map.get(host, self.handshake))
                        new_host = host
            # 锁外：关闭过期连接、建立新连接
            if stale_conn is not None:
                try:
                    await stale_conn.close()
                except Exception:
                    pass
            if new_conn is not None:
                try:
                    await new_conn.connect()
                    return new_conn
                except Exception as e:
                    log.warning("EX扩容失败 %s: %s", new_host, str(e)[:60])
                    # 连接失败，回滚预留槽位
                    async with self._lock:
                        self._total = max(0, self._total - 1)
                        self._server_count[new_host] = max(0, self._server_count.get(new_host, 0) - 1)
            await asyncio.sleep(0.05)
        raise ConnectionError("EX连接池无可用连接")

    def _next_available_server(self) -> str | None:
        for h in self.servers:
            if self._server_count.get(h, 0) < self.per_server_cap:
                return h
        return None

    async def release(self, conn: ExTdxConnection, *, broken: bool = False) -> None:
        if broken or not conn.alive:
            async with self._lock:
                self._total = max(0, self._total - 1)
                self._server_count[conn.host] = max(0, self._server_count.get(conn.host, 0) - 1)
            try:
                await conn.close()
            except Exception:
                pass
            return
        async with self._lock:
            self._idle.append((conn.host, conn))
