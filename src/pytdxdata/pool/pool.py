"""异步动态连接池（标准 / MAC 通道 7709）。

设计目标：
- 高并发：池内连接数 = 服务器数 × per_server_cap，多服务器并行请求。
- 自愈：坏连接自动丢弃重建；服务器故障时自动切换到下一个可用服务器。
- 动态：支持运行时 add() 新服务器；支持后台全量测速入库（当日首次启动）。
- 单连接约束（核心需求）：per_server_cap 默认 1 —— 一台服务器只允许建立 1 个连接，
  不允许多开，避免对单一券商服务器造成并发压力被限流/封禁。
- 后台测速（核心需求）：当日首次启动时，在后台对全量候选服务器测速，按
  「排名前 FAST_POOL_TOP_N 或 延迟 < FAST_POOL_MAX_LATENCY」筛选入库，结果写回 servers.json。
- 弹性：空闲连接超时后由 reaper 缩容回 min_size；支持 stats 统计。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from .connection import TdxConnection
from .health import HealthEngine
from .probe_cache import get_probe_cache

log = logging.getLogger(__name__)

DEFAULT_PORT = 7709


class PoolTimeoutError(Exception):
    """连接池在限定重试内无法获取可用连接。"""


class _Stats:
    def __init__(self) -> None:
        self.borrowed = 0
        self.total = 0
        self.created = 0
        self.dropped = 0
        self.reaped = 0


class PooledConnection:
    """池化连接包装：记录所属 host，转发 execute / close。

    server 是 host 的别名（兼容历史调用）。
    """

    __slots__ = ("conn", "host", "server", "last_used", "alive")

    def __init__(self, conn, host: str) -> None:
        self.conn = conn
        self.host = host
        self.server = host  # 别名
        self.last_used = time.monotonic()
        self.alive = True

    async def execute(self, cmd):
        self.last_used = time.monotonic()
        return await self.conn.execute(cmd)

    async def close(self) -> None:
        self.alive = False
        try:
            await self.conn.close()
        except Exception:
            pass


class DynamicPool:
    """动态连接池：按服务器维度统计连接数，每台服务器最多 per_server_cap 个连接。"""

    def __init__(self, servers: list[str], *, port: int = DEFAULT_PORT,
                 timeout: float = 10.0, min_size: int = 4, max_size: int = 12,
                 per_server_cap: int = 1,
                 health: HealthEngine | None = None,
                 probe_factory=None,
                 idle_ttl: float = 30.0,
                 scale_down_grace: float = 5.0,
                 reaper_interval: float = 5.0,
                 auto_probe: bool = True) -> None:
        self.servers = list(dict.fromkeys(servers))  # 去重保序
        self.port = port
        self.timeout = timeout
        self.min_size = max(1, min_size)
        self.max_size = max(self.min_size, max_size)
        self.per_server_cap = max(1, per_server_cap)  # 一台服务器最多几个连接（默认1=单连接）
        self.health = health or HealthEngine()
        self.probe_factory = probe_factory  # MAC通道用真实命令探测可用性
        self.idle_ttl = idle_ttl
        self.scale_down_grace = scale_down_grace
        self.reaper_interval = reaper_interval
        self.auto_probe = auto_probe

        self.stats = _Stats()
        self._idle: list[PooledConnection] = []
        self._conns: set[PooledConnection] = set()
        self._server_count: dict[str, int] = {}  # 每服务器已建立连接数
        self._total = 0
        self._closed = False
        self._started = False
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self._reaper_task: asyncio.Task | None = None

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if self._started:
            return
        self._started = True

        # 需求①：当日首次启动 → 后台全量测速入库（不阻塞主流程）
        if self.auto_probe:
            hosts = list(self.servers)
            asyncio.create_task(
                get_probe_cache().ensure_probe(hosts),
                name="pytdxdata-daily-probe",
            )

        targets = self._pick_servers(self.min_size)
        for host in targets:
            if self._total >= self.max_size:
                break
            try:
                pc = await self._new_conn(host)
                self._idle.append(pc)
                self._total += 1
            except Exception as e:
                log.warning("初始连接失败 %s: %s", host, str(e)[:60])

        if self._total == 0:
            log.warning("连接池初始连接全部失败，将在使用时按需重试")

        # 启动 reaper：空闲连接超时后缩容回 min_size
        try:
            self._reaper_task = asyncio.create_task(self._reaper_loop())
        except RuntimeError:
            pass  # 无事件循环时跳过

    def _pick_servers(self, n: int) -> list[str]:
        ranked = self.health.rank(self.servers)
        out: list[str] = []
        counts = dict(self._server_count)
        for h in ranked:
            if len(out) >= n:
                break
            if counts.get(h, 0) >= self.per_server_cap:
                continue
            out.append(h)
            counts[h] = counts.get(h, 0) + 1
        if len(out) < n:  # 兜底：服务器不足时允许突破 cap
            for h in ranked:
                if len(out) >= n:
                    break
                out.append(h)
        return out

    async def _new_conn(self, host: str) -> PooledConnection:
        conn = TdxConnection(host, self.port, self.timeout)
        await conn.connect()
        if self.probe_factory is not None:
            try:
                await conn.execute(self.probe_factory())
            except Exception:
                await conn.close()
                raise
        pc = PooledConnection(conn, host)
        self._conns.add(pc)
        self._server_count[host] = self._server_count.get(host, 0) + 1
        self.stats.created += 1
        self.stats.total = self._total + 1
        return pc

    async def close(self) -> None:
        self._closed = True
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
        async with self._lock:
            for pc in list(self._conns):
                try:
                    await pc.close()
                except Exception:
                    pass
            self._idle.clear()
            self._conns.clear()
            self._total = 0
            self._server_count.clear()
            self.stats.total = 0

    # ---------- 借还 ----------
    async def acquire(self, timeout: float | None = 30.0) -> PooledConnection:
        """借出一条连接。

        单连接约束下，池大小 = 服务器数 × per_server_cap。当并发请求超过池大小时，
        超额协程在此**排队等待**（基于 Condition），待其他协程 release 后被唤醒，
        而非立即超时放弃。默认 timeout=30s 防止在坏服务器场景下永久挂起。
        """
        deadline = None if timeout is None else (time.monotonic() + timeout)
        async with self._cond:
            while True:
                now = time.monotonic()
                # 1. 优先借用空闲连接（跳过冷却中的服务器，避免反复选中坏节点）
                while self._idle:
                    pc = self._idle.pop(0)
                    fresh = now - pc.last_used < self.idle_ttl
                    if pc.alive and fresh and not self.health.in_cooldown(pc.host):
                        self.stats.borrowed += 1
                        return pc
                    self._drop_locked(pc)

                # 2. 可扩容（未达上限且有可用服务器）→ 新建连接
                if self._total < self.max_size:
                    host = self._next_available_server()
                    if host is None:
                        # 兜底：全部服务器都在冷却中，而池子未满 —— 此时等 release
                        # 永远等不到（没人持有），只能挑最健康的一台顶着冷却重试
                        host = self._next_available_server(ignore_cooldown=True)
                    if host:
                        try:
                            pc = await self._new_conn(host)
                            self._total += 1
                            self.stats.borrowed += 1
                            return pc
                        except Exception as e:
                            log.warning("扩容连接失败 %s: %s", host, str(e)[:60])
                            self.health.record_failure(host)
                            # 循环重试：可能换下一台服务器
                            continue

                # 3. 池满：排队等待 release 唤醒（或超时）
                if deadline is not None and now >= deadline:
                    raise PoolTimeoutError("连接池获取超时（连接数已达上限且无空闲）")
                try:
                    if deadline is None:
                        await self._cond.wait()
                    else:
                        await asyncio.wait_for(self._cond.wait(), timeout=deadline - now)
                except asyncio.TimeoutError:
                    raise PoolTimeoutError("连接池获取超时（连接数已达上限且无空闲）")

    async def acquire_many(self, k: int) -> list[PooledConnection]:
        """批量借出 k 个连接（分页并发用），超额请求排队等待不丢。"""
        out: list[PooledConnection] = []
        for _ in range(k):
            try:
                out.append(await self.acquire())
            except PoolTimeoutError:
                break
        return out

    def _next_available_server(self, *, ignore_cooldown: bool = False) -> str | None:
        ranked = self.health.rank(self.servers)
        # 1. 优先选未冷却且未达并发上限的服务器
        for h in ranked:
            if self.health.in_cooldown(h) and not ignore_cooldown:
                continue
            if self._server_count.get(h, 0) < self.per_server_cap:
                return h
        # 2. 兜底：仅从「未冷却」的已占用服务器里随机复用（避免反复打坏服务器）
        alive = [
            h for h in ranked
            if (ignore_cooldown or not self.health.in_cooldown(h))
            and self._server_count.get(h, 0) > 0
        ]
        return random.choice(alive) if alive else None

    async def release(self, pc: PooledConnection, *, broken: bool = False) -> None:
        if self.stats.borrowed > 0:
            self.stats.borrowed -= 1
        # self._cond 的底层锁即 self._lock，统一用 _cond 加锁避免重复加锁死锁
        async with self._cond:
            if broken or not pc.alive:
                self.health.record_failure(pc.host)
                self._drop_locked(pc)
            else:
                self.health.record_success(pc.host)
                if not self._closed and pc.alive:
                    self._idle.append(pc)
            self._cond.notify_all()

    def _drop_locked(self, pc: PooledConnection) -> None:
        self._conns.discard(pc)
        self._total = max(0, self._total - 1)
        self.stats.total = self._total
        if self._server_count.get(pc.host, 0) > 0:
            self._server_count[pc.host] -= 1
        self._idle = [c for c in self._idle if c is not pc]
        self.stats.dropped += 1
        asyncio.create_task(self._safe_close(pc))

    @staticmethod
    async def _safe_close(pc: PooledConnection) -> None:
        try:
            await pc.close()
        except Exception:
            pass

    # ---------- reaper（空闲缩容） ----------
    async def _reaper_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(self.reaper_interval)
                async with self._lock:
                    now = time.monotonic()
                    kept = []
                    while self._idle:
                        pc = self._idle.pop(0)
                        if (now - pc.last_used > self.idle_ttl
                                and self._total > self.min_size):
                            self._drop_locked(pc)
                            self.stats.reaped += 1
                        else:
                            kept.append(pc)
                    self._idle = kept
        except asyncio.CancelledError:
            pass

    # ---------- 动态管理 ----------
    def add(self, host: str) -> None:
        if host not in self.servers:
            self.servers.append(host)

    def remove(self, host: str) -> None:
        if host in self.servers:
            self.servers.remove(host)

    @property
    def size(self) -> int:
        return self._total

    @property
    def idle_count(self) -> int:
        return len(self._idle)
