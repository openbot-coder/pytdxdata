"""权威价格小数位表的同步与缓存（对用户无感）。

为什么需要它
------------
``get_quotes`` 解析报价时，协议给的是**整数**，真实价格 = ``整数 / 10**小数位``。
小数位在协议里**不是**随报价一起下发的，只能靠推断（见
:func:`pytdxdata.protocol.commands.quotes.price_decimal_digits`）。

但证券清单接口（``get_universe``，底层 ``GetSecurityListCmd``）**自带**这个字段：
``SecurityInfo.decimal_point``，同时还带 ``pre_close``。这就是权威来源。本模块把它
灌进 ``price_decimal_digits`` 使用的运行时表，使价格标度不再依赖硬编码前缀规则。

行为
----
- **当日首次启动**检查缓存文件（``decimal-points.json``）：命中就直接装表（纯内存，
  无网络、约 10 ms）；未命中则挂一个**后台 task** 拉证券清单并写缓存，
  当天不再重复。
- 后台 task 不阻塞 ``start()``，不打印、不抛异常（取消除外），失败只落 debug 日志。
- 未同步成功时 ``price_decimal_digits`` 静默退回静态前缀规则，行为与 0.6.5 一致。
- 证券清单本身已被 ``cache/`` 层缓存，因此同步的实际网络开销只在**首次**
  （约 26 + 25 个分页请求）。

开关
----
- ``PYPYTDX_DATA_DISABLE_DECIMAL_SYNC=1`` 关闭后台同步（CI / 测试用，保证确定性）。
- ``PYPYTDX_DATA_CACHE_DIR`` 覆盖缓存目录（测试指向临时目录）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import decimal_cache_file, decimal_sync_disabled
from .protocol.commands.quotes import (
    clear_decimal_map,
    decimal_map_source,
    decimal_map_stats,
    install_decimal_map,
)

if TYPE_CHECKING:  # pragma: no cover
    from .api import TdxData

log = logging.getLogger(__name__)

#: 同步哪些市场（只对 A 股有意义：扩展市场报价本身不含标度歧义）
DEFAULT_MARKETS: tuple[str, ...] = ("sh", "sz")

_CN_NAME = {"sh": 1, "sz": 0}

_state: dict[str, Any] = {"loaded": "", "task": None}


# ---------------------------------------------------------------------- #
# 磁盘缓存
# ---------------------------------------------------------------------- #

def cache_file() -> Path:
    """缓存文件路径（每次重新解析，便于测试改环境变量）。"""
    return decimal_cache_file()


def _read_cache(path: Path | None = None) -> dict[str, dict[str, int]] | None:
    """读缓存 → ``{"1": {code: dp}}``；缺失 / 损坏 → ``None``。"""
    target = Path(path) if path is not None else cache_file()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("date") != date.today().isoformat():
        return None
    markets = raw.get("markets")
    if not isinstance(markets, dict) or not markets:
        return None
    out: dict[str, dict[str, int]] = {}
    for k, v in markets.items():
        if not isinstance(v, dict):
            continue
        try:
            out[str(int(k))] = {str(c): int(d) for c, d in v.items() if int(d) > 0}
        except (TypeError, ValueError):
            continue
    return out or None


def load_cached(path: Path | None = None) -> int:
    """把当日缓存装进权威表；成功返回覆盖条目数，否则 0。"""
    if decimal_sync_disabled():
        return 0
    markets = _read_cache(path)
    if not markets:
        return 0
    items = [(int(m), c, d) for m, codes in markets.items() for c, d in codes.items()]
    stats = install_decimal_map(items, source="cache")
    _state["loaded"] = date.today().isoformat()
    log.debug("小数位表已从缓存加载: %s", stats)
    return int(stats["stock"])


def _write_cache(markets: dict[str, dict[str, int]], path: Path | None = None) -> None:
    """原子写缓存（tmp + ``os.replace``），失败只落 debug 日志。"""
    target = Path(path) if path is not None else cache_file()
    payload = {"date": date.today().isoformat(), "markets": markets}
    tmp = target.with_suffix(".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, target)
    except OSError as e:  # pragma: no cover - 磁盘不可写等
        log.debug("小数位表缓存写入失败: %s", e)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------- #
# 同步
# ---------------------------------------------------------------------- #

async def refresh(td: "TdxData", markets: tuple[str, ...] = DEFAULT_MARKETS,
                  *, path: Path | None = None) -> dict[str, int]:
    """拉证券清单 → 装表 + 写缓存。返回统计。

    证券清单走 ``get_universe``（含 ``cache/`` 层缓存），只有首次真正发请求。
    """
    tables: dict[str, dict[str, int]] = {}
    items: list[tuple[int, str, int]] = []
    for name in markets:
        m = _CN_NAME.get(name)
        if m is None:
            continue
        rows = await td.get_universe(name)
        table: dict[str, int] = {}
        for it in rows:
            dp = int(getattr(it, "decimal_point", 0) or 0)
            code = str(getattr(it, "code", "") or "").strip().rstrip("\x00")
            if dp <= 0 or not code:
                continue
            table[code] = dp
            items.append((m, code, dp))
        if table:
            tables[str(m)] = table

    stats = install_decimal_map(items, source="universe")
    _state["loaded"] = date.today().isoformat()
    if tables:
        _write_cache(tables, path)
    log.debug("小数位表同步完成: %s", stats)
    return stats


async def _run(td: "TdxData") -> dict[str, int]:
    try:
        return await refresh(td)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # pragma: no cover - 网络/解析异常都不该影响用户
        log.debug("小数位表后台同步失败: %s", e)
        return {}


def schedule(td: "TdxData") -> asyncio.Task | None:
    """[内部] 需要时把同步挂到后台并返回 Task（调用方持有 / 取消）。

    - 已关闭（``PYPYTDX_DATA_DISABLE_DECIMAL_SYNC=1``）→ ``None``
    - 当日缓存命中 → 立即装表（同步、无网络），返回 ``None``
    - 否则创建后台 task；进程内单例，重复调用复用同一个 task
    """
    if decimal_sync_disabled():
        return None
    if decimal_map_stats()["stock"] and _state["loaded"] == date.today().isoformat():
        return None
    if load_cached():
        return None
    task = _state.get("task")
    if task is not None and not task.done():
        return task
    task = asyncio.get_running_loop().create_task(_run(td), name="pytdxdata-decimal-sync")
    _state["task"] = task
    return task


async def cancel(task: asyncio.Task | None) -> None:
    """取消并等待后台同步 task（供 ``TdxData.close()`` 调用）。"""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 关闭路径不抛
        pass


def status() -> dict[str, Any]:
    """当前状态快照（排障 / 测试用）。"""
    task = _state.get("task")
    return {
        "loaded": _state["loaded"],
        "source": decimal_map_source(),
        "syncing": bool(task is not None and not task.done()),
        **decimal_map_stats(),
    }


def reset() -> None:
    """清空内存状态（测试用；不动磁盘缓存）。"""
    task = _state.get("task")
    if task is not None and not task.done():
        task.cancel()
    _state["task"] = None
    _state["loaded"] = ""
    clear_decimal_map()
