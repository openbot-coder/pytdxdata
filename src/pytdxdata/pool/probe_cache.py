"""当日首次启动的全量服务器测速与缓存（对用户完全无感）。

行为
----
- **当日首次启动**触发一次后台全量测速；当天已有缓存则直接复用，**不再刷新**。
- 测速覆盖**全部候选**（包内清单 ∪ 代码常量 ∪ env 覆盖，见
  :func:`pytdxdata.config.all_probe_candidates`），而不是只测当前已在池里的少数节点 ——
  否则"上次慢、这次快"的节点永远捞不回来。
- 用**真实业务命令**判定可达（标准: GetSecurityBarsCmd / MAC: MacKlineCmd /
  EX: GetExInstrumentBarsCmd）并要求返回非空数据，而不是只做握手回显：
  存在"握手正常但 K线空响应"的坏节点（见 ``config.py`` 注释），只有真数据能测出来。
- 结果写入**用户级缓存目录**（见 :func:`pytdxdata.config.probe_cache_file`），
  不写安装目录：site-packages 在容器 / 系统级安装 / pipx 下可能只读，
  多用户共用一份安装时还会互相覆盖。
- 结果被 :func:`pytdxdata.config.get_standard_hosts` 等按**实测延迟升序**采用。

无感保证
--------
- 不打印任何东西；日志一律 ``debug`` 级（默认不输出）。
- 不抛异常（取消除外）；失败只落 debug 日志，下次启动自然重试。
- 由连接池以后台 task 方式调用，不阻塞 ``start()`` 与任何用户请求。

一致性与并发
------------
- 进程内单例 + ``asyncio.Lock``：标准 / MAC / EX 三个池共用一次真正测速。
- 原子写入（tmp + ``os.replace``）：进程被杀或多进程并发都不会读到半截 JSON。
- 全部不可达时**不写缓存**，避免把"全挂"状态缓存一整天。

开关
----
- ``PYPYTDX_DATA_DISABLE_PROBE=1`` 关闭（CI / 测试用，保证确定性）。
- ``PYPYTDX_DATA_CACHE_DIR``       覆盖缓存目录（测试指向临时目录）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path

from ..config import (
    PROBE_CACHE_VERSION,
    all_probe_candidates,
    get_ex_handshakes,
    is_valid_host,
    load_probe_cache,
    probe_cache_file,
    probe_disabled,
)
from ..models import Adjust, KlinePeriod
from ..protocol.commands.ex_proto import GetExInstrumentBarsCmd
from ..protocol.commands.kline import GetSecurityBarsCmd
from ..protocol.commands.mac import MacKlineCmd
from .connection import TdxConnection
from .ex_connection import ExTdxConnection

log = logging.getLogger(__name__)

# 入池阈值（仅用于缓存里的 fast 诊断标记；运行时按实测延迟整体排序，不做硬切）
FAST_POOL_TOP_N = 10
FAST_POOL_MAX_LATENCY = 0.2
# 探针参数
PROBE_PORT = 7709
EX_PORT = 7727
PROBE_TIMEOUT = 3.0
PROBE_CONCURRENCY = 12
PROBE_BUDGET = 60.0  # 整轮测速硬上限（秒），防止极端网络下长期占着后台

ProbeFn = Callable[[str, float], Awaitable[float | None]]


# ---------- 三个协议各自的真实数据探针 ----------
async def _probe_standard(host: str, timeout: float) -> float | None:
    """标准通道 7709：取 600000 日K，非空才算可达。"""
    conn = TdxConnection(host, PROBE_PORT, timeout)
    t0 = time.monotonic()
    try:
        await conn.connect()
        bars = await conn.execute(GetSecurityBarsCmd(1, "600000", KlinePeriod.DAY, 0, 100))
        return time.monotonic() - t0 if bars else None
    except Exception:
        return None
    finally:
        await conn.close()


async def _probe_mac(host: str, timeout: float) -> float | None:
    """MAC 通道 7709：MAC 帧与标准帧不同，用 MacKlineCmd 取日K。"""
    conn = TdxConnection(host, PROBE_PORT, timeout)
    t0 = time.monotonic()
    try:
        await conn.connect()
        bars = await conn.execute(
            MacKlineCmd(1, "600000", KlinePeriod.DAY, Adjust.NONE, 0, 100)
        )
        return time.monotonic() - t0 if bars else None
    except Exception:
        return None
    finally:
        await conn.close()


async def _probe_ex(host: str, timeout: float) -> float | None:
    """EX 扩展市场 7727：取港股 00700，握手模式按主机取（login / setup）。"""
    conn = ExTdxConnection(host, EX_PORT, timeout, get_ex_handshakes().get(host, "setup"))
    t0 = time.monotonic()
    try:
        await conn.connect()
        bars = await conn.execute(GetExInstrumentBarsCmd(31, "00700", 0, 0, 5))
        return time.monotonic() - t0 if bars else None
    except Exception:
        return None
    finally:
        await conn.close()


_DEFAULT_PROBERS: dict[str, ProbeFn] = {
    "standard": _probe_standard,
    "mac": _probe_mac,
    "ex": _probe_ex,
}


class ProbeCache:
    """当日全量测速的进程内协调器。

    一台主机不论申报几个协议，都按协议分别探测；缓存里记录
    ``{host: {protocol: 延迟或 None}}``，``None`` 表示该协议本次不可达。

    参数（后 5 个主要供测试注入）：
        cache_file: 显式指定缓存文件；``None`` 时按 config 的平台规则解析。
        probers:    协议 → 探针函数；默认使用真实网络探针。
        today_fn:   返回 ``YYYY-MM-DD``；测试可注入假日期来验证跨日重测。
    """

    def __init__(self, cache_file: Path | str | None = None, *,
                 timeout: float = PROBE_TIMEOUT,
                 concurrency: int = PROBE_CONCURRENCY,
                 budget: float = PROBE_BUDGET,
                 probers: dict[str, ProbeFn] | None = None,
                 today_fn: Callable[[], str] | None = None) -> None:
        self._file = Path(cache_file) if cache_file is not None else None
        self._timeout = timeout
        self._concurrency = concurrency
        self._budget = budget
        self._probers = dict(probers or _DEFAULT_PROBERS)
        self._today_fn = today_fn or (lambda: date.today().isoformat())
        self._lock = asyncio.Lock()
        self._handled = False  # 本进程是否已处理过今日测速（含"已有缓存，跳过"）
        self._task: asyncio.Task | None = None

    # ---------- 路径与状态 ----------
    def cache_file(self) -> Path:
        """缓存文件路径（构造时显式传入的优先，否则按平台规则解析）。"""
        return self._file if self._file is not None else probe_cache_file()

    def today(self) -> str:
        return self._today_fn()

    def read_cache(self) -> dict | None:
        """读取当日缓存；``None`` 表示"今天还没测过"。"""
        return load_probe_cache(self.today(), self.cache_file())

    def needs_probe_today(self) -> bool:
        """今日是否仍需测速（缓存缺失 / 非今日 / 损坏 / 版本不符 / 无任何可用项）。"""
        if probe_disabled():
            return False
        return self.read_cache() is None

    def reset(self) -> None:
        """清除"本进程已处理"标记（仅供测试复用单例）。"""
        self._handled = False

    # ---------- 主入口 ----------
    async def ensure_probe(self, hosts: list[str] | None = None) -> None:
        """幂等入口：当日尚未测速则测一次；当天已有缓存则立即返回。

        - 不阻塞调用方：正常由连接池以后台 task 调用。
        - 绝不抛异常（``CancelledError`` 除外）：测速 / 写盘失败只落 debug 日志。
        - ``hosts``：为兼容旧签名保留，作为**额外的标准通道候选**并入，
          且必须通过 :func:`~pytdxdata.config.is_valid_host` 校验（挡住 mock 脏数据）。
        """
        async with self._lock:
            if self._handled:
                return
            self._handled = True  # 先占位：并发调用方不必排队等网络
            if not self.needs_probe_today():
                log.debug("当日测速缓存已存在，跳过测速: %s", self.cache_file())
                return
        try:
            payload = await self._run_probe(hosts)
            if payload is not None:
                self._write(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 测速绝不能影响主流程
            log.debug("后台测速失败，沿用既有清单: %s", str(exc)[:120])

    # ---------- 内部实现 ----------
    async def _run_probe(self, extra_hosts: list[str] | None = None) -> dict | None:
        """全量并发测速，返回待写入的缓存 payload；无任何可达主机时返回 None。"""
        candidates = all_probe_candidates()
        if extra_hosts:
            extra = [h.strip() for h in extra_hosts
                     if isinstance(h, str) and is_valid_host(h)]
            if extra:
                candidates["standard"] = list(dict.fromkeys(candidates["standard"] + extra))
        jobs = [(proto, ip) for proto, ips in candidates.items() for ip in ips]
        if not jobs:
            log.debug("无测速候选，跳过")
            return None

        log.debug("开始当日全量测速: %d 台主机 / %d 个协议探针",
                  len({ip for _, ip in jobs}), len(jobs))
        sem = asyncio.Semaphore(self._concurrency)
        # {host: {protocol: 延迟或 None}}
        results: dict[str, dict[str, float | None]] = defaultdict(dict)

        async def _one(protocol: str, host: str) -> None:
            async with sem:
                try:
                    lat = await asyncio.wait_for(
                        self._probers[protocol](host, self._timeout),
                        timeout=self._timeout + 2.0,
                    )
                except Exception:
                    lat = None
                results[host][protocol] = lat

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.gather(*(_one(p, ip) for p, ip in jobs)),
                timeout=self._budget,
            )
        except TimeoutError:
            # 超时：采用已完成的部分结果（部分可用也强于完全没有）
            log.debug("测速整体超过 %.0fs 预算，采用已完成的部分结果", self._budget)
        except Exception as exc:
            log.debug("测速过程异常: %s", str(exc)[:120])
        elapsed = time.monotonic() - t0

        responsive = {
            ip: {p: lat for p, lat in per.items() if lat is not None}
            for ip, per in results.items()
        }
        responsive = {ip: per for ip, per in responsive.items() if per}
        if not responsive:
            log.debug("测速结束: 无任何可达主机，不写缓存（下次启动重试）")
            return None

        def _ordered(protocol: str) -> list[str]:
            rows = [(per[protocol], ip) for ip, per in responsive.items()
                    if protocol in per]
            rows.sort(key=lambda kv: (kv[0], kv[1]))
            return [ip for _, ip in rows]

        hosts_by_proto = {p: _ordered(p) for p in ("standard", "mac", "ex")}
        overall = sorted(
            (min(per.values()), ip) for ip, per in responsive.items()
        )
        fast = {ip for i, (lat, ip) in enumerate(overall)
                if i < FAST_POOL_TOP_N or lat < FAST_POOL_MAX_LATENCY}

        log.debug("测速完成: 候选 %d 台, 可达 %d 台, fast %d 台, 耗时 %.2fs",
                  len({ip for _, ip in jobs}), len(responsive), len(fast), elapsed)
        return {
            "version": PROBE_CACHE_VERSION,
            "date": self.today(),
            "probed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(elapsed, 3),
            # 每个协议的完整探测结果（含 None = 本次不可达），便于事后诊断
            "results": {ip: dict(per) for ip, per in results.items() if per},
            # 按延迟升序的可达主机（运行时即按此顺序取用）
            "hosts": hosts_by_proto,
            # 诊断用：符合"前N名或<200ms"的可达主机
            "fast": {p: [h for h in ips if h in fast]
                     for p, ips in hosts_by_proto.items()},
        }

    def _write(self, payload: dict) -> None:
        """原子写入：先写临时文件再 ``os.replace``，避免读到半截 JSON。"""
        target = self.cache_file()
        tmp = target.with_name(f"{target.name}.tmp{os.getpid()}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, target)
            log.debug("当日测速结果已缓存: %s", target)
        except OSError as exc:
            log.debug("测速结果写入失败（忽略）: %s", str(exc)[:120])
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


# 进程级单例（标准 / MAC / EX 三池共享，确保当日只真正测一次）
_probe_cache = ProbeCache()


def get_probe_cache() -> ProbeCache:
    return _probe_cache


def schedule_probe(hosts: list[str] | None = None) -> asyncio.Task | None:
    """[连接池用] 把当日测速挂到后台并返回 Task，供调用方持有 / 取消。

    返回 ``None`` 表示当前没有运行中的事件循环（此时无法起后台任务）。
    """
    try:
        return asyncio.create_task(
            get_probe_cache().ensure_probe(hosts), name="pytdxdata-daily-probe"
        )
    except RuntimeError:
        return None
