"""磁盘持久缓存：SQLite WAL + asyncio.to_thread（事件循环零阻塞）"""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    ttl_name   TEXT NOT NULL,
    expires_at REAL NOT NULL,
    size       INTEGER NOT NULL,
    payload    BLOB NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);
"""


class DiskCache:
    def __init__(self, path: Path, *, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self._write_conn: sqlite3.Connection | None = None
        self._write_lock = threading.Lock()
        self._read_conn: sqlite3.Connection | None = None

    async def open(self) -> None:
        def _init():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._write_conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._write_conn.execute("PRAGMA journal_mode=WAL")
            self._write_conn.execute("PRAGMA synchronous=NORMAL")
            self._write_conn.executescript(_SCHEMA)
            self._write_conn.commit()
            # 读连接
            self._read_conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._read_conn.execute("PRAGMA query_only=ON")
        await asyncio.to_thread(_init)

    async def close(self) -> None:
        def _close():
            for c in (self._write_conn, self._read_conn):
                if c is not None:
                    c.close()
        await asyncio.to_thread(_close)

    async def get(self, key: str) -> bytes | None:
        return (await self.get_many([key]))[0]

    async def get_many(self, keys: list[str]) -> list[bytes | None]:
        """批量读，减少往返。"""
        def _read() -> list[bytes | None]:
            conn = self._read_conn
            if conn is None:
                return [None] * len(keys)
            now = time.time()
            out: list[bytes | None] = []
            for k in keys:
                row = conn.execute(
                    "SELECT payload, expires_at FROM cache WHERE key=?", (k,)
                ).fetchone()
                if row is None:
                    out.append(None)
                elif row[1] > 0 and now > row[1]:
                    out.append(None)
                else:
                    out.append(row[0])
            return out
        return await asyncio.to_thread(_read)

    async def set(self, key: str, value: bytes, ttl: float, ttl_name: str = "") -> None:
        def _write():
            with self._write_lock:
                if self._write_conn is None:
                    return
                now = time.time()
                expires_at = 0.0 if ttl <= 0 else now + ttl
                self._write_conn.execute(
                    "INSERT OR REPLACE INTO cache (key, ttl_name, expires_at, size, payload, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (key, ttl_name, expires_at, len(value), value, now),
                )
                self._write_conn.commit()
        await asyncio.to_thread(_write)

    async def delete(self, key: str) -> None:
        def _del():
            with self._write_lock:
                if self._write_conn is not None:
                    self._write_conn.execute("DELETE FROM cache WHERE key=?", (key,))
                    self._write_conn.commit()
        await asyncio.to_thread(_del)

    async def delete_prefix(self, prefix: str) -> int:
        """按前缀批量删除，返回删除条数。"""
        def _del_prefix() -> int:
            with self._write_lock:
                if self._write_conn is None:
                    return 0
                cur = self._write_conn.execute(
                    "DELETE FROM cache WHERE key LIKE ?", (f"{prefix}%",)
                )
                self._write_conn.commit()
                return cur.rowcount
        return await asyncio.to_thread(_del_prefix)

    async def sweep(self) -> int:
        """清理过期行 + 超限淘汰，返回清理条数。"""
        def _sweep() -> int:
            with self._write_lock:
                if self._write_conn is None:
                    return 0
                now = time.time()
                cur = self._write_conn.execute(
                    "DELETE FROM cache WHERE expires_at > 0 AND expires_at < ?", (now,)
                )
                n = cur.rowcount
                # 超限淘汰最旧
                total = self._write_conn.execute("SELECT COALESCE(SUM(size),0) FROM cache").fetchone()[0]
                if total > self.max_bytes:
                    overflow = total - self.max_bytes
                    rows = self._write_conn.execute(
                        "SELECT key, size FROM cache ORDER BY created_at ASC"
                    ).fetchall()
                    freed = 0
                    for k, sz in rows:
                        if freed >= overflow:
                            break
                        self._write_conn.execute("DELETE FROM cache WHERE key=?", (k,))
                        freed += sz
                        n += 1
                self._write_conn.commit()
                return n
        return await asyncio.to_thread(_sweep)

    async def stats(self) -> dict:
        def _stats() -> dict:
            if self._read_conn is None:
                return {"rows": 0, "bytes": 0}
            r = self._read_conn.execute(
                "SELECT count(*), COALESCE(SUM(size),0) FROM cache"
            ).fetchone()
            return {"rows": r[0], "bytes": r[1]}
        return await asyncio.to_thread(_stats)
