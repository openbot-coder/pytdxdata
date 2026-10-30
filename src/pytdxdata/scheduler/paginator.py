"""并发分页调度器：单请求拆N页，用至多 concurrency 个连接并行拉取，按序拼接。

v5 设计（回归 v1 稳定模型 + 修复连接复用 bug）：
- 预分配 k 个连接，每个 worker 复用同一连接（避免每页 acquire 开销）。
- 单页失败时：释放坏连接 → 获取新连接 → 更新 worker 持有的连接引用，
  避免跨页使用已关闭连接导致的连锁失败。
- 不额外加重试轮（避免引入更多 acquire 命中坏服务器的机会）。
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable

from ..pool.pool import DynamicPool, PooledConnection, PoolTimeoutError

log = logging.getLogger(__name__)


@dataclass(slots=True)
class PageSpec:
    page_size: int = 800
    total: int | None = None          # 已知总量（None=用 stop_when_short）
    stop_when_short: bool = True      # 某页返回 < page_size 即停
    max_pages: int | None = None
    ordered: bool = True              # 按 start 升序拼接
    min_page: int = 100               # 规避标准K线 count<100 空响应坑
    start_offset: int = 0             # 起始偏移（从该偏移开始分页，避免拉取历史全量）
    newest_first: bool = False        # 服务端 start 从最新往回数（0x122F 逐笔即如此）


@dataclass(slots=True)
class PageResult:
    start: int
    count: int
    items: list[Any]


class _Slot:
    """worker 当前持有的连接槽：失败换连时原地更新，保证最终能精确归还。"""

    __slots__ = ("pc",)

    def __init__(self, pc: PooledConnection) -> None:
        self.pc: PooledConnection | None = pc


class Paginator:
    """用至多 concurrency 个连接并行拉取分页数据。"""

    def __init__(self, pool: DynamicPool, *, concurrency: int = 3,
                 retries: int = 2, strict: bool = True) -> None:
        self.pool = pool
        self.concurrency = max(1, concurrency)
        self.retries = retries
        self.strict = strict

    async def fetch_all(
        self,
        cmd_factory: Callable[[int, int], Any],   # (start, count) -> BaseCommand
        spec: PageSpec,
        *,
        concurrency: int | None = None,
    ) -> list[Any]:
        """按序拼接后的完整结果。"""
        jobs = self._plan_starts(spec)
        if not jobs:
            return []
        eff_conc = min(
            self.concurrency if concurrency is None else concurrency,
            len(jobs),
        )
        conns = await self.pool.acquire_many(eff_conc)
        if not conns:
            raise PoolTimeoutError("连接池获取超时（无可用连接）")
        # 每个 worker 一个连接槽：中途换连会更新槽内引用，
        # 归还时按槽归还（否则换掉的新连接会漏还，把池耗干）
        slots = [_Slot(pc) for pc in conns]
        try:
            stop = asyncio.Event()
            out: list[PageResult] = []
            out_lock = asyncio.Lock()
            # 静态切片保证「每台连接都干活」（并发摊开），另备一个共享的
            # ``leftover``：某个 worker 的连接坏掉退出时，它**没做完的页**会
            # 放回这里，由还活着的 worker 接着做。老实现没有这层——一个
            # worker 挂掉，它那一整份页就没人做了，要么整轮报错、要么静默缺页。
            leftover: deque[tuple[int, int]] = deque()
            q_lock = asyncio.Lock()
            k = len(slots)
            workers = [
                asyncio.create_task(
                    self._worker(slots[wid], jobs[wid::k], leftover, q_lock,
                                 cmd_factory, stop, out, out_lock, spec)
                )
                for wid in range(k)
            ]
            await asyncio.gather(*workers)
            # 收尾补页：所有 worker 都退出后 leftover 里可能还剩页（连接全坏时会
            # 发生）。再借一条**新连接**顺序把它们补完，最多两轮——否则 skip 的页
            # 就变成静默缺数据（strict=False 时最危险：不报错、只是少几页）。
            rounds = 0
            while leftover and not stop.is_set() and rounds < 2:
                rounds += 1
                try:
                    pc = await self.pool.acquire(timeout=5.0)
                except PoolTimeoutError:
                    break
                slot = _Slot(pc)
                progressed = False
                try:
                    while leftover and not stop.is_set():
                        start, count = leftover[0]       # 成功后才出队，失败可重试
                        items, ok = await self._fetch_one(
                            slot, cmd_factory(start, count), count)
                        if not ok:
                            break
                        leftover.popleft()
                        progressed = True
                        async with out_lock:
                            out.append(PageResult(start=start, count=count, items=items))
                        if spec.stop_when_short and len(items) < count:
                            stop.set()
                finally:
                    if slot.pc is not None:
                        await self.pool.release(slot.pc)
                if not progressed:
                    break
            if leftover:
                log.warning("分页调度：%d 页最终未取到（strict=False 留空）", len(leftover))
        finally:
            for s in slots:
                if s.pc is not None:
                    pc, s.pc = s.pc, None
                    await self.pool.release(pc)
        return self._assemble(out, spec.ordered, spec.newest_first)

    def _plan_starts(self, spec: PageSpec) -> list[tuple[int, int]]:
        """切页：返回 [(start, count), ...]。"""
        jobs: list[tuple[int, int]] = []
        if spec.total is not None:
            pos = spec.start_offset
            pages = 0
            while pos < spec.total:
                if spec.max_pages is not None and pages >= spec.max_pages:
                    break
                cnt = min(spec.page_size, spec.total - pos)
                # 已知 total 时直接使用精确 cnt，不强制 min_page（避免过量请求）
                jobs.append((pos, cnt))
                pos += cnt
                pages += 1
        else:
            for page in range(spec.max_pages if spec.max_pages is not None else 1024):
                jobs.append((spec.start_offset + page * spec.page_size, spec.page_size))
        return jobs

    async def _worker(
        self, slot: _Slot,
        jobs: list[tuple[int, int]],
        leftover: "deque[tuple[int, int]]",
        q_lock: asyncio.Lock,
        cmd_factory: Callable[[int, int], Any], stop: asyncio.Event,
        out: list[PageResult], out_lock: asyncio.Lock, spec: PageSpec,
    ) -> None:
        i = 0
        try:
            while True:
                if i < len(jobs):
                    start, count = jobs[i]
                    i += 1
                else:
                    # 自己的份额做完了 → 看看有没有别人掉下的活
                    async with q_lock:
                        if stop.is_set() or not leftover:
                            return
                        start, count = leftover.popleft()
                if slot.pc is None or stop.is_set():
                    return
                items, ok = await self._fetch_one(slot, cmd_factory(start, count), count)
                if not ok:
                    return   # 换连接失败 → 本 worker 退出（剩余页在 finally 里交还）
                async with out_lock:
                    out.append(PageResult(start=start, count=count, items=items))
                if spec.stop_when_short and len(items) < count:
                    stop.set()
                    return
        finally:
            rest = jobs[i:]
            if rest:
                async with q_lock:
                    leftover.extendleft(reversed(rest))

    async def _fetch_one(self, slot: _Slot, cmd, page_size: int):
        """单页拉取，失败换连接重试。

        返回 ``(items, ok)``。失败时连接一律 ``broken=True`` 归还并把槽清空，
        调用方据此退出，不会留下悬空引用。
        """
        for attempt in range(self.retries + 1):
            try:
                result = await slot.pc.execute(cmd)   # type: ignore[union-attr]
                return (list(result) if result is not None else []), True
            except Exception as e:
                if slot.pc is not None:
                    await self.pool.release(slot.pc, broken=True)
                    slot.pc = None
                if attempt >= self.retries:
                    if self.strict:
                        raise
                    log.warning("分页拉取失败(strict=False留空): %s", e)
                    return None, False
                try:
                    slot.pc = await self.pool.acquire(timeout=3.0)
                except PoolTimeoutError:
                    log.warning("分页重试获取连接失败，跳过该页: %s", e)
                    return None, False
        return None, False

    def _assemble(self, results: list[PageResult], ordered: bool,
                  newest_first: bool = False) -> list[Any]:
        # newest_first=True：start=0 是最新一页，start 越大越旧 → 反向拼才对得上时间序。
        # 单页内部服务端已按时间升序，所以只翻页序，不翻页内元素。
        if ordered:
            results.sort(key=lambda r: r.start, reverse=newest_first)
        out: list[Any] = []
        for r in results:
            out.extend(r.items)
        return out
