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


@dataclass(slots=True)
class PageResult:
    start: int
    count: int
    items: list[Any]


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
        try:
            stop = asyncio.Event()
            out: list[PageResult] = []
            out_lock = asyncio.Lock()
            k = len(conns)
            workers = [
                asyncio.create_task(
                    self._worker(wid, conns[wid % k], jobs[wid::k], cmd_factory,
                                 stop, out, out_lock, spec)
                )
                for wid in range(k)
            ]
            await asyncio.gather(*workers)
        finally:
            for pc in conns:
                await self.pool.release(pc)
        return self._assemble(out, spec.ordered)

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
        self, wid: int, conn: PooledConnection, jobs: list[tuple[int, int]],
        cmd_factory: Callable[[int, int], Any], stop: asyncio.Event,
        out: list[PageResult], out_lock: asyncio.Lock, spec: PageSpec,
    ) -> None:
        current = conn
        for start, count in jobs:
            if stop.is_set():
                return
            items, current = await self._fetch_one(
                current, cmd_factory(start, count), count)
            if items is None:
                continue  # 该页失败（strict=False 时留空）
            async with out_lock:
                out.append(PageResult(start=start, count=count, items=items))
            if spec.stop_when_short and len(items) < count:
                stop.set()
                return

    async def _fetch_one(self, conn: PooledConnection, cmd, page_size: int):
        """单页拉取，失败换连接重试。

        返回 (items, current_conn) — current_conn 始终指向当前可用连接，
        worker 据此更新自己的连接引用，避免跨页使用已关闭连接。
        """
        current = conn
        for attempt in range(self.retries + 1):
            try:
                result = await current.execute(cmd)
                return (list(result) if result is not None else []), current
            except Exception as e:
                if attempt >= self.retries:
                    if self.strict:
                        raise
                    log.warning("分页拉取失败(strict=False留空): %s", e)
                    return None, current
                # 换连接重试
                await self.pool.release(current, broken=True)
                try:
                    current = await self.pool.acquire(timeout=3.0)
                except PoolTimeoutError:
                    log.warning("分页重试获取连接失败，跳过该页: %s", e)
                    return None, current
        return None, current

    def _assemble(self, results: list[PageResult], ordered: bool) -> list[Any]:
        if ordered:
            results.sort(key=lambda r: r.start)
        out: list[Any] = []
        for r in results:
            out.extend(r.items)
        return out
