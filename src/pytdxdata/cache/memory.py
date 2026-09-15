"""内存TTL感知LRU缓存（OrderedDict，get命中刷新热度，超容量逐出最旧）"""
from __future__ import annotations

import time
from collections import OrderedDict


class MemoryCache:
    def __init__(self, max_entries: int = 4096, max_bytes: int = 128 * 1024 * 1024) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._data: OrderedDict[str, tuple[bytes, float]] = OrderedDict()  # key -> (value, expires_at)
        self._bytes = 0

    def get(self, key: str) -> bytes | None:
        item = self._data.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at > 0 and time.monotonic() > expires_at:
            self._delete(key)
            return None
        self._data.move_to_end(key)  # LRU刷新
        return value

    def set(self, key: str, value: bytes, ttl: float) -> None:
        expires_at = 0.0 if ttl <= 0 else time.monotonic() + ttl
        old = self._data.pop(key, None)
        if old is not None:
            self._bytes -= len(old[0])
        self._data[key] = (value, expires_at)
        self._bytes += len(value)
        # 超容量逐出最旧
        while (len(self._data) > self.max_entries or self._bytes > self.max_bytes) and self._data:
            self._data.popitem(last=False)
        # 简化：逐出后重新计算字节数
        self._recount()

    def delete(self, key: str) -> None:
        self._delete(key)

    def keys(self) -> list[str]:
        """返回当前所有缓存 key（供失效/清理使用）。"""
        return list(self._data.keys())

    def _delete(self, key: str) -> None:
        item = self._data.pop(key, None)
        if item is not None:
            self._bytes -= len(item[0])

    def _recount(self) -> None:
        self._bytes = sum(len(v[0]) for v in self._data.values())

    def clear(self) -> None:
        self._data.clear()
        self._bytes = 0

    def __len__(self) -> int:
        return len(self._data)

    @property
    def bytes_used(self) -> int:
        return self._bytes
