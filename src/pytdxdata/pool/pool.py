"""异步动态连接池（标准 / MAC 通道 7709）。

设计目标：
- 高并发：池内连接数 = 服务器数 × per_server_cap，多服务器并行请求。
- 自愈：坏连接自动丢弃重建；服务器故障时自动切换到下一个可用服务器。
- 动态：支持运行时 add() 新服务器；支持后台全量测速入库（当日首次启动）。
- 单连接约束（核心需求）：per_server_cap 默认 1 —— 一台服务器只允许建立 1 个连接，
  不允许多开，避免对单一券商服务器造成并发压力被限流/封禁。
- 后台测速（核心需求）：当日首次启动时，在后台对**全量候选**服务器测速（覆盖标准/MAC/EX
  三个协议，而不是只测当前池内节点），结果缓存到用户级缓存目录；当天已有缓存则跳过、不再刷新。
  全程不阻塞 start()、不打印、不抛错（对用户无感）。详见 pool/probe_cache.py。
- 弹性：空闲连接超时后由 reaper 缩容回 min_size；支持 stats 统计。
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from .connection import TdxConnection
from .health import HealthEngine
from .probe_cache import schedule_probe

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

    async def execute_pipeline(self, cmds, *, timeout: float | None = None):
        """在同一条连接上流水线执行多条命令（见 TdxConnection.execute_pipeline）。"""
        self.last_used = time.monotonic()
        return await self.conn.execute_pipeline(cmds, timeout=timeout)

    async def close(self) -> None:
        self.alive = False
        try:
            await self.conn.close()
        except Exception:
            pass


class DynamicPool:
    """动态连接池：按服务器维度统计连接数，每台服务器最多 per_server_cap 个连接。"""

    BLACKOUT_COOLDOWN = 2.0
    """全主机失败后的熔断时长（秒）。

    全部服务器同时不可用时，每次 :meth:`acquire` 都会把每台主机握手一遍再放弃。
    上层是「按标的逐只重试」的循环，6000 只标的 × 16 台主机 × 握手耗时 = 几分钟
    的空转。熔断期内直接抛 :class:`PoolTimeoutError`，让调用方快速走完并返回，
    冷却结束再试一次探测全灭是否恢复。
    """

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
        self._blackout_until = 0.0   # 全灭熔断截止时刻（monotonic）
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self._reaper_task: asyncio.Task | None = None
        self._probe_task: asyncio.Task | None = None

    # ---------- 生命周期 ----------
    async def start(self) -> None:
        if self._started:
            return
        self._started = True

        # 需求①：当日首次启动 → 后台全量测速（不阻塞主流程；当天已有缓存则自动跳过）
        # 候选清单由 probe_cache 自行枚举全量主机（本池的 servers 只是其中一个子集，
        # 不能直接当作测速输入，否则只会重测已在池内的少数节点、其余记录永久过期）。
        if self.auto_probe:
            self._probe_task = schedule_probe()

        targets = self._pick_servers(self.min_size)
        for host in targets:
            if self._total >= self.max_size:
                break
            try:
                pc = await self._new_conn(host)
            except Exception as e:
                log.warning("初始连接失败 %s: %s", host, str(e)[:60])
                continue
            async with self._lock:
                self._register_locked(pc)
                self._idle.append(pc)

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
        """建立一条连接（**纯 IO，不改任何计数**）。

        计数由调用方在锁内用 :meth:`_register_locked` / :meth:`_reserve_locked`
        单独完成——建连必须能在池锁之外进行，否则一台坏服务器的握手超时
        会把所有并发协程一起冻住。
        """
        conn = TdxConnection(host, self.port, self.timeout)
        await conn.connect()
        if self.probe_factory is not None:
            try:
                await conn.execute(self.probe_factory())
            except Exception:
                await conn.close()
                raise
        return PooledConnection(conn, host)

    def _register_locked(self, pc: PooledConnection) -> None:
        """登记一条已建好的连接（调用方需持锁）。"""
        self._conns.add(pc)
        self._server_count[pc.host] = self._server_count.get(pc.host, 0) + 1
        self._total += 1
        self.stats.total = self._total
        self.stats.created += 1

    def _reserve_locked(self, host: str) -> None:
        """锁内**预留**一个槽位（真正建连在锁外做）。"""
        self._total += 1
        self._server_count[host] = self._server_count.get(host, 0) + 1
        self.stats.total = self._total

    def _unreserve_locked(self, host: str) -> None:
        """撤销预留（建连失败时回滚计数）。"""
        self._total = max(0, self._total - 1)
        if self._server_count.get(host, 0) > 0:
            self._server_count[host] -= 1
        self.stats.total = self._total

    async def close(self) -> None:
        self._closed = True
        if self._reaper_task is not None:
            self._reaper_task.cancel()
            self._reaper_task = None
        # 后台测速仍可能持有 socket；取消并等它退场，避免事件循环关闭时冒出
        # "Task was destroyed but it is pending"（那会破坏"无感"）
        if self._probe_task is not None:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except (asyncio.CancelledError, Exception):
                pass
            self._probe_task = None
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

        **建连在池锁之外**：锁内只预留槽位，TCP 握手 + 探针放到锁外做。否则
        一台坏服务器握手超时（最长 ``self.timeout``）期间，所有其它协程的
        acquire/release 全部被这个锁挡住——5 台坏节点能把整批任务拖慢十几秒。
        """
        deadline = None if timeout is None else (time.monotonic() + timeout)
        avoid: set[str] = set()   # 本次 acquire 里已握手失败的主机，别再重复试
        while True:
            if time.monotonic() < self._blackout_until:
                raise PoolTimeoutError(
                    "所有服务器均不可用（熔断中，稍后自动重试）")
            host = ""
            async with self._cond:
                now = time.monotonic()
                # 1. 优先借用空闲连接（跳过冷却中的服务器，避免反复选中坏节点）
                while self._idle:
                    pc = self._idle.pop(0)
                    fresh = now - pc.last_used < self.idle_ttl
                    if pc.alive and fresh and not self.health.in_cooldown(pc.host):
                        self.stats.borrowed += 1
                        return pc
                    self._drop_locked(pc)

                # 2. 全坏场景下这里必须能退出：扩容失败分支会 continue，
                #    没有这一行就会无限重试、永远不会抛 PoolTimeoutError。
                if deadline is not None and now >= deadline:
                    raise PoolTimeoutError("连接池获取超时（连接数已达上限且无空闲）")

                # 3. 可扩容 → 锁内**只预留槽位**
                if self._total < self.max_size:
                    host = self._next_available_server(avoid=avoid) or ""
                    if not host:
                        # 兜底：全部服务器都在冷却中，而池子未满 —— 此时等 release
                        # 永远等不到（没人持有），只能挑最健康的一台顶着冷却重试
                        host = self._next_available_server(
                            ignore_cooldown=True, avoid=avoid) or ""
                    if host:
                        self._reserve_locked(host)
                    elif avoid and all(h in avoid for h in self.servers):
                        # 本轮每台主机都握手失败过了 → 再等下去也不会有连接。
                        # 同时熔断一小段时间，避免上层「逐只重试」把握手放大成
                        # 成千上万次（实测 6000 标的能空转 60s+）。
                        self._blackout_until = now + self.BLACKOUT_COOLDOWN
                        raise PoolTimeoutError(
                            f"所有服务器均不可用（{len(avoid)} 台均已失败）")

                if not host:
                    # 4. 池满/无可用服务器：排队等 release 唤醒（或超时）
                    remaining = None if deadline is None else deadline - now
                    try:
                        if remaining is None:
                            await self._cond.wait()
                        else:
                            await asyncio.wait_for(self._cond.wait(), timeout=remaining)
                    except asyncio.TimeoutError:
                        raise PoolTimeoutError(
                            "连接池获取超时（连接数已达上限且无空闲）") from None
                    continue

            # ---- 锁外建连：失败只影响本次请求，不冻结其它协程 ----
            try:
                pc = await self._new_conn(host)
            except Exception as e:
                avoid.add(host)
                async with self._cond:
                    self._unreserve_locked(host)
                    self._cond.notify_all()   # 槽位已回滚，唤醒排队者
                self.health.record_failure(host)
                log.warning("扩容连接失败 %s: %s", host, str(e)[:60])
                continue
            if self._closed:
                await pc.close()
                async with self._cond:
                    self._unreserve_locked(host)
                    self._cond.notify_all()
                raise PoolTimeoutError("连接池已关闭")
            # 槽位已在锁内预留，这里只补登记，不重复计数
            self._conns.add(pc)
            self.stats.created += 1
            self.stats.borrowed += 1
            self._blackout_until = 0.0   # 有连接成功 → 立刻解除熔断
            return pc

    async def acquire_many(self, k: int) -> list[PooledConnection]:
        """批量借出 k 个连接（分页并发用），超额请求排队等待不丢。"""
        out: list[PooledConnection] = []
        for _ in range(k):
            try:
                out.append(await self.acquire())
            except PoolTimeoutError:
                break
        return out

    def _next_available_server(self, *, ignore_cooldown: bool = False,
                               avoid: set[str] | None = None) -> str | None:
        """挑一台主机（``avoid`` 里的跳过，用于同一次 acquire 内不重复试错）。"""
        avoid = avoid or set()
        ranked = self.health.rank([h for h in self.servers if h not in avoid])
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

    async def warm(self, n: int) -> None:
        """并发预建连接至 ``n`` 条（最多到 ``max_size``）。

        :meth:`acquire` 建连时持有池锁，**一次只能建一条**——22 路并发首次
        调用会排队 22×握手时间（实测全市场首拉多花 5~6s）。大批量请求前先调
        本方法把连接并发建好，首拉只多付一次握手。

        失败静默忽略：缺的连接后续由 ``acquire`` 按需补齐。
        """
        if self._closed:
            return
        need = min(n, self.max_size) - self._total
        if need <= 0:
            return

        # 用局部计数挑 host，避免并发建连时反复选中同一台
        counts = dict(self._server_count)
        ranked = self.health.rank(self.servers)
        hosts: list[str] = []
        for _ in range(need):
            host = next((h for h in ranked
                         if not self.health.in_cooldown(h)
                         and counts.get(h, 0) < self.per_server_cap), None)
            if host is None:
                break
            hosts.append(host)
            counts[host] = counts.get(host, 0) + 1
        if not hosts:
            return

        results = await asyncio.gather(
            *(self._new_conn(h) for h in hosts), return_exceptions=True)
        async with self._lock:
            for host, res in zip(hosts, results):
                if not isinstance(res, PooledConnection):
                    # 建连失败 → 记一次失败，后续 acquire 会把它排到队尾
                    self.health.record_failure(host)
                    continue
                pc: PooledConnection = res
                if self._closed or not pc.alive or self._total >= self.max_size:
                    # 建好却发现池已满/已关闭：必须关掉，否则连接泄漏，
                    # 而且 _total 会被虚增（老实现就在这儿漏计数）
                    asyncio.create_task(self._safe_close(pc))
                    continue
                self._register_locked(pc)
                self._idle.append(pc)
            self._cond.notify_all()

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
