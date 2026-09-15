"""缓存管理器：内存LRU + 磁盘SQLite 双层 + single-flight 并发去重"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .disk import DiskCache
from .key import CacheKey
from .memory import MemoryCache
from .ttl import ttl_for

log = logging.getLogger(__name__)


class CacheManager:
    """双层缓存：读 内存→磁盘→回源；写 内存立即+异步回写磁盘。

    single-flight：同 key 并发请求只回源一次，其余 await 同一 future。
    """

    def __init__(self, memory: MemoryCache, disk: DiskCache | None = None,
                 *, max_inflight: int = 64) -> None:
        self.memory = memory
        self.disk = disk
        self._locks: dict[str, asyncio.Lock] = {}
        self._semaphore = asyncio.Semaphore(max_inflight)

    async def start(self) -> None:
        if self.disk is not None:
            await self.disk.open()
            await self.disk.sweep()

    async def close(self) -> None:
        if self.disk is not None:
            await self.disk.close()

    async def get(self, key: CacheKey) -> bytes | None:
        ks = key.to_str()
        # 1. 内存
        v = self.memory.get(ks)
        if v is not None:
            return v
        # 2. 磁盘
        if self.disk is not None:
            v = (await self.disk.get_many([ks]))[0]
            if v is not None:
                ttl = ttl_for(key.category, period=key.period, adjust=key.extra, date=_parse_date(key.extra))
                self.memory.set(ks, v, ttl)
                return v
        return None

    async def set(self, key: CacheKey, payload: bytes, *, ttl: float | None = None) -> None:
        ks = key.to_str()
        t = ttl if ttl is not None else ttl_for(
            key.category, period=key.period, adjust=key.extra, date=_parse_date(key.extra)
        )
        self.memory.set(ks, payload, t)
        if self.disk is not None:
            # 异步回写（best-effort，失败不阻塞）
            try:
                await self.disk.set(ks, payload, t, ttl_name=key.category)
            except Exception:
                log.debug("磁盘缓存写失败: %s", ks)

    async def get_or_fetch(
        self,
        key: CacheKey,
        fetcher: Callable[[], Awaitable[bytes]],
    ) -> bytes:
        """命中缓存返回；否则 single-flight 回源并缓存。"""
        ks = key.to_str()
        cached = await self.get(key)
        if cached is not None:
            return cached

        lock = self._locks.setdefault(ks, asyncio.Lock())
        try:
            async with lock:
                # 双检：等待期间可能已被其他任务写入
                cached = await self.get(key)
                if cached is not None:
                    return cached
                async with self._semaphore:
                    payload = await fetcher()
                await self.set(key, payload)
                return payload
        finally:
            # 确保清理锁（即使 fetcher 异常也清理，避免无限增长）
            self._locks.pop(ks, None)

    async def invalidate(self, category: str, *, market: int | None = None,
                         code: str | None = None) -> int:
        """按前缀批量失效，返回失效条数。"""
        n = 0
        # 内存：遍历匹配（使用公共 keys() 方法）
        keys = self.memory.keys()
        for k in keys:
            try:
                ck = CacheKey.from_str(k)
            except Exception:
                continue
            if ck.category == category:
                if market is not None and ck.market != market:
                    continue
                if code is not None and ck.code != code:
                    continue
                self.memory.delete(k)
                n += 1
        # 磁盘：按前缀删除（best-effort）
        if self.disk is not None:
            prefix = f"{category}|{'' if market is None else market}|"
            try:
                await self.disk.delete_prefix(prefix)
            except Exception:
                pass
        return n


def _parse_date(extra: str) -> int | None:
    """从 extra 解析 date（transaction类）。extra 可能是 '20260811' 或空。"""
    if extra and extra.isdigit() and len(extra) == 8:
        return int(extra)
    return None
