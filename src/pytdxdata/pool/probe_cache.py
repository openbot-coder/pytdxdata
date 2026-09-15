"""当日首次启动的全量服务器测速与入库缓存。

设计要点：
- 仅在"当日首次启动"时触发一次后台全量测速（按本地日期判断是否已探测）。
- 测速采用真实握手连通（TdxConnection.test），比单纯 ping 更能反映通达信可用性。
- 入库规则：延迟排名前 FAST_POOL_TOP_N(10) **或** 延迟 < FAST_POOL_MAX_LATENCY(0.2s) 的服务器
  才标记为 status="ok" 并写入 servers.json，其余降权（保留但不优先）。
- 结果持久化到 servers.json，供 config / 连接池直接使用。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date
from pathlib import Path

from ..config import _SERVERS_FILE

log = logging.getLogger(__name__)

# 入池阈值
FAST_POOL_TOP_N = 10          # 延迟排名前 N
FAST_POOL_MAX_LATENCY = 0.2   # 延迟 < 200ms（秒）也可入池
PROBE_PORT = 7709
PROBE_TIMEOUT = 3.0
PROBE_CONCURRENCY = 24


class ProbeCache:
    """管理当日测速缓存与 servers.json 入库。

    一台服务器不论协议（standard/mac/ex），测速结果共享：
    只要任一端口可达即视为该主机可用，并以最快端口延迟作为延迟基准。
    """

    def __init__(self, servers_file: Path | None = None) -> None:
        self._file = servers_file or _SERVERS_FILE
        self._lock = asyncio.Lock()
        self._last_probe_date: str | None = None
        self._probed_today = False

    # ---- 公开 API ----
    def needs_probe_today(self) -> bool:
        """是否有必要在今日执行一次全量测速（文件不存在 / 日期不符 / 无 ok 项）。"""
        if not self._file.exists():
            return True
        try:
            cfg = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return True
        checked = str(cfg.get("checked_date", ""))
        today = date.today().isoformat()
        if checked != today:
            return True
        servers = cfg.get("servers", [])
        if not any(s.get("status") == "ok" for s in servers):
            return True
        return False

    async def ensure_probe(self, hosts: list[str]) -> None:
        """若今日尚未测速，则后台执行一次全量测速并入库（幂等）。"""
        async with self._lock:
            if self._probed_today:
                return
            if not self.needs_probe_today():
                self._probed_today = True
                return
            self._probed_today = True

        # 在锁外执行（耗时网络 IO），允许并发进入的调用方共享本次结果
        try:
            await self._run_full_probe(hosts)
        except Exception as e:  # 测速失败不能阻塞主流程
            log.warning("后台全量测速失败，沿用既有清单: %s", str(e)[:80])

    # ---- 内部实现 ----
    async def _run_full_probe(self, hosts: list[str]) -> None:
        from .connection import TdxConnection

        log.info("开始当日全量服务器测速 (%d 台)...", len(hosts))
        sem = asyncio.Semaphore(PROBE_CONCURRENCY)
        results: dict[str, float | None] = {}

        async def _probe_one(host: str) -> None:
            async with sem:
                try:
                    # 使用连接自带的 ping()：建连+握手+发1条命令测往返延迟
                    conn = TdxConnection(host, PROBE_PORT, PROBE_TIMEOUT)
                    dt = await asyncio.wait_for(conn.ping(), timeout=PROBE_TIMEOUT + 1.0)
                    results[host] = dt  # 失败时为 None
                except Exception:
                    results[host] = None

        await asyncio.gather(*(_probe_one(h) for h in hosts))

        ok_hosts = {h: lt for h, lt in results.items() if lt is not None}
        # 按延迟升序排名
        ranked = sorted(ok_hosts.items(), key=lambda kv: kv[1])
        fast = set()
        for i, (h, lt) in enumerate(ranked):
            if i < FAST_POOL_TOP_N or lt < FAST_POOL_MAX_LATENCY:
                fast.add(h)

        log.info("测速完成: 可用 %d 台, 入池 %d 台 (前%d或<%dms)",
                 len(ok_hosts), len(fast), FAST_POOL_TOP_N,
                 int(FAST_POOL_MAX_LATENCY * 1000))
        self._write_results(results, fast)

    def _write_results(self, results: dict[str, float | None], fast: set[str]) -> None:
        try:
            cfg = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {"servers": []}
        servers = cfg.get("servers", [])
        by_ip = {s.get("ip"): s for s in servers}

        today = date.today().isoformat()
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        checked_any = False
        for host, lt in results.items():
            entry = by_ip.get(host)
            if entry is None:
                entry = {"ip": host, "name": host, "protocols": ["standard"]}
                servers.append(entry)
                by_ip[host] = entry
            if lt is not None:
                checked_any = True
                entry["status"] = "ok" if host in fast else "slow"
                entry["latency"] = round(lt, 3)
                entry["fast_pool"] = host in fast
            else:
                # 探测不可达：不降级已有 ok（避免误杀），仅标记本次不可达
                entry.setdefault("status", "unknown")
                entry["latency"] = None
                entry["fast_pool"] = False
            entry["checked_date"] = today
            entry["checked_at"] = now

        # 若有 ok 服务器写入可入池日期；否则保留今日以免反复重试
        cfg["checked_date"] = today
        cfg["note"] = (
            f"当日全量测速入库({today}): 入池={len(fast)}台 "
            f"(排名前{FAST_POOL_TOP_N}或延迟<{int(FAST_POOL_MAX_LATENCY*1000)}ms)"
        )
        if not checked_any:
            # 全部不可达时不写 checked_date，下次启动重试
            cfg.pop("checked_date", None)

        self._file.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("测速结果已写入 %s", self._file)


# 进程级单例（跨池共享，确保当日仅测一次）
_probe_cache = ProbeCache()


def get_probe_cache() -> ProbeCache:
    return _probe_cache
